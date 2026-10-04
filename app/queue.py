"""Hand-written Postgres job queue using FOR UPDATE SKIP LOCKED: enqueue, claim,
heartbeat, complete, fail (with retry/backoff or dead-letter), and stale reclaim.

# PROD: SQS, Pub/Sub or Cloud Tasks with autoscaled workers (scale on queue
depth), or Celery/RQ/Redis Streams; Temporal/Step Functions for durable
multi-step workflows; dead-letter queues, alarms, idempotency keys.
"""

import asyncio
import json
import logging

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

STALE_AFTER_S = 5 * 60
BASE_BACKOFF_S = 15

_wake_event: asyncio.Event | None = None


def get_wake_event() -> asyncio.Event:
    """Only meaningful when the worker runs inside the API process
    (EMBEDDED_WORKER=true): lets enqueue() wake it immediately instead of
    waiting out the idle poll interval. A separate worker process has its own
    module instance of this and just polls on its own schedule."""
    global _wake_event
    if _wake_event is None:
        _wake_event = asyncio.Event()
    return _wake_event


async def enqueue(conn: AsyncConnection, kind: str, payload: dict, *, max_attempts: int = 3) -> int:
    """Caller passes an open connection so this can run in the same transaction
    as the row(s) the job depends on (e.g. the document insert)."""
    cur = await conn.execute(
        "insert into jobs (kind, payload, max_attempts) values (%s, %s, %s) returning id",
        (kind, json.dumps(payload), max_attempts),
    )
    row = await cur.fetchone()
    assert row is not None
    get_wake_event().set()
    return row["id"]


async def claim(pool: AsyncConnectionPool, worker_name: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """update jobs set status = 'running', locked_at = now(), locked_by = %(worker)s,
                              attempts = attempts + 1, updated_at = now()
               where id = (
                 select id from jobs
                 where status = 'queued' and run_after <= now()
                 order by id for update skip locked limit 1
               )
               returning *""",
            {"worker": worker_name},
        )
        return await cur.fetchone()


async def heartbeat(pool: AsyncConnectionPool, job_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute("update jobs set locked_at = now() where id = %s", (job_id,))


async def complete(pool: AsyncConnectionPool, job_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "update jobs set status = 'succeeded', updated_at = now() where id = %s", (job_id,)
        )


async def fail_permanent(pool: AsyncConnectionPool, job_id: int, error: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "update jobs set status = 'failed', last_error = %s, updated_at = now() where id = %s",
            (error[:2000], job_id),
        )


async def fail_transient(pool: AsyncConnectionPool, job: dict, error: str) -> bool:
    """Reschedules with jittered exponential backoff (15s * 4^(attempts-1)), or
    dead-letters if max_attempts is exhausted. Returns True if dead-lettered."""
    if job["attempts"] >= job["max_attempts"]:
        await fail_permanent(pool, job["id"], error)
        return True
    delay_s = BASE_BACKOFF_S * (4 ** (job["attempts"] - 1))
    async with pool.connection() as conn:
        await conn.execute(
            """update jobs set status = 'queued', run_after = now() + %(delay)s * interval '1 second',
                              locked_at = null, locked_by = null, last_error = %(error)s, updated_at = now()
               where id = %(id)s""",
            {"delay": delay_s, "error": error[:2000], "id": job["id"]},
        )
    return False


async def reclaim_stale(pool: AsyncConnectionPool) -> int:
    """Running jobs whose locked_at is older than STALE_AFTER_S go back to
    queued, unless they've exhausted attempts, in which case they're failed.
    Makes jobs crash-safe, including when the host spins the container down
    mid-job. Returns the number of jobs touched."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """update jobs set status = 'failed', last_error = 'stale: exceeded max attempts',
                              updated_at = now()
               where status = 'running' and locked_at < now() - %(stale)s * interval '1 second'
                 and attempts >= max_attempts
               returning id""",
            {"stale": STALE_AFTER_S},
        )
        failed = len(await cur.fetchall())
        cur = await conn.execute(
            """update jobs set status = 'queued', locked_at = null, locked_by = null, updated_at = now()
               where status = 'running' and locked_at < now() - %(stale)s * interval '1 second'
               returning id""",
            {"stale": STALE_AFTER_S},
        )
        reclaimed = len(await cur.fetchall())
    if reclaimed or failed:
        logger.info("reclaimed %s stale job(s), dead-lettered %s", reclaimed, failed)
    return reclaimed + failed


async def vacuum_finished(pool: AsyncConnectionPool, older_than_hours: int = 24) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """delete from jobs where status in ('succeeded', 'failed')
               and updated_at < now() - %(hours)s * interval '1 hour'
               returning id""",
            {"hours": older_than_hours},
        )
        return len(await cur.fetchall())
