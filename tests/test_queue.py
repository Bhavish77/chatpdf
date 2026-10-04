import asyncio

from app import db, queue
from app.config import get_settings


async def _pool():
    settings = get_settings()
    return await db.open_pool(settings, max_size=5)


async def _enqueue(pool, n: int = 1) -> list[int]:
    ids = []
    for i in range(n):
        async with pool.connection() as conn:
            job_id = await queue.enqueue(conn, "ingest_document", {"document_id": f"doc-{i}"})
        ids.append(job_id)
    return ids


async def test_claim_is_exclusive_across_concurrent_workers():
    pool = await _pool()
    job_ids = set(await _enqueue(pool, n=2))

    claimed = await asyncio.gather(
        queue.claim(pool, "worker-a"),
        queue.claim(pool, "worker-b"),
    )
    assert all(j is not None for j in claimed)
    claimed_ids = {j["id"] for j in claimed}
    assert claimed_ids == job_ids  # each job claimed exactly once, between the two workers

    # No third job was left to claim.
    assert await queue.claim(pool, "worker-c") is None


async def test_fail_transient_reschedules_with_backoff():
    pool = await _pool()
    async with pool.connection() as conn:
        await queue.enqueue(conn, "ingest_document", {"document_id": "doc-x"}, max_attempts=3)
    job = await queue.claim(pool, "w1")
    assert job["attempts"] == 1

    dead_lettered = await queue.fail_transient(pool, job, "simulated transient failure")
    assert dead_lettered is False

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select status, run_after > now() as in_future from jobs where id = %s", (job["id"],)
        )
        row = await cur.fetchone()
    assert row["status"] == "queued"
    assert row["in_future"] is True


async def test_fail_transient_dead_letters_after_max_attempts():
    pool = await _pool()
    async with pool.connection() as conn:
        await queue.enqueue(conn, "ingest_document", {"document_id": "doc-y"}, max_attempts=1)
    job = await queue.claim(pool, "w1")
    assert job["attempts"] == 1 == job["max_attempts"]

    dead_lettered = await queue.fail_transient(pool, job, "out of retries")
    assert dead_lettered is True

    async with pool.connection() as conn:
        cur = await conn.execute("select status, last_error from jobs where id = %s", (job["id"],))
        row = await cur.fetchone()
    assert row["status"] == "failed"
    assert row["last_error"] == "out of retries"


async def test_reclaim_stale_requeues_jobs_under_max_attempts():
    pool = await _pool()
    async with pool.connection() as conn:
        await queue.enqueue(conn, "ingest_document", {"document_id": "doc-z"}, max_attempts=5)
    job = await queue.claim(pool, "w1")

    async with pool.connection() as conn:
        await conn.execute(
            "update jobs set locked_at = now() - interval '10 minutes' where id = %s", (job["id"],)
        )

    touched = await queue.reclaim_stale(pool)
    assert touched == 1

    async with pool.connection() as conn:
        cur = await conn.execute("select status, locked_by from jobs where id = %s", (job["id"],))
        row = await cur.fetchone()
    assert row["status"] == "queued"
    assert row["locked_by"] is None


async def test_reclaim_stale_dead_letters_jobs_over_max_attempts():
    pool = await _pool()
    async with pool.connection() as conn:
        await queue.enqueue(conn, "ingest_document", {"document_id": "doc-w"}, max_attempts=1)
    job = await queue.claim(pool, "w1")
    assert job["attempts"] >= job["max_attempts"]

    async with pool.connection() as conn:
        await conn.execute(
            "update jobs set locked_at = now() - interval '10 minutes' where id = %s", (job["id"],)
        )

    touched = await queue.reclaim_stale(pool)
    assert touched == 1

    async with pool.connection() as conn:
        cur = await conn.execute("select status from jobs where id = %s", (job["id"],))
        row = await cur.fetchone()
    assert row["status"] == "failed"
