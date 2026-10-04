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


class ChunkCandidate(TypedDict):
    id: int
    document_id: str
    page: int | None
    content: str
    filename: str
    dist: float


class VectorIndex(Protocol):
    async def upsert_chunks(
        self, *, document_id: str, owner_id: str | None, chunks: list[ChunkInput]
    ) -> None: ...

    async def search(
        self,
        *,
        doc_ids: list[str],
        owner_id: str,
        query_embedding: list[float],
        k: int,
        k_per_doc: int,
    ) -> list[ChunkCandidate]: ...


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

    async def search(
        self,
        *,
        doc_ids: list[str],
        owner_id: str,
        query_embedding: list[float],
        k: int,
        k_per_doc: int,
    ) -> list[ChunkCandidate]:
        """Per-document-balanced retrieval: no single document can crowd out
        the others, since each contributes at most k_per_doc candidates.
        v1 uses exact vector search with no ANN index (see README "Design
        decisions"): restricting by document id keeps this fast at this
        scale and avoids the recall problems of filtered HNSW."""
        if not doc_ids:
            return []
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """with ranked as (
                     select c.id, c.document_id, c.page, c.content, d.filename,
                            c.embedding <=> %(q)s::vector as dist,
                            row_number() over (
                              partition by c.document_id order by c.embedding <=> %(q)s::vector
                            ) rn
                     from chunks c join documents d on d.id = c.document_id
                     where c.document_id = any(%(doc_ids)s)
                       and (c.owner_id = %(uid)s or c.owner_id is null)
                   )
                   select id, document_id, page, content, filename, dist
                   from ranked where rn <= %(k_per_doc)s
                   order by dist limit %(k)s""",
                {
                    "q": query_embedding,
                    "doc_ids": doc_ids,
                    "uid": owner_id,
                    "k_per_doc": k_per_doc,
                    "k": k,
                },
            )
            return await cur.fetchall()
