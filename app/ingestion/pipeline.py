"""Ingestion pipeline: parse -> chunk -> embed -> ready. Updates documents.status
and .progress at each stage so the UI can poll live progress (spec 6.4).
"""

import asyncio
import logging

from psycopg_pool import AsyncConnectionPool

from app.ingestion import chunking, parsers
from app.llm import LLMClient
from app.llm import PermanentError as LLMPermanentError
from app.storage import BlobStore
from app.vectorindex import VectorIndex

logger = logging.getLogger(__name__)


class PermanentIngestError(Exception):
    """Not retryable: corrupt file, no extractable text, over a limit, or a
    permanent LLM error (e.g. a rejected model name)."""


async def _get_document(pool: AsyncConnectionPool, document_id: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute("select * from documents where id = %s", (document_id,))
        return await cur.fetchone()


async def _update(pool: AsyncConnectionPool, document_id: str, set_clause: str, params: dict) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            f"update documents set {set_clause}, updated_at = now() where id = %(id)s",
            {**params, "id": document_id},
        )


async def run_ingestion(
    pool: AsyncConnectionPool,
    llm: LLMClient,
    blobs: BlobStore,
    vectors: VectorIndex,
    settings,
    payload: dict,
) -> None:
    document_id = payload["document_id"]
    doc = await _get_document(pool, document_id)
    if doc is None:
        logger.info("document %s deleted before ingestion started", document_id)
        return

    # 1. parsing (0-15%)
    await _update(pool, document_id, "status = 'parsing', progress = 0", {})
    blob = await blobs.get(document_id)
    if blob is None:
        raise PermanentIngestError("Uploaded file is missing")
    _content_type, data = blob
    try:
        pages = await asyncio.to_thread(parsers.parse, doc["mime"], data, settings.MAX_PAGES)
    except parsers.PermanentParseError as exc:
        raise PermanentIngestError(str(exc)) from exc
    await _update(pool, document_id, "progress = 15", {})

    if await _get_document(pool, document_id) is None:
        logger.info("document %s deleted mid-ingest (after parsing)", document_id)
        return

    # 2. chunking (to ~25%)
    paged = doc["mime"] in parsers.PAGED_MIMES
    await _update(pool, document_id, "status = 'chunking', progress = 15", {})
    chunks = await asyncio.to_thread(
        chunking.chunk_pages, pages, paged=paged, max_chunks=settings.MAX_CHUNKS_PER_DOC
    )
    if not chunks:
        raise PermanentIngestError("No extractable text after chunking")
    await _update(pool, document_id, "progress = 25", {})

    if await _get_document(pool, document_id) is None:
        logger.info("document %s deleted mid-ingest (after chunking)", document_id)
        return

    # 3. embedding (to ~95%), batched and upserted so retries/reclaims never duplicate chunks
    await _update(pool, document_id, "status = 'embedding', progress = 25", {})
    batch_size = settings.EMBED_BATCH_SIZE
    total_batches = (len(chunks) + batch_size - 1) // batch_size
    for batch_num in range(total_batches):
        if await _get_document(pool, document_id) is None:
            logger.info("document %s deleted mid-ingest (during embedding)", document_id)
            return

        batch = chunks[batch_num * batch_size : (batch_num + 1) * batch_size]
        try:
            batch_vectors = await llm.embed_documents([c.content for c in batch], title=doc["filename"])
        except LLMPermanentError as exc:
            raise PermanentIngestError(f"Embedding rejected: {exc}") from exc

        await vectors.upsert_chunks(
            document_id=document_id,
            owner_id=doc["owner_id"],
            chunks=[
                {"chunk_index": c.index, "page": c.page, "content": c.content, "embedding": v}
                for c, v in zip(batch, batch_vectors, strict=True)
            ],
        )
        progress = min(25 + round(70 * (batch_num + 1) / total_batches), 95)
        await _update(pool, document_id, "progress = %(p)s", {"p": progress})

    # 4. ready
    page_count = len(pages) if paged else None
    await _update(
        pool,
        document_id,
        "status = 'ready', progress = 100, chunk_count = %(cc)s, page_count = %(pc)s, error = null",
        {"cc": len(chunks), "pc": page_count},
    )
