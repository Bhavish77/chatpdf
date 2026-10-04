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

    async def _run() -> None:
        pool = await db.open_pool(settings, max_size=2)
        await db.run_migrations(pool)
        await db.close_pool()

    asyncio.run(_run())
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
