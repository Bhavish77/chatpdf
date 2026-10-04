"""File storage seam: original bytes live in Postgres (`blobs`), keyed by document id.

# PROD: swap for S3 or GCS with presigned direct uploads, virus scanning,
# lifecycle rules, and KMS encryption.
"""

from typing import Protocol

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


class BlobStore(Protocol):
    async def put(self, conn: AsyncConnection, document_id: str, content_type: str, data: bytes) -> None: ...

    async def get(self, document_id: str) -> tuple[str, bytes] | None: ...


class PostgresBlobStore:
    """put() takes an open connection so it can run inside the caller's
    transaction (the document row it references via FK is typically inserted
    in that same transaction, and wouldn't be visible to a separate one yet).
    get() is a standalone read, so it manages its own pooled connection."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def put(self, conn: AsyncConnection, document_id: str, content_type: str, data: bytes) -> None:
        await conn.execute(
            """insert into blobs (document_id, content_type, data) values (%s, %s, %s)
               on conflict (document_id) do update
                 set content_type = excluded.content_type, data = excluded.data""",
            (document_id, content_type, data),
        )

    async def get(self, document_id: str) -> tuple[str, bytes] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "select content_type, data from blobs where document_id = %s", (document_id,)
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return row["content_type"], row["data"]
