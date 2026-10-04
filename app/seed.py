"""Seeds one public, freely redistributable document on startup through the
real ingestion queue, so there's always something to try the demo against.
See BUILD_SPEC.md section 6.9."""

import hashlib
import logging
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from app import queue
from app.storage import PostgresBlobStore

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"
SEED_PDF = SEED_DIR / "elements_of_style.pdf"


async def ensure_seed_document(pool: AsyncConnectionPool) -> None:
    if not SEED_PDF.exists():
        logger.warning("seed PDF not found at %s, skipping", SEED_PDF)
        return

    data = SEED_PDF.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id from documents where sha256 = %s and owner_id is null", (sha256,)
        )
        if await cur.fetchone() is not None:
            return

        async with conn.transaction():
            cur = await conn.execute(
                """insert into documents (owner_id, filename, mime, size_bytes, sha256, expires_at)
                   values (null, %s, 'application/pdf', %s, %s, null) returning id""",
                (SEED_PDF.name, len(data), sha256),
            )
            doc = await cur.fetchone()
            assert doc is not None
            await PostgresBlobStore(pool).put(conn, str(doc["id"]), "application/pdf", data)
            await queue.enqueue(conn, "ingest_document", {"document_id": str(doc["id"])})
    logger.info("seeded public document %s", SEED_PDF.name)
