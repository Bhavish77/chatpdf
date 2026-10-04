"""pgvector seam: writes embedded chunks, and (from Phase 3) runs the
per-document-balanced similarity search the chat graph retrieves with.

# PROD: swap for Qdrant, Pinecone, Weaviate, OpenSearch, or Vertex AI Vector
# Search at larger scale, or add HNSW + iterative scan if staying on Postgres.
"""

from typing import Protocol, TypedDict

from psycopg_pool import AsyncConnectionPool


class ChunkInput(TypedDict):
    chunk_index: int
    page: int | None
    content: str
    embedding: list[float]


class VectorIndex(Protocol):
    async def upsert_chunks(
        self, *, document_id: str, owner_id: str | None, chunks: list[ChunkInput]
    ) -> None: ...


class PgVectorIndex:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def upsert_chunks(
        self, *, document_id: str, owner_id: str | None, chunks: list[ChunkInput]
    ) -> None:
        if not chunks:
            return
        async with self._pool.connection() as conn, conn.transaction():
            for c in chunks:
                await conn.execute(
                    """insert into chunks (document_id, owner_id, chunk_index, page, content, embedding)
                       values (%(document_id)s, %(owner_id)s, %(chunk_index)s, %(page)s,
                               %(content)s, %(embedding)s)
                       on conflict (document_id, chunk_index) do update
                         set page = excluded.page, content = excluded.content,
                             embedding = excluded.embedding""",
                    {
                        "document_id": document_id,
                        "owner_id": owner_id,
                        "chunk_index": c["chunk_index"],
                        "page": c["page"],
                        "content": c["content"],
                        "embedding": c["embedding"],
                    },
                )
