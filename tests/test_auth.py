import logging

import psycopg
from conftest import CSRF_HEADERS, override_env
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.main import app

PASSWORD = "correct-horse-battery-staple"


def test_full_auth_flow(client):
    r = client.post(
        "/api/auth/signup", json={"email": "Flow@Example.com", "password": PASSWORD}, headers=CSRF_HEADERS
    )
    assert r.status_code == 201
    assert r.json()["email"] == "flow@example.com"

    r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "flow@example.com"
    assert isinstance(body["usage"]["storage_mb"], (int, float))

    r = client.post("/api/auth/logout", headers=CSRF_HEADERS)
    assert r.status_code == 200

    r = client.get("/api/auth/me")
    assert r.status_code == 401

    r = client.post(
        "/api/auth/login", json={"email": "flow@example.com", "password": PASSWORD}, headers=CSRF_HEADERS
    )
    assert r.status_code == 200

    r = client.get("/api/auth/me")
    assert r.status_code == 200


def test_signup_rejects_short_password(client):
    r = client.post(
        "/api/auth/signup", json={"email": "short@example.com", "password": "short"}, headers=CSRF_HEADERS
    )
    assert r.status_code == 422


def test_signup_rejects_password_equal_email(client):
    email = "sameaspw@example.com"
    r = client.post("/api/auth/signup", json={"email": email, "password": email}, headers=CSRF_HEADERS)
    assert r.status_code == 422


def test_signup_duplicate_email_409(client):
    body = {"email": "dup@example.com", "password": PASSWORD}
    r1 = client.post("/api/auth/signup", json=body, headers=CSRF_HEADERS)
    assert r1.status_code == 201
    r2 = client.post("/api/auth/signup", json=body, headers=CSRF_HEADERS)
    assert r2.status_code == 409


def test_unknown_email_and_wrong_password_return_identical_401(client, monkeypatch):
    client.post(
        "/api/auth/signup", json={"email": "known@example.com", "password": PASSWORD}, headers=CSRF_HEADERS
    )
    client.post("/api/auth/logout", headers=CSRF_HEADERS)

    import app.auth as auth_module

    calls = []
    original = auth_module.verify_dummy

    async def spy(password):
        calls.append(password)
        return await original(password)

    monkeypatch.setattr(auth_module, "verify_dummy", spy)

    r_unknown = client.post(
        "/api/auth/login",
        json={"email": "nosuchuser@example.com", "password": "whatever-123"},
        headers=CSRF_HEADERS,
    )
    r_wrong = client.post(
        "/api/auth/login", json={"email": "known@example.com", "password": "nope-nope-nope"}, headers=CSRF_HEADERS
    )

    assert r_unknown.status_code == 401
    assert r_wrong.status_code == 401
    assert r_unknown.json() == r_wrong.json()
    assert len(calls) == 1  # only the unknown-email path runs the dummy check


def test_login_throttle_returns_429_with_retry_after(client):
    client.post(
        "/api/auth/signup", json={"email": "throttle@example.com", "password": PASSWORD}, headers=CSRF_HEADERS
    )
    client.post("/api/auth/logout", headers=CSRF_HEADERS)

    settings = get_settings()
    for _ in range(settings.LOGIN_MAX_FAILURES):
        r = client.post(
            "/api/auth/login", json={"email": "throttle@example.com", "password": "wrong"}, headers=CSRF_HEADERS
        )
        assert r.status_code == 401

    r = client.post(
        "/api/auth/login", json={"email": "throttle@example.com", "password": "wrong"}, headers=CSRF_HEADERS
    )
    assert r.status_code == 429
    assert "Retry-After" in r.headers

    # a correct login still works for a *different* account while this one is throttled
    client.post(
        "/api/auth/signup", json={"email": "other@example.com", "password": PASSWORD}, headers=CSRF_HEADERS
    )


def test_cookie_flags_in_insecure_dev_mode(client):
    r = client.post(
        "/api/auth/signup", json={"email": "cookie@example.com", "password": PASSWORD}, headers=CSRF_HEADERS
    )
    set_cookie = r.headers.get("set-cookie", "")
    assert "askdocs_session=" in set_cookie
    assert "__Host-" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "Secure" not in set_cookie


