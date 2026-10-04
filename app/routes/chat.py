"""Chat: resolves document scope, then streams the LangGraph answer over SSE.
See BUILD_SPEC.md sections 6.5 and 6.6."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.deps import current_user, get_pool_dep
from app.llm import PermanentError, QuotaExhausted, TransientLLMError
from app.rag.prompts import NO_ANSWER_MESSAGE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class ChatBody(BaseModel):
    thread_id: str | None = None
    message: str
    doc_ids: list[str] | None = None


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _resolve_doc_ids(pool: AsyncConnectionPool, user_id: str, requested: list[str] | None) -> list[str]:
    """No ids given: every ready document the user can see (own + public).
    Ids given: every one of them must resolve to a ready document the user
    owns or a public one, else 404 - never revealing that a foreign id
    belongs to someone else."""
    async with pool.connection() as conn:
        if requested:
            cur = await conn.execute(
                """select id from documents where id = any(%(ids)s) and status = 'ready'
                   and (owner_id = %(uid)s or owner_id is null)""",
                {"ids": requested, "uid": user_id},
            )
            found = {str(row["id"]) for row in await cur.fetchall()}
            if set(requested) - found:
                raise HTTPException(status_code=404, detail="Document not found")
            return list(found)

        cur = await conn.execute(
            "select id from documents where status = 'ready' and (owner_id = %s or owner_id is null)",
            (user_id,),
        )
        return [str(row["id"]) for row in await cur.fetchall()]


async def _ensure_thread(pool: AsyncConnectionPool, user_id: str, thread_id: str | None) -> str:
    async with pool.connection() as conn:
        if thread_id:
            cur = await conn.execute(
                "select id from threads where id = %s and owner_id = %s", (thread_id, user_id)
            )
            if await cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Thread not found")
            return thread_id
        cur = await conn.execute("insert into threads (owner_id) values (%s) returning id", (user_id,))
        row = await cur.fetchone()
        assert row is not None
        return str(row["id"])


async def _load_history(pool: AsyncConnectionPool, thread_id: str) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select role, content from messages where thread_id = %s order by created_at desc limit 6",
            (thread_id,),
        )
        rows = await cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def _save_messages(
    pool: AsyncConnectionPool,
    *,
    thread_id: str,
    doc_ids: list[str],
    question: str,
    answer: str,
    citations: list[dict],
    trace: list[dict],
    grounding: dict | None,
) -> int:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "insert into messages (thread_id, role, content, doc_ids) values (%s, 'user', %s, %s)",
            (thread_id, question, doc_ids),
        )
        cur = await conn.execute(
            """insert into messages (thread_id, role, content, doc_ids, citations, trace)
               values (%s, 'assistant', %s, %s, %s, %s) returning id""",
            (
                thread_id,
                answer,
                doc_ids,
                json.dumps(citations),
                json.dumps({"steps": trace, "grounding": grounding}),
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return row["id"]


@router.post("/chat")
async def chat(
    body: ChatBody,
    request: Request,
    user: dict = Depends(current_user),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
):
    doc_ids = await _resolve_doc_ids(pool, user["id"], body.doc_ids)
    thread_id = await _ensure_thread(pool, user["id"], body.thread_id)
    history = await _load_history(pool, thread_id) if body.thread_id else []
    graph = request.app.state.graph

    async def event_stream():
        if not doc_ids:
            await _save_messages(
                pool,
                thread_id=thread_id,
                doc_ids=doc_ids,
                question=body.message,
                answer=NO_ANSWER_MESSAGE,
                citations=[],
                trace=[{"node": "resolve_scope", "detail": "user has no ready documents"}],
                grounding=None,
            )
            yield _sse("token", {"text": NO_ANSWER_MESSAGE})
            yield _sse("citations", [])
            yield _sse("done", {"message_id": None, "thread_id": thread_id})
            return

        initial_state = {
            "question": body.message,
            "history": history,
            "user_id": user["id"],
            "resolved_doc_ids": doc_ids,
            "rewrites": 0,
            "trace": [],
        }
        answer, citations, grounding, trace = "", [], None, []
        try:
            async for event in graph.astream(initial_state, stream_mode="custom"):
                etype = event.get("type")
                if etype == "token":
                    answer += event["text"]
                    yield _sse("token", {"text": event["text"]})
                elif etype == "step":
                    trace.append({"node": event["node"], "detail": event["detail"]})
                    yield _sse("step", {"node": event["node"], "detail": event["detail"]})
                elif etype == "citations":
                    citations = event["citations"]
                    yield _sse("citations", citations)
                elif etype == "grounding":
                    grounding = event["grounding"]
                    yield _sse("grounding", grounding)
        except QuotaExhausted:
            yield _sse(
                "error",
                {"code": "quota", "message": "The demo's free AI quota is used up for the moment. Try again in a minute."},
            )
            return
        except (TransientLLMError, PermanentError) as exc:
            logger.warning("chat graph failed: %s", exc)
            yield _sse("error", {"code": "error", "message": "Something went wrong answering that. Please try again."})
            return

        message_id = await _save_messages(
            pool,
            thread_id=thread_id,
            doc_ids=doc_ids,
            question=body.message,
            answer=answer,
            citations=citations,
            trace=trace,
            grounding=grounding,
        )
        yield _sse("done", {"message_id": message_id, "thread_id": thread_id})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/threads")
async def list_threads(user: dict = Depends(current_user), pool: AsyncConnectionPool = Depends(get_pool_dep)):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id, created_at from threads where owner_id = %s order by created_at desc", (user["id"],)
        )
        rows = await cur.fetchall()
    return [{"id": str(r["id"]), "created_at": r["created_at"].isoformat()} for r in rows]


@router.get("/threads/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str, user: dict = Depends(current_user), pool: AsyncConnectionPool = Depends(get_pool_dep)
):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id from threads where id = %s and owner_id = %s", (thread_id, user["id"])
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        cur = await conn.execute(
            """select id, role, content, citations, trace, created_at from messages
               where thread_id = %s order by created_at""",
            (thread_id,),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "citations": r["citations"],
            "trace": r["trace"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@router.delete("/threads/{thread_id}")
async def delete_thread(
    thread_id: str, user: dict = Depends(current_user), pool: AsyncConnectionPool = Depends(get_pool_dep)
):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "delete from threads where id = %s and owner_id = %s returning id", (thread_id, user["id"])
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Thread not found")
    return {"status": "ok"}
