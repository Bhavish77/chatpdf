from conftest import insert_document
from fakes import ScriptedLLMClient, deterministic_vector

from app.config import get_settings
from app.rag.graph import build_graph
from app.rag.prompts import NO_ANSWER_MESSAGE
from app.vectorindex import PgVectorIndex

ANON_USER = "00000000-0000-0000-0000-000000000001"


async def _insert_chunk(pool, *, document_id, owner_id, chunk_index, page, content):
    async with pool.connection() as conn:
        cur = await conn.execute(
            """insert into chunks (document_id, owner_id, chunk_index, page, content, embedding)
               values (%s, %s, %s, %s, %s, %s) returning id""",
            (document_id, owner_id, chunk_index, page, content, deterministic_vector(content)),
        )
        row = await cur.fetchone()
        return row["id"]


def _no_grounding_settings():
    return get_settings().model_copy(update={"ENABLE_GROUNDING_CHECK": False, "GRADE_MIN_RELEVANT": 2})


async def test_single_document_question_returns_cited_answer(pool):
    settings = _no_grounding_settings()
    doc_id = await insert_document(pool, filename="manual.pdf", mime="application/pdf")
    id1 = await _insert_chunk(pool, document_id=doc_id, owner_id=None, chunk_index=0, page=1, content="first fact")
    id2 = await _insert_chunk(pool, document_id=doc_id, owner_id=None, chunk_index=1, page=2, content="second fact")

    llm = ScriptedLLMClient(
        generate_json_responses=[{"results": [{"id": id1, "relevant": True}, {"id": id2, "relevant": True}]}],
        generate_stream_text="The answer combines [1] and [2].",
    )
    graph = build_graph(llm, PgVectorIndex(pool), settings)

    final = await graph.ainvoke(
        {"question": "What are the facts?", "history": [], "user_id": ANON_USER, "resolved_doc_ids": [doc_id], "rewrites": 0, "trace": []}
    )

    assert "[1]" in final["answer"] and "[2]" in final["answer"]
    assert {c["n"] for c in final["citations"]} == {1, 2}
    assert {c["document_id"] for c in final["citations"]} == {doc_id}


async def test_single_relevant_chunk_is_enough_when_fewer_candidates_than_grade_min_relevant(pool):
    """Regression: a document with only one chunk total must still be
    answerable even though the default GRADE_MIN_RELEVANT is 2 - "at least 2
    relevant" can never be satisfied if there's only ever 1 candidate.
    Found live against the real API."""
    settings = _no_grounding_settings()  # GRADE_MIN_RELEVANT=2, only 1 chunk exists below
    doc_id = await insert_document(pool, filename="tiny.pdf", mime="application/pdf")
    chunk_id = await _insert_chunk(pool, document_id=doc_id, owner_id=None, chunk_index=0, page=1, content="the only fact")

    llm = ScriptedLLMClient(
        generate_json_responses=[{"results": [{"id": chunk_id, "relevant": True}]}],
        generate_stream_text="The only fact is [1].",
    )
    graph = build_graph(llm, PgVectorIndex(pool), settings)

    final = await graph.ainvoke(
        {"question": "what is the fact?", "history": [], "user_id": ANON_USER, "resolved_doc_ids": [doc_id], "rewrites": 0, "trace": []}
    )

    assert final["rewrites"] == 0
    assert "[1]" in final["answer"]


async def test_two_documents_both_cited(pool):
    settings = _no_grounding_settings()
    doc_a = await insert_document(pool, filename="a.pdf", mime="application/pdf")
    doc_b = await insert_document(pool, filename="b.pdf", mime="application/pdf")
    id_a = await _insert_chunk(pool, document_id=doc_a, owner_id=None, chunk_index=0, page=1, content="fact from a")
    id_b = await _insert_chunk(pool, document_id=doc_b, owner_id=None, chunk_index=0, page=1, content="fact from b")

    llm = ScriptedLLMClient(
        generate_json_responses=[{"results": [{"id": id_a, "relevant": True}, {"id": id_b, "relevant": True}]}],
        generate_stream_text="Combining both: [1] and [2].",
    )
    graph = build_graph(llm, PgVectorIndex(pool), settings)

    final = await graph.ainvoke(
        {
            "question": "Compare a and b",
            "history": [],
            "user_id": ANON_USER,
            "resolved_doc_ids": [doc_a, doc_b],
            "rewrites": 0,
            "trace": [],
        }
    )

    cited_docs = {c["document_id"] for c in final["citations"]}
    assert cited_docs == {doc_a, doc_b}


