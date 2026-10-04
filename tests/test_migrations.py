import psycopg

from app import db
from app.config import get_settings


async def test_migrations_are_idempotent():
    settings = get_settings()
    await db.run_migrations(settings.DATABASE_URL)  # second application must not raise

    async with await psycopg.AsyncConnection.connect(settings.DATABASE_URL) as conn:
        cur = await conn.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        )
        tables = {row[0] for row in await cur.fetchall()}

    assert {"users", "auth_sessions", "documents", "blobs", "chunks", "jobs", "threads", "messages"} <= tables
