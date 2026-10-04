import pytest
from conftest import insert_document
from docx_fixtures import make_docx_bytes
from fakes import FakeLLMClient
from pdf_fixtures import make_pdf_bytes

from app import db
from app.config import get_settings
from app.ingestion.pipeline import PermanentIngestError, run_ingestion
from app.storage import PostgresBlobStore
from app.vectorindex import PgVectorIndex

PAGE_1 = "Hello from page one, this has enough text to form a real chunk of content."
PAGE_2 = "Second page content here with a different set of words entirely."


async def _pool():
    settings = get_settings()
    return await db.open_pool(settings, max_size=5)


async def _setup(pool, *, mime: str, data: bytes, filename: str = "test.pdf") -> str:
    document_id = await insert_document(pool, filename=filename, mime=mime, size_bytes=len(data))
    async with pool.connection() as conn:
        await PostgresBlobStore(pool).put(conn, document_id, mime, data)
    return document_id


async def _run(pool, document_id: str, llm=None):
    settings = get_settings()
    llm = llm or FakeLLMClient()
    await run_ingestion(pool, llm, PostgresBlobStore(pool), PgVectorIndex(pool), settings, {"document_id": document_id})
    return llm


async def _get_document(pool, document_id: str) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute("select * from documents where id = %s", (document_id,))
        return await cur.fetchone()


async def _chunk_count(pool, document_id: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("select count(*) as n from chunks where document_id = %s", (document_id,))
        return (await cur.fetchone())["n"]


async def test_pdf_ingestion_succeeds_and_sets_ready():
    pool = await _pool()
    data = make_pdf_bytes([PAGE_1, PAGE_2])
    document_id = await _setup(pool, mime="application/pdf", data=data)

    await _run(pool, document_id)

    doc = await _get_document(pool, document_id)
    assert doc["status"] == "ready"
    assert doc["progress"] == 100
    assert doc["page_count"] == 2
    assert doc["chunk_count"] == await _chunk_count(pool, document_id)
    assert doc["chunk_count"] > 0


async def test_docx_ingestion_is_unpaged():
    pool = await _pool()
    data = make_docx_bytes([PAGE_1, PAGE_2])
    document_id = await _setup(
        pool,
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        data=data,
        filename="test.docx",
    )

    await _run(pool, document_id)

    doc = await _get_document(pool, document_id)
    assert doc["status"] == "ready"
    assert doc["page_count"] is None

    async with pool.connection() as conn:
        cur = await conn.execute("select page from chunks where document_id = %s", (document_id,))
        pages = [row["page"] for row in await cur.fetchall()]
    assert all(p is None for p in pages)


async def test_corrupt_pdf_fails_permanently_without_retry():
    pool = await _pool()
    data = b"%PDF-1.4\n" + bytes(range(256)) * 4  # magic bytes, garbage body
    document_id = await _setup(pool, mime="application/pdf", data=data)

    with pytest.raises(PermanentIngestError):
        await _run(pool, document_id)


async def test_scanned_pdf_with_no_text_shows_ocr_message():
    pool = await _pool()
    data = make_pdf_bytes(["", ""])  # structurally valid, no extractable text
    document_id = await _setup(pool, mime="application/pdf", data=data)

    with pytest.raises(PermanentIngestError, match="OCR"):
        await _run(pool, document_id)


async def test_permanent_llm_error_wraps_as_permanent_ingest_error():
    pool = await _pool()
    data = make_pdf_bytes([PAGE_1])
    document_id = await _setup(pool, mime="application/pdf", data=data)

    llm = FakeLLMClient(fail_times=1, permanent=True)
    with pytest.raises(PermanentIngestError):
        await _run(pool, document_id, llm=llm)


async def test_transient_llm_error_propagates_for_job_level_retry():
    """run_ingestion itself does not retry; a transient LLM error must bubble
    up so the worker's job-level backoff (queue.fail_transient) takes over."""
    from app.llm import QuotaExhausted

    pool = await _pool()
    data = make_pdf_bytes([PAGE_1])
    document_id = await _setup(pool, mime="application/pdf", data=data)

    llm = FakeLLMClient(fail_times=1, permanent=False)
    with pytest.raises(QuotaExhausted):
        await _run(pool, document_id, llm=llm)


async def test_deleted_document_before_ingestion_exits_quietly():
    pool = await _pool()
    data = make_pdf_bytes([PAGE_1])
    document_id = await _setup(pool, mime="application/pdf", data=data)

    async with pool.connection() as conn:
        await conn.execute("delete from documents where id = %s", (document_id,))

    await _run(pool, document_id)  # must not raise


async def test_rerunning_ingestion_does_not_duplicate_chunks():
    """Models what a reclaimed-after-crash retry looks like: the same document
    gets ingested twice. ON CONFLICT (document_id, chunk_index) must keep the
    chunk count stable instead of doubling it."""
    pool = await _pool()
    data = make_pdf_bytes([PAGE_1, PAGE_2])
    document_id = await _setup(pool, mime="application/pdf", data=data)

    await _run(pool, document_id)
    first_count = await _chunk_count(pool, document_id)

    await _run(pool, document_id)
    second_count = await _chunk_count(pool, document_id)

    assert first_count > 0
    assert second_count == first_count
