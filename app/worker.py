"""Background worker: claims jobs from the queue, runs ingestion, and runs
periodic maintenance (stale-job reclaim, session/account purge, finished-job
vacuum). `python -m app.worker` runs this standalone; EMBEDDED_WORKER=true
runs the same run_worker() as a background asyncio task inside the API process.
"""

import asyncio
import logging
import signal
import sys
import uuid
from collections.abc import Callable

if sys.platform == "win32":
    # psycopg's async waiting needs loop.add_reader/add_writer, which the default
    # ProactorEventLoop on Windows does not implement. Only matters running outside
    # Docker on a Windows dev machine; the Linux container is unaffected.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app import auth, db, queue
from app.config import Settings, get_settings
from app.ingestion.pipeline import PermanentIngestError, run_ingestion
from app.llm import GeminiClient
from app.logutil import configure_logging
from app.storage import PostgresBlobStore
from app.vectorindex import PgVectorIndex

logger = logging.getLogger(__name__)

POLL_BUSY_S = 2
POLL_IDLE_MAX_S = 30
MAINTENANCE_INTERVAL_S = 120


async def _mark_document_failed(pool, document_id: str, error: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "update documents set status = 'failed', error = %s, updated_at = now() where id = %s",
            (error[:500], document_id),
        )


async def _heartbeat_loop(pool, job_id: int) -> None:
    while True:
        await asyncio.sleep(30)
        await queue.heartbeat(pool, job_id)


async def _run_job(pool, llm, blobs, vectors, settings, job: dict) -> None:
    heartbeat_task = asyncio.create_task(_heartbeat_loop(pool, job["id"]))
    try:
        if job["kind"] == "ingest_document":
            await run_ingestion(pool, llm, blobs, vectors, settings, job["payload"])
        else:
            raise PermanentIngestError(f"Unknown job kind: {job['kind']}")
        await queue.complete(pool, job["id"])
    except PermanentIngestError as exc:
        logger.warning("job %s failed permanently: %s", job["id"], exc)
        await queue.fail_permanent(pool, job["id"], str(exc))
        await _mark_document_failed(pool, job["payload"]["document_id"], str(exc))
    except Exception as exc:  # noqa: BLE001 - anything else is treated as transient
        logger.warning("job %s failed transiently: %s", job["id"], exc)
        dead_lettered = await queue.fail_transient(pool, job, str(exc))
        if dead_lettered:
            await _mark_document_failed(
                pool, job["payload"]["document_id"], f"Failed after repeated retries: {exc}"
            )
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


async def _claim_loop(
    pool,
    llm,
    blobs,
    vectors,
    settings: Settings,
    stop_event: asyncio.Event,
    name: str,
    on_tick: Callable[[], None] | None,
) -> None:
    wake = queue.get_wake_event()
    poll_interval = POLL_BUSY_S
    while not stop_event.is_set():
        job = await queue.claim(pool, name)
        if on_tick:
            on_tick()
        if job is None:
            # Poll quickly for a while after work was last seen, backing off
            # toward POLL_IDLE_MAX_S the longer nothing shows up.
            poll_interval = min(poll_interval * 2, POLL_IDLE_MAX_S)
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=poll_interval)
            except TimeoutError:
                pass
            continue
        poll_interval = POLL_BUSY_S
        await _run_job(pool, llm, blobs, vectors, settings, job)


async def _maintenance_loop(pool, settings: Settings, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await queue.reclaim_stale(pool)
            await auth.purge_expired_sessions(pool)
            await auth.delete_inactive_accounts(pool, settings.ACCOUNT_TTL_DAYS)
            await queue.vacuum_finished(pool)
            # document TTL sweep is added in Phase 5, once documents.py exists.
        except Exception:
            logger.exception("maintenance loop iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=MAINTENANCE_INTERVAL_S)
        except TimeoutError:
            pass


async def run_worker(
    stop_event: asyncio.Event,
    settings: Settings | None = None,
    on_tick: Callable[[], None] | None = None,
) -> None:
    settings = settings or get_settings()
    pool = await db.open_pool(settings, max_size=settings.WORKER_CONCURRENCY + 1)
    worker_name = f"worker-{uuid.uuid4().hex[:8]}"
    llm = GeminiClient(settings)
    blobs = PostgresBlobStore(pool)
    vectors = PgVectorIndex(pool)

    logger.info("worker %s starting with concurrency %s", worker_name, settings.WORKER_CONCURRENCY)
    maintenance_task = asyncio.create_task(_maintenance_loop(pool, settings, stop_event))
    claim_tasks = [
        asyncio.create_task(
            _claim_loop(pool, llm, blobs, vectors, settings, stop_event, f"{worker_name}-{i}", on_tick)
        )
        for i in range(settings.WORKER_CONCURRENCY)
    ]
    try:
        await asyncio.gather(*claim_tasks)
    finally:
        maintenance_task.cancel()
        await asyncio.gather(maintenance_task, return_exceptions=True)
        logger.info("worker %s stopped", worker_name)


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    await db.run_migrations(settings.DATABASE_URL)
    stop_event = asyncio.Event()
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)
    await run_worker(stop_event, settings)


if __name__ == "__main__":
    asyncio.run(_main())