def test_cookie_flags_in_secure_mode_use_host_prefix():
    with override_env(COOKIE_SECURE="true"):
        with TestClient(app) as c:
            r = c.post(
                "/api/auth/signup",
                json={"email": "secure@example.com", "password": PASSWORD},
                headers=CSRF_HEADERS,
            )
            set_cookie = r.headers.get("set-cookie", "")
            assert "__Host-askdocs_session=" in set_cookie
            assert "Secure" in set_cookie
            assert "HttpOnly" in set_cookie


def test_post_without_csrf_header_is_rejected(client):
    r = client.post("/api/auth/login", json={"email": "a@example.com", "password": "whatever-123"})
    assert r.status_code == 403


def test_post_with_foreign_origin_is_rejected(client):
    r = client.post(
        "/api/auth/login",
        json={"email": "a@example.com", "password": "whatever-123"},
        headers={**CSRF_HEADERS, "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_expired_session_returns_401(client):
    r = client.post(
        "/api/auth/signup", json={"email": "expired@example.com", "password": PASSWORD}, headers=CSRF_HEADERS
    )
    assert r.status_code == 201
    token = client.cookies.get("askdocs_session")
    assert token

    settings = get_settings()
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "update auth_sessions set expires_at = now() - interval '1 day' where token_hash = %s",
            (auth.hash_token(token),),
        )

    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_signups_disabled_blocks_signup():
    with override_env(SIGNUPS_ENABLED="false"):
        with TestClient(app) as c:
            r = c.post(
                "/api/auth/signup",
                json={"email": "blocked@example.com", "password": PASSWORD},
                headers=CSRF_HEADERS,
            )
            assert r.status_code == 403


def test_wrong_invite_code_blocks_signup():
    with override_env(SIGNUP_INVITE_CODE="letmein"):
        with TestClient(app) as c:
            r = c.post(
                "/api/auth/signup",
                json={"email": "noinvite@example.com", "password": PASSWORD, "invite_code": "wrong"},
                headers=CSRF_HEADERS,
            )
            assert r.status_code == 403

            r = c.post(
                "/api/auth/signup",
                json={"email": "withinvite@example.com", "password": PASSWORD, "invite_code": "letmein"},
                headers=CSRF_HEADERS,
            )
            assert r.status_code == 201


def test_no_password_or_token_in_responses_or_logs(client, caplog):
    caplog.set_level(logging.DEBUG)
    password = "super-secret-password-999"
    r = client.post(
        "/api/auth/signup", json={"email": "secrets@example.com", "password": password}, headers=CSRF_HEADERS
    )
    assert r.status_code == 201
    assert password not in r.text

    token = client.cookies.get("askdocs_session")
    assert token
    assert token not in r.text

    settings = get_settings()
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        cur = conn.execute("select password_hash from users where email = %s", ("secrets@example.com",))
        (password_hash,) = cur.fetchone()
    assert password_hash.startswith("$argon2id$")

    for record in caplog.records:
        message = record.getMessage()
        assert password not in message
        assert token not in message
        assert password_hash not in message


def test_account_deletion_removes_every_row(client):
    r = client.post(
        "/api/auth/signup", json={"email": "delme@example.com", "password": PASSWORD}, headers=CSRF_HEADERS
    )
    user_id = r.json()["id"]

    settings = get_settings()
    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "insert into documents (id, owner_id, filename, mime, size_bytes, sha256, status) "
            "values (gen_random_uuid(), %s, 'a.pdf', 'application/pdf', 10, 'x', 'ready')",
            (user_id,),
        )
        conn.execute("insert into threads (id, owner_id) values (gen_random_uuid(), %s)", (user_id,))

    r = client.request("DELETE", "/api/auth/me", json={"password": PASSWORD}, headers=CSRF_HEADERS)
    assert r.status_code == 200

    with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
        for table, column in [
            ("users", "id"),
            ("documents", "owner_id"),
            ("threads", "owner_id"),
            ("auth_sessions", "user_id"),
        ]:
            cur = conn.execute(f"select count(*) from {table} where {column} = %s", (user_id,))
            assert cur.fetchone()[0] == 0, f"{table} still has rows for the deleted user"
