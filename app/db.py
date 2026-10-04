import logging
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_pool: AsyncConnectionPool | None = None


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


async def run_migrations(pool: AsyncConnectionPool) -> None:
    """Idempotent: every statement in migrations/*.sql uses IF NOT EXISTS, so
    running this on every startup is safe and cheap."""
    async with pool.connection() as conn:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            logger.info("applying migration %s", path.name)
            await conn.execute(sql)
