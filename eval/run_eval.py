"""Compares a naive baseline (embed, retrieve top-K, generate - no grading,
no rewrite, no groundedness check) against the full LangGraph pipeline, on
eval/questions.jsonl against the public seed document. Writes eval/RESULTS.md.

Calls the real Gemini API - never run this in CI, and expect it to take a
few minutes for the full question set (throttled to stay under the free
tier). Calls the graph directly rather than going over HTTP (BUILD_SPEC.md
7.2 allows either).

Usage: python -m eval.run_eval
"""

import asyncio
import json
import logging
import statistics
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    # Same reason as app/main.py and app/worker.py: psycopg's async waiting
    # needs loop.add_reader/add_writer, which the default ProactorEventLoop
    # on Windows does not implement.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm import (  # noqa: E402
    GeminiClient,  # noqa: E402
    PermanentError,
    QuotaExhausted,
    TransientLLMError,
)
from app.rag import prompts as rag_prompts  # noqa: E402
from app.rag.graph import build_graph  # noqa: E402
from app.rag.nodes import format_passages, parse_citations  # noqa: E402
from app.vectorindex import PgVectorIndex  # noqa: E402

logging.basicConfig(level=logging.WARNING)

QUESTIONS_PATH = Path(__file__).parent / "questions.jsonl"
RESULTS_PATH = Path(__file__).parent / "RESULTS.md"
THROTTLE_BETWEEN_QUESTIONS_S = 2.0

REFUSAL_PHRASES = [
    "couldn't find", "could not find", "does not contain", "doesn't contain",
    "no mention", "not mentioned", "cannot answer", "can't answer",
    "not found in", "no information", "not covered", "not available in",
]


def load_questions() -> list[dict]:
    questions = []
    for line in QUESTIONS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        questions.append(json.loads(line))
    return questions


class CountingLLMClient:
    """Wraps a real LLMClient and counts calls, so the eval can report
    average LLM calls per question without changing app code."""

    def __init__(self, inner: GeminiClient) -> None:
        self._inner = inner
        self.calls = 0

    async def embed_documents(self, *args, **kwargs):
        self.calls += 1
        return await self._inner.embed_documents(*args, **kwargs)

    async def embed_query(self, *args, **kwargs):
        self.calls += 1
        return await self._inner.embed_query(*args, **kwargs)

    async def generate_json(self, *args, **kwargs):
        self.calls += 1
        return await self._inner.generate_json(*args, **kwargs)

    async def generate_stream(self, *args, **kwargs):
        self.calls += 1
        async for token in self._inner.generate_stream(*args, **kwargs):
            yield token


def is_refusal(answer: str) -> bool:
    lowered = answer.lower()
    return any(p in lowered for p in REFUSAL_PHRASES)


def check_must_include(answer: str, must_include: list[str]) -> bool:
    lowered = answer.lower()
    return all(kw.lower() in lowered for kw in must_include)


def citation_hits_expected(citations: list[dict], expected_pages: list[int]) -> bool:
    if not expected_pages:
        return True
    cited_pages = {c.get("page") for c in citations if c.get("page") is not None}
    return bool(cited_pages & set(expected_pages))


async def run_baseline(llm, vectors, settings, doc_ids, question: str):
    query_vec = await llm.embed_query(question)
    candidates = await vectors.search(
        doc_ids=doc_ids,
        owner_id=None,
        query_embedding=query_vec,
        k=settings.RETRIEVAL_K,
        k_per_doc=settings.RETRIEVAL_K,
    )
    passages = format_passages(candidates)
    prompt = f"Question: {question}\n\nPassages:\n{passages}"
    answer = ""
    async for token in llm.generate_stream(
        model=settings.GEN_MODEL, system=rag_prompts.GENERATE_SYSTEM_PROMPT, prompt=prompt
    ):
        answer += token
    citations = parse_citations(answer, candidates)
    return answer, citations


