"""Phase 5 hardening: rate limits, retention sweeps, response headers.
See BUILD_SPEC.md section 6.7."""

import datetime

from conftest import CSRF_HEADERS, insert_document, override_env
from fastapi.testclient import TestClient
from pdf_fixtures import make_pdf_bytes

from app import auth, worker
from app.config import get_settings
from app.main import app

PASSWORD = "correct-horse-battery-staple"


def _signup(client, email):
    r = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD}, headers=CSRF_HEADERS)
    assert r.status_code == 201
    return r.json()["id"]


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "cdnjs.cloudflare.com" in csp
    assert "frame-ancestors 'none'" in csp
    assert "sandbox" not in csp


def test_chat_rate_limit_returns_429_with_retry_after():
    with override_env(CHAT_RATE_PER_MIN="2"), TestClient(app) as c:
        _signup(c, "chatrate@example.com")
        for _ in range(2):
            r = c.post("/api/chat", json={"message": "hi"}, headers=CSRF_HEADERS)
            assert r.status_code == 200
        r = c.post("/api/chat", json={"message": "hi"}, headers=CSRF_HEADERS)
        assert r.status_code == 429
        assert "Retry-After" in r.headers


def test_chat_daily_quota_blocks_further_messages():
    with override_env(USER_CHAT_PER_DAY="1"), TestClient(app) as c:
        _signup(c, "chatquota@example.com")
        r = c.post("/api/chat", json={"message": "hi"}, headers=CSRF_HEADERS)
        assert r.status_code == 200
        r = c.post("/api/chat", json={"message": "hi again"}, headers=CSRF_HEADERS)
        assert r.status_code == 429


def test_upload_rate_limit_returns_429_with_retry_after():
    with override_env(UPLOAD_RATE_PER_HOUR="1"), TestClient(app) as c:
        _signup(c, "uploadrate@example.com")
        pdf = make_pdf_bytes(["first upload"])
        r = c.post("/api/documents", files=[("files", ("a.pdf", pdf, "application/pdf"))], headers=CSRF_HEADERS)
        assert r.status_code == 202
        r = c.post("/api/documents", files=[("files", ("b.pdf", pdf, "application/pdf"))], headers=CSRF_HEADERS)
        assert r.status_code == 429
        assert "Retry-After" in r.headers


def test_signup_rate_limit_returns_429():
    with override_env(SIGNUP_RATE_PER_HOUR="2"), TestClient(app) as c:
        for i in range(2):
            r = c.post(
                "/api/auth/signup",
                json={"email": f"sigrate{i}@example.com", "password": PASSWORD},
                headers=CSRF_HEADERS,
            )
            assert r.status_code == 201
        r = c.post(
            "/api/auth/signup", json={"email": "sigrate-over@example.com", "password": PASSWORD}, headers=CSRF_HEADERS
        )
        assert r.status_code == 429


async def test_document_ttl_sweep_removes_expired_documents(pool):
    doc_id = await insert_document(pool, filename="old.pdf", mime="application/pdf")
    async with pool.connection() as conn:
        await conn.execute(
            "update documents set expires_at = now() - interval '1 day' where id = %s", (doc_id,)
        )

    touched = await worker._purge_expired_documents(pool)
    assert touched == 1

    async with pool.connection() as conn:
        cur = await conn.execute("select count(*) from documents where id = %s", (doc_id,))
        assert (await cur.fetchone())["count"] == 0


async def test_document_ttl_sweep_keeps_unexpired_documents(pool):
    doc_id = await insert_document(pool, filename="fresh.pdf", mime="application/pdf")
    async with pool.connection() as conn:
        await conn.execute(
            "update documents set expires_at = now() + interval '7 days' where id = %s", (doc_id,)
        )

    await worker._purge_expired_documents(pool)

    async with pool.connection() as conn:
        cur = await conn.execute("select count(*) from documents where id = %s", (doc_id,))
        assert (await cur.fetchone())["count"] == 1


async def test_inactive_account_purge_removes_dormant_accounts(pool):
    settings = get_settings()
    password_hash = await auth.hash_password(PASSWORD)
    user = await auth.create_user(pool, "dormant@example.com", password_hash)

    long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=settings.ACCOUNT_TTL_DAYS + 5)
    async with pool.connection() as conn:
        await conn.execute(
            "update users set created_at = %s, last_login_at = null where id = %s", (long_ago, user["id"])
        )

    touched = await auth.delete_inactive_accounts(pool, settings.ACCOUNT_TTL_DAYS)
    assert touched >= 1

    async with pool.connection() as conn:
        cur = await conn.execute("select count(*) from users where id = %s", (user["id"],))
        assert (await cur.fetchone())["count"] == 0
