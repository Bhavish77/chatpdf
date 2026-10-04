import logging
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_pool: AsyncConnectionPool | None = None


async def run_migrations(database_url: str) -> None:
    """Runs on a throwaway connection, not the pool. The first migration creates
    the `vector` extension; the pool's connections register the vector type
    adapter as soon as they open (see _configure), which would fail if that
    type doesn't exist in the database yet. So migrations must go first, and
    on a plain connection that doesn't try to register it.

    Idempotent: every statement in migrations/*.sql uses IF NOT EXISTS, so
    running this on every startup is safe and cheap."""
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            logger.info("applying migration %s", path.name)
            await conn.execute(sql)


async def _configure(conn: psycopg.AsyncConnection) -> None:
    await register_vector_async(conn)


def get_pool(settings: Settings, max_size: int = 5) -> AsyncConnectionPool:
    """Process-wide singleton pool. The API and the worker are separate processes,
    each calling this once with their own max_size (API 5, worker 2)."""
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            settings.DATABASE_URL,
            min_size=1,
            max_size=max_size,
            check=AsyncConnectionPool.check_connection,
            configure=_configure,
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
            open=False,
        )
    return _pool


async def open_pool(settings: Settings, max_size: int = 5) -> AsyncConnectionPool:
    pool = get_pool(settings, max_size=max_size)
    if pool.closed:
        await pool.open(wait=True)
    return pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