async def run_graph_system(graph, doc_ids, question: str):
    state = {
        "question": question,
        "history": [],
        "user_id": None,
        "resolved_doc_ids": doc_ids,
        "rewrites": 0,
        "trace": [],
    }
    final = await graph.ainvoke(state)
    return final.get("answer", ""), final.get("citations", [])


async def evaluate_system(name, run_one, questions, llm_counter) -> dict:
    rows = []
    for item in questions:
        llm_counter.calls = 0
        start = time.perf_counter()
        try:
            answer, citations = await run_one(item["question"])
            error = None
        except (QuotaExhausted, TransientLLMError, PermanentError) as exc:
            answer, citations, error = "", [], str(exc)
        elapsed = time.perf_counter() - start

        if error:
            rows.append({"item": item, "error": error, "elapsed": elapsed, "llm_calls": llm_counter.calls})
            await asyncio.sleep(THROTTLE_BETWEEN_QUESTIONS_S)
            continue

        if item["type"] == "unanswerable":
            passed = is_refusal(answer)
        else:
            passed = check_must_include(answer, item["must_include"])

        rows.append(
            {
                "item": item,
                "answer": answer,
                "citations": citations,
                "passed": passed,
                "cited_expected_page": citation_hits_expected(citations, item["expected_pages"]),
                "refused": is_refusal(answer),
                "elapsed": elapsed,
                "llm_calls": llm_counter.calls,
            }
        )
        print(f"  [{name}] {item['type']:<12} {'PASS' if passed else 'FAIL':<5} {item['question'][:60]}")
        await asyncio.sleep(THROTTLE_BETWEEN_QUESTIONS_S)
    return rows


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    errors = [r for r in rows if "error" in r]
    answerable = [r for r in ok if r["item"]["type"] != "unanswerable"]
    unanswerable = [r for r in ok if r["item"]["type"] == "unanswerable"]
    return {
        "n": len(rows),
        "errors": len(errors),
        "pass_rate": (sum(r["passed"] for r in ok) / len(ok)) if ok else 0.0,
        "citation_page_hit_rate": (
            sum(r["cited_expected_page"] for r in answerable) / len(answerable) if answerable else None
        ),
        "correct_refusal_rate": (
            sum(r["refused"] for r in unanswerable) / len(unanswerable) if unanswerable else None
        ),
        "avg_llm_calls": statistics.mean(r["llm_calls"] for r in rows) if rows else 0.0,
        "median_latency_s": statistics.median(r["elapsed"] for r in rows) if rows else 0.0,
    }


