import asyncio
import contextlib
import os
import sys

if sys.platform == "win32":
    # psycopg's async waiting needs loop.add_reader/add_writer, which the default
    # ProactorEventLoop on Windows does not implement (it hangs instead of raising).
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

os.environ.setdefault("DATABASE_URL", "postgresql://askdocs:askdocs@localhost:5544/askdocs_test")
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("SIGNUPS_ENABLED", "true")
os.environ.setdefault("SIGNUP_INVITE_CODE", "")
os.environ.setdefault("LOGIN_MAX_FAILURES", "3")
os.environ.setdefault("SIGNUP_RATE_PER_HOUR", "1000")
os.environ.setdefault("SESSION_TTL_DAYS", "7")
os.environ.setdefault("LOG_LEVEL", "INFO")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import auth as auth_routes  # noqa: E402

CSRF_HEADERS = {"X-Requested-With": "askdocs"}

_TABLES = "messages, threads, jobs, chunks, blobs, documents, auth_sessions, users"


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    settings = get_settings()
    asyncio.run(db.run_migrations(settings.DATABASE_URL))
    yield


@pytest.fixture(autouse=True)
def _clean_state(_migrated):
    settings = get_settings()
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"truncate table {_TABLES} cascade")
    auth_routes._signup_rate.clear()
    auth_routes._login_throttle_cache.clear()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def pool():
    """A DB pool opened and closed within this test's own event loop.

    Tests that need the pool directly (not through the `client` fixture)
    must use this instead of calling db.open_pool() themselves: psycopg's
    async pool is bound to the loop it was opened on, and pytest-asyncio
    gives each async test its own loop. A pool left open past its test (or
    opened on one test's loop and later closed by a *different* test's
    TestClient lifespan) causes exactly the cross-loop hangs/CancelledErrors
    this fixture exists to avoid.
    """
    settings = get_settings()
    p = await db.open_pool(settings, max_size=5)
    yield p
    await db.close_pool()


async def insert_document(
    pool,
    *,
    owner_id=None,
    filename: str = "test.pdf",
    mime: str = "application/pdf",
    size_bytes: int = 10,
) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """insert into documents (owner_id, filename, mime, size_bytes, sha256)
               values (%s, %s, %s, %s, 'x') returning id""",
            (owner_id, filename, mime, size_bytes),
        )
        row = await cur.fetchone()
        return str(row["id"])


@contextlib.contextmanager
def override_env(**kwargs):
    """Temporarily set env vars and bust the Settings cache, restoring both after."""
    old = {k: os.environ.get(k) for k in kwargs}
    os.environ.update({k: str(v) for k, v in kwargs.items()})
    get_settings.cache_clear()
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()
