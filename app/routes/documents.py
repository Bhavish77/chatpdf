"""Upload and manage documents. See BUILD_SPEC.md sections 6.4 and 6.6."""

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from psycopg_pool import AsyncConnectionPool

from app import queue
from app.config import Settings
from app.deps import current_user, get_pool_dep, get_settings_dep
from app.ratelimit import FixedWindowCounter
from app.storage import PostgresBlobStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])

# Process-local, in-memory. # PROD: Redis-backed so limits hold across replicas.
_upload_rate = FixedWindowCounter(window_seconds=3600)

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT_MIME = "text/plain"
MD_MIME = "text/markdown"

_EXT_MIME = {".pdf": PDF_MIME, ".docx": DOCX_MIME, ".txt": TXT_MIME, ".md": MD_MIME}


def _sanitize_filename(name: str | None) -> str:
    name = Path(name or "upload").name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:255] or "upload"


def _detect_mime(filename: str, data: bytes) -> str:
    """Never trust the client's declared content type: check the extension
    against the bytes actually on the wire."""
    ext = Path(filename).suffix.lower()
    mime = _EXT_MIME.get(ext)
    if mime is None:
        raise ValueError(f"Unsupported file extension: {ext or '(none)'}")
    if mime == PDF_MIME and data[:5] != b"%PDF-":
        raise ValueError("File does not look like a PDF")
    if mime == DOCX_MIME and data[:2] != b"PK":
        raise ValueError("File does not look like a DOCX")
    if mime in (TXT_MIME, MD_MIME):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("File is not valid UTF-8 text") from exc
    return mime


def _doc_out(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "filename": row["filename"],
        "status": row["status"],
        "progress": row["progress"],
        "error": row["error"],
        "page_count": row["page_count"],
        "chunk_count": row["chunk_count"],
        "is_public": row["owner_id"] is None,
    }


@router.post("", status_code=202)
async def upload_documents(
    files: list[UploadFile],
    user: dict = Depends(current_user),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
    settings: Settings = Depends(get_settings_dep),
) -> list[dict]:
    if not files:
        raise HTTPException(status_code=422, detail="No files provided")

    rate_key = str(user["id"])
    if _upload_rate.increment(rate_key) > settings.UPLOAD_RATE_PER_HOUR:
        retry_after = _upload_rate.retry_after(rate_key) or 3600
        raise HTTPException(
            status_code=429,
            detail="Too many uploads, try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) as docs, coalesce(sum(size_bytes), 0) as bytes from documents where owner_id = %s",
            (user["id"],),
        )
        usage = await cur.fetchone()
        cur = await conn.execute("select coalesce(sum(size_bytes), 0) as bytes from documents")
        total = await cur.fetchone()

    doc_count = usage["docs"]
    user_bytes = int(usage["bytes"])
    total_bytes = int(total["bytes"])
    max_file_bytes = settings.MAX_FILE_MB * 1_000_000
    max_user_bytes = settings.MAX_USER_STORAGE_MB * 1_000_000
    max_total_bytes = settings.MAX_TOTAL_STORAGE_MB * 1_000_000

    blobs = PostgresBlobStore(pool)
    results = []
    for upload in files:
        data = await upload.read()
        size = len(data)

        if doc_count >= settings.MAX_DOCS_PER_USER:
            raise HTTPException(status_code=413, detail="Document limit reached for this account")
        if size > max_file_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{upload.filename}: file exceeds the {settings.MAX_FILE_MB} MB limit",
            )
        if user_bytes + size > max_user_bytes:
            raise HTTPException(status_code=413, detail="Storage limit reached for this account")
        if total_bytes + size > max_total_bytes:
            raise HTTPException(status_code=503, detail="The demo is full for now, try again later")

        filename = _sanitize_filename(upload.filename)
        try:
            mime = _detect_mime(filename, data)
        except ValueError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc

        sha256 = hashlib.sha256(data).hexdigest()
        expires_at = (
            None
            if settings.DOC_TTL_DAYS == 0
            else datetime.now(UTC) + timedelta(days=settings.DOC_TTL_DAYS)
        )

        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """insert into documents (owner_id, filename, mime, size_bytes, sha256, expires_at)
                   values (%(owner_id)s, %(filename)s, %(mime)s, %(size)s, %(sha256)s, %(expires_at)s)
                   returning *""",
                {
                    "owner_id": user["id"],
                    "filename": filename,
                    "mime": mime,
                    "size": size,
                    "sha256": sha256,
                    "expires_at": expires_at,
                },
            )
            doc = await cur.fetchone()
            assert doc is not None
            await blobs.put(conn, str(doc["id"]), mime, data)
            await queue.enqueue(conn, "ingest_document", {"document_id": str(doc["id"])})

        doc_count += 1
        user_bytes += size
        total_bytes += size
        results.append(_doc_out(doc))

    return results


@router.get("")
async def list_documents(
    user: dict = Depends(current_user), pool: AsyncConnectionPool = Depends(get_pool_dep)
) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """select id, filename, status, progress, error, page_count, chunk_count, owner_id
               from documents where owner_id = %s or owner_id is null
               order by created_at desc""",
            (user["id"],),
        )
        rows = await cur.fetchall()
    return [_doc_out(row) for row in rows]


@router.get("/{document_id}")
async def get_document(
    document_id: str,
    user: dict = Depends(current_user),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """select id, filename, status, progress, error, page_count, chunk_count, owner_id
               from documents where id = %s and (owner_id = %s or owner_id is null)""",
            (document_id, user["id"]),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return _doc_out(row)


@router.get("/{document_id}/file")
async def get_document_file(
    document_id: str,
    user: dict = Depends(current_user),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
) -> Response:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select mime from documents where id = %s and (owner_id = %s or owner_id is null)",
            (document_id, user["id"]),
        )
        doc = await cur.fetchone()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    blob = await PostgresBlobStore(pool).get(document_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="File not found")
    _stored_content_type, data = blob

    if doc["mime"] == PDF_MIME:
        media_type, disposition = PDF_MIME, "inline"
    elif doc["mime"] in (TXT_MIME, MD_MIME):
        media_type, disposition = "text/plain; charset=utf-8", "inline"
    else:
        media_type, disposition = doc["mime"], "attachment"

    return Response(
        content=data,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'{disposition}; filename="{document_id}"',
        },
    )


@router.delete("/{document_id}")
async def delete_document(
    document_id: str,
    user: dict = Depends(current_user),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "delete from documents where id = %s and owner_id = %s returning id",
            (document_id, user["id"]),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "ok"}