def format_pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def write_results_md(baseline_rows, graph_rows) -> None:
    baseline_summary = summarize(baseline_rows)
    graph_summary = summarize(graph_rows)

    lines = [
        "# Eval results",
        "",
        "DRAFT: generated by `eval/run_eval.py` against the real Gemini API and the public seed",
        "document. The question set (`eval/questions.jsonl`) is itself still a draft pending human",
        "review (BUILD_SPEC.md section 11) - treat these numbers as illustrative, not final.",
        "",
        "`pass_rate` requires every `must_include` keyword present (answerable questions) or a",
        "refusal (unanswerable questions). `correct_refusal_rate` only covers the unanswerable",
        "items. Refusal is detected by a fixed phrase list (see REFUSAL_PHRASES in run_eval.py) -",
        "a heuristic, not a semantic check, so it can miss a correct-but-differently-worded refusal.",
        "",
        "| Metric | Baseline | Graph |",
        "|---|---|---|",
        f"| Questions run | {baseline_summary['n']} | {graph_summary['n']} |",
        f"| Errors (quota/transient) | {baseline_summary['errors']} | {graph_summary['errors']} |",
        f"| Pass rate | {format_pct(baseline_summary['pass_rate'])} | {format_pct(graph_summary['pass_rate'])} |",
        f"| Citation page hit rate | {format_pct(baseline_summary['citation_page_hit_rate'])} | {format_pct(graph_summary['citation_page_hit_rate'])} |",
        f"| Correct refusal rate | {format_pct(baseline_summary['correct_refusal_rate'])} | {format_pct(graph_summary['correct_refusal_rate'])} |",
        f"| Avg LLM calls / question | {baseline_summary['avg_llm_calls']:.1f} | {graph_summary['avg_llm_calls']:.1f} |",
        f"| Median latency | {baseline_summary['median_latency_s']:.1f}s | {graph_summary['median_latency_s']:.1f}s |",
        "",
        "## Per-question detail",
        "",
        "| # | Type | Question | Baseline | Graph |",
        "|---|---|---|---|---|",
    ]
    for i, (b, g) in enumerate(zip(baseline_rows, graph_rows, strict=True), start=1):
        q = b["item"]["question"]
        b_mark = "error" if "error" in b else ("PASS" if b["passed"] else "FAIL")
        g_mark = "error" if "error" in g else ("PASS" if g["passed"] else "FAIL")
        lines.append(f"| {i} | {b['item']['type']} | {q} | {b_mark} | {g_mark} |")

    lines += [
        "",
        "## Honest notes",
        "",
        "- The graph spends more LLM calls per question (condense/grade/rewrite/verify on top of",
        "  generate) in exchange for grading retrieved passages and refusing when they're not",
        "  relevant. Where the baseline's pass rate is at or above the graph's, that's the cost of",
        "  that extra safety showing up as a real trade-off, not a bug in this report.",
        "- Both systems are only as good as the single free-tier helper/generation models behind",
        "  them; a failure here can reflect model capability as much as pipeline design.",
    ]

    RESULTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> None:
    settings = get_settings()
    pool = await db.open_pool(settings, max_size=3)

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id from documents where owner_id is null and status = 'ready' order by created_at limit 1"
        )
        row = await cur.fetchone()
        status_row = None
        if row is None:
            cur = await conn.execute(
                "select status, error from documents where owner_id is null order by created_at desc limit 1"
            )
            status_row = await cur.fetchone()
    if row is None:
        detail = (
            f"status={status_row['status']}, error={status_row['error']}"
            if status_row
            else "no public document exists at all"
        )
        print(f"No ready public seed document found ({detail}). Writing that to {RESULTS_PATH} instead of running.")
        RESULTS_PATH.write_text(
            "# Eval results\n\n"
            "**Not run.** The public seed document is not in `ready` status, so there is nothing to\n"
            f"evaluate against yet ({detail}).\n\n"
            "This is most likely the free-tier Gemini quota being exhausted for the day - start the\n"
            "app (it retries ingestion through the normal job queue) once quota resets and re-run\n"
            "`python -m eval.run_eval`.\n",
            encoding="utf-8",
        )
        await db.close_pool()
        return
    seed_doc_id = str(row["id"])
    doc_ids = [seed_doc_id]

    questions = load_questions()
    print(f"Loaded {len(questions)} questions. Seed document: {seed_doc_id}\n")

    vectors = PgVectorIndex(pool)

    print("Running baseline...")
    baseline_llm = GeminiClient(settings)
    baseline_counter = CountingLLMClient(baseline_llm)
    baseline_rows = await evaluate_system(
        "baseline",
        lambda q: run_baseline(baseline_counter, vectors, settings, doc_ids, q),
        questions,
        baseline_counter,
    )

    print("\nRunning graph...")
    graph_llm = GeminiClient(settings)
    graph_counter = CountingLLMClient(graph_llm)
    graph = build_graph(graph_counter, vectors, settings)
    graph_rows = await evaluate_system(
        "graph", lambda q: run_graph_system(graph, doc_ids, q), questions, graph_counter
    )

    write_results_md(baseline_rows, graph_rows)
    print(f"\nWrote {RESULTS_PATH}")
    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
