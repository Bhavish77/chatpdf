from app import db
from app.config import get_settings


async def test_migrations_are_idempotent():
    settings = get_settings()
    pool = await db.open_pool(settings, max_size=2)
    await db.run_migrations(pool)
    await db.run_migrations(pool)  # must not raise on a second application

    async with pool.connection() as conn:
        cur = await conn.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        )
        tables = {row["table_name"] for row in await cur.fetchall()}

    assert {"users", "auth_sessions", "documents", "blobs", "chunks", "jobs", "threads", "messages"} <= tables