async def test_rewrite_then_answer(pool):
    settings = _no_grounding_settings()
    doc_id = await insert_document(pool, filename="c.pdf", mime="application/pdf")
    chunk_id = await _insert_chunk(pool, document_id=doc_id, owner_id=None, chunk_index=0, page=1, content="the fact")

    llm = ScriptedLLMClient(
        generate_json_responses=[
            {"results": [{"id": chunk_id, "relevant": False}]},  # grade #1: not enough
            {"query": "a better phrasing of the question"},  # rewrite
            {"results": [{"id": chunk_id, "relevant": True}]},  # grade #2: enough (GRADE_MIN_RELEVANT patched below)
        ],
        generate_stream_text="Found it: [1].",
    )
    settings = settings.model_copy(update={"GRADE_MIN_RELEVANT": 1})
    graph = build_graph(llm, PgVectorIndex(pool), settings)

    final = await graph.ainvoke(
        {"question": "obscurely phrased question", "history": [], "user_id": ANON_USER, "resolved_doc_ids": [doc_id], "rewrites": 0, "trace": []}
    )

    assert final["rewrites"] == 1
    assert "[1]" in final["answer"]
    assert len(llm.generate_json_calls) == 3


async def test_rewrite_then_no_answer(pool):
    settings = _no_grounding_settings().model_copy(update={"GRADE_MIN_RELEVANT": 5})
    doc_id = await insert_document(pool, filename="d.pdf", mime="application/pdf")
    chunk_id = await _insert_chunk(pool, document_id=doc_id, owner_id=None, chunk_index=0, page=1, content="unrelated content")

    llm = ScriptedLLMClient(
        generate_json_responses=[
            {"results": [{"id": chunk_id, "relevant": False}]},  # grade #1
            {"query": "still unrelated"},  # rewrite
            {"results": [{"id": chunk_id, "relevant": False}]},  # grade #2: still not enough
        ],
        generate_stream_text="should not be used",
    )
    graph = build_graph(llm, PgVectorIndex(pool), settings)

    final = await graph.ainvoke(
        {"question": "something unanswerable", "history": [], "user_id": ANON_USER, "resolved_doc_ids": [doc_id], "rewrites": 0, "trace": []}
    )

    assert final["answer"] == NO_ANSWER_MESSAGE
    assert final["citations"] == []
    assert final["rewrites"] == 1


async def test_grounding_check_runs_after_generate(pool):
    settings = get_settings().model_copy(update={"ENABLE_GROUNDING_CHECK": True, "GRADE_MIN_RELEVANT": 1})
    doc_id = await insert_document(pool, filename="e.pdf", mime="application/pdf")
    chunk_id = await _insert_chunk(pool, document_id=doc_id, owner_id=None, chunk_index=0, page=1, content="grounded fact")

    llm = ScriptedLLMClient(
        generate_json_responses=[
            {"results": [{"id": chunk_id, "relevant": True}]},  # grade
            {"grounded": False, "unsupported_claims": ["an extra claim"]},  # verify
        ],
        generate_stream_text="Here is [1] plus an unsupported extra claim.",
    )
    graph = build_graph(llm, PgVectorIndex(pool), settings)

    final = await graph.ainvoke(
        {"question": "what is the fact?", "history": [], "user_id": ANON_USER, "resolved_doc_ids": [doc_id], "rewrites": 0, "trace": []}
    )

    assert final["grounding"] == {"grounded": False, "unsupported_claims": ["an extra claim"]}


async def test_condense_rewrites_followup_using_history(pool):
    settings = _no_grounding_settings().model_copy(update={"GRADE_MIN_RELEVANT": 1})
    doc_id = await insert_document(pool, filename="f.pdf", mime="application/pdf")
    chunk_id = await _insert_chunk(pool, document_id=doc_id, owner_id=None, chunk_index=0, page=1, content="some fact")

    llm = ScriptedLLMClient(
        generate_json_responses=[
            {"standalone_query": "what color is the widget mentioned earlier?"},  # condense
            {"results": [{"id": chunk_id, "relevant": True}]},  # grade
        ],
        generate_stream_text="It is blue [1].",
    )
    graph = build_graph(llm, PgVectorIndex(pool), settings)

    final = await graph.ainvoke(
        {
            "question": "what color is it?",
            "history": [{"role": "user", "content": "tell me about the widget"}, {"role": "assistant", "content": "it's great"}],
            "user_id": ANON_USER,
            "resolved_doc_ids": [doc_id],
            "rewrites": 0,
            "trace": [],
        }
    )

    assert final["standalone_query"] == "what color is the widget mentioned earlier?"
    assert len(llm.generate_json_calls) == 2
