"""Graph node factories. Each node emits its own progress through LangGraph's
custom stream writer (node-level `step` events, plus `token`/`citations`/
`grounding` from generate/verify/no_answer) so the chat route can forward them
as SSE events while the graph is still running.
"""

import re
from collections.abc import Callable

from langgraph.config import get_stream_writer

from app.config import Settings
from app.llm import LLMClient
from app.rag import prompts
from app.rag.state import ChatState
from app.vectorindex import ChunkCandidate, VectorIndex


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def format_passages(chunks: list[ChunkCandidate]) -> str:
    lines = []
    for i, c in enumerate(chunks, start=1):
        page_info = f"p.{c['page']}" if c.get("page") else "unpaged"
        lines.append(f"[{i}] {c['filename']} ({page_info}):\n{c['content']}")
    return "\n\n".join(lines)


def parse_citations(answer: str, relevant: list[ChunkCandidate]) -> list[dict]:
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})
    citations = []
    for n in cited:
        if 1 <= n <= len(relevant):
            c = relevant[n - 1]
            citations.append(
                {
                    "n": n,
                    "document_id": str(c["document_id"]),
                    "filename": c["filename"],
                    "page": c.get("page"),
                    "snippet": c["content"][:200],
                }
            )
    return citations


def make_condense(llm: LLMClient, settings: Settings) -> Callable:
    async def condense(state: ChatState) -> dict:
        writer = get_stream_writer()
        history = state.get("history") or []
        if not history:
            writer({"type": "step", "node": "condense", "detail": "no history, using the question as-is"})
            return {
                "standalone_query": state["question"],
                "trace": [{"node": "condense", "detail": "passthrough (no history)"}],
            }

        writer({"type": "step", "node": "condense", "detail": "rewriting the follow-up as a standalone question"})
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        result = await llm.generate_json(
            model=settings.HELPER_MODEL,
            system=prompts.CONDENSE_SYSTEM_PROMPT,
            prompt=f"Conversation so far:\n{convo}\n\nLatest message: {state['question']}",
            schema=prompts.CONDENSE_SCHEMA,
        )
        standalone = result.get("standalone_query") or state["question"]
        return {"standalone_query": standalone, "trace": [{"node": "condense", "detail": standalone}]}

    return condense


def make_retrieve(llm: LLMClient, vectors: VectorIndex, settings: Settings) -> Callable:
    async def retrieve(state: ChatState) -> dict:
        writer = get_stream_writer()
        writer({"type": "step", "node": "retrieve", "detail": "searching your documents"})
        query_vector = await llm.embed_query(state["standalone_query"])
        doc_ids = state["resolved_doc_ids"]
        n_docs = max(1, len(doc_ids))
        k_per_doc = max(3, _ceil_div(settings.RETRIEVAL_K, n_docs))
        candidates = await vectors.search(
            doc_ids=doc_ids,
            owner_id=state["user_id"],
            query_embedding=query_vector,
            k=settings.RETRIEVAL_K,
            k_per_doc=k_per_doc,
        )
        return {
            "candidates": candidates,
            "trace": [{"node": "retrieve", "detail": f"{len(candidates)} candidates from {n_docs} document(s)"}],
        }

    return retrieve


def make_grade(llm: LLMClient, settings: Settings) -> Callable:
    async def grade(state: ChatState) -> dict:
        writer = get_stream_writer()
        candidates = state.get("candidates") or []
        if not candidates:
            writer({"type": "step", "node": "grade", "detail": "no candidates to grade"})
            return {"relevant": [], "trace": [{"node": "grade", "detail": "0 relevant (no candidates)"}]}

        writer({"type": "step", "node": "grade", "detail": f"checking {len(candidates)} passages"})
        numbered = "\n\n".join(f"[{c['id']}] {c['content'][:300]}" for c in candidates)
        result = await llm.generate_json(
            model=settings.HELPER_MODEL,
            system=prompts.GRADE_SYSTEM_PROMPT,
            prompt=f"Question: {state['standalone_query']}\n\nPassages:\n{numbered}",
            schema=prompts.GRADE_SCHEMA,
        )
        relevant_ids = {r["id"] for r in result.get("results", []) if r.get("relevant")}
        relevant = [c for c in candidates if c["id"] in relevant_ids]
        return {"relevant": relevant, "trace": [{"node": "grade", "detail": f"{len(relevant)} relevant"}]}

    return grade


def make_rewrite(llm: LLMClient, settings: Settings) -> Callable:
    async def rewrite(state: ChatState) -> dict:
        writer = get_stream_writer()
        writer({"type": "step", "node": "rewrite", "detail": "not enough relevant passages, rewriting the query"})
        result = await llm.generate_json(
            model=settings.HELPER_MODEL,
            system=prompts.REWRITE_SYSTEM_PROMPT,
            prompt=f"Original question: {state['standalone_query']}",
            schema=prompts.REWRITE_SCHEMA,
        )
        new_query = result.get("query") or state["standalone_query"]
        return {
            "standalone_query": new_query,
            "rewrites": state.get("rewrites", 0) + 1,
            "trace": [{"node": "rewrite", "detail": new_query}],
        }

    return rewrite


def make_generate(llm: LLMClient, settings: Settings) -> Callable:
    async def generate(state: ChatState) -> dict:
        writer = get_stream_writer()
        writer({"type": "step", "node": "generate", "detail": "writing the answer"})
        relevant = state["relevant"]
        passages = format_passages(relevant)
        prompt = f"Question: {state['standalone_query']}\n\nPassages:\n{passages}"

        full_text = ""
        async for token in llm.generate_stream(
            model=settings.GEN_MODEL, system=prompts.GENERATE_SYSTEM_PROMPT, prompt=prompt
        ):
            full_text += token
            writer({"type": "token", "text": token})

        citations = parse_citations(full_text, relevant)
        writer({"type": "citations", "citations": citations})
        return {
            "answer": full_text,
            "citations": citations,
            "trace": [{"node": "generate", "detail": f"{len(full_text)} chars, {len(citations)} citations"}],
        }

    return generate


def make_verify(llm: LLMClient, settings: Settings) -> Callable:
    async def verify(state: ChatState) -> dict:
        writer = get_stream_writer()
        writer({"type": "step", "node": "verify", "detail": "checking groundedness"})
        passages = format_passages(state["relevant"])
        result = await llm.generate_json(
            model=settings.HELPER_MODEL,
            system=prompts.VERIFY_SYSTEM_PROMPT,
            prompt=f"Answer:\n{state['answer']}\n\nPassages:\n{passages}",
            schema=prompts.VERIFY_SCHEMA,
        )
        grounding = {
            "grounded": bool(result.get("grounded", True)),
            "unsupported_claims": result.get("unsupported_claims") or [],
        }
        writer({"type": "grounding", "grounding": grounding})
        return {"grounding": grounding, "trace": [{"node": "verify", "detail": grounding}]}

    return verify


def make_no_answer() -> Callable:
    async def no_answer(state: ChatState) -> dict:
        writer = get_stream_writer()
        writer({"type": "step", "node": "no_answer", "detail": "no relevant passages found"})
        writer({"type": "token", "text": prompts.NO_ANSWER_MESSAGE})
        writer({"type": "citations", "citations": []})
        return {
            "answer": prompts.NO_ANSWER_MESSAGE,
            "citations": [],
            "trace": [{"node": "no_answer", "detail": "no relevant passages after rewrite"}],
        }

    return no_answer
