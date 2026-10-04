"""Email + password auth: argon2id hashing, server-side sessions in Postgres.

# PROD: swap for a managed identity provider (Auth0, Clerk, Cognito) over OIDC,
# so you no longer own password storage, MFA, email verification or password reset.
"""

import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from psycopg_pool import AsyncConnectionPool

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
# Bounds concurrent argon2 hashing so a burst of signups/logins cannot exhaust
# memory on a 512 MB host (each hash costs ~19 MB; 2 concurrent keeps it bounded).
_hash_semaphore = asyncio.Semaphore(2)

# A valid argon2id hash of a fixed dummy password. Verifying against this for an
# unknown email makes that path cost the same time as a real verification, so
# login timing cannot be used to enumerate which emails have accounts.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-safety-only")


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def hash_password(password: str) -> str:
    async with _hash_semaphore:
        return await asyncio.to_thread(_hasher.hash, password)


def _verify_sync(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


async def verify_password(password_hash: str, password: str) -> bool:
    async with _hash_semaphore:
        return await asyncio.to_thread(_verify_sync, password_hash, password)


async def verify_dummy(password: str) -> None:
    async with _hash_semaphore:
        await asyncio.to_thread(_verify_sync, _DUMMY_HASH, password)


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_user(pool: AsyncConnectionPool, email: str, password_hash: str) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "insert into users (email, password_hash) values (%s, %s) "
            "returning id, email, created_at",
            (email, password_hash),
        )
        row = await cur.fetchone()
        assert row is not None
        return row


async def get_user_by_email(pool: AsyncConnectionPool, email: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id, email, password_hash, created_at from users where lower(email) = lower(%s)",
            (email,),
        )
        return await cur.fetchone()


async def update_password_hash(pool: AsyncConnectionPool, user_id: str, password_hash: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("update users set password_hash = %s where id = %s", (password_hash, user_id))


async def touch_last_login(pool: AsyncConnectionPool, user_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("update users set last_login_at = now() where id = %s", (user_id,))


async def create_session(
    pool: AsyncConnectionPool,
    user_id: str,
    *,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
) -> tuple[str, datetime]:
    token = new_session_token()
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    async with pool.connection() as conn:
        await conn.execute(
            """insert into auth_sessions (user_id, token_hash, expires_at, user_agent, ip)
               values (%s, %s, %s, %s, %s)""",
            (user_id, hash_token(token), expires_at, user_agent, ip),
        )
    return token, expires_at


async def lookup_session(pool: AsyncConnectionPool, token: str) -> dict | None:
    token_hash = hash_token(token)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """select u.id, u.email, u.created_at, s.id as session_id, s.last_seen_at
               from auth_sessions s join users u on u.id = s.user_id
               where s.token_hash = %s and s.expires_at > now()""",
            (token_hash,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        if row["last_seen_at"] < datetime.now(UTC) - timedelta(minutes=10):
            await conn.execute(
                "update auth_sessions set last_seen_at = now() where id = %s", (row["session_id"],)
            )
        return row


async def revoke_session(pool: AsyncConnectionPool, token: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("delete from auth_sessions where token_hash = %s", (hash_token(token),))


async def purge_expired_sessions(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("delete from auth_sessions where expires_at <= now()")
        return cur.rowcount


async def delete_inactive_accounts(pool: AsyncConnectionPool, ttl_days: int) -> int:
    """Deletes accounts with no login and no session activity for ttl_days.
    on delete cascade removes their documents, chunks, blobs, threads, messages and sessions."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """delete from users u
               where greatest(
                       coalesce(u.last_login_at, u.created_at),
                       coalesce(
                         (select max(s.last_seen_at) from auth_sessions s where s.user_id = u.id),
                         u.created_at
                       )
                     ) < now() - (%(ttl)s * interval '1 day')""",
            {"ttl": ttl_days},
        )
        return cur.rowcount


async def delete_account(pool: AsyncConnectionPool, user_id: str) -> None:
    """Deletes the user and, via on delete cascade, every document, chunk, blob,
    thread, message and session belonging to them. Jobs have no FK (their
    document id lives in jsonb payload), so they are cleaned up explicitly first."""
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("select id from documents where owner_id = %s", (user_id,))
            doc_ids = [str(row["id"]) for row in await cur.fetchall()]
            if doc_ids:
                await conn.execute(
                    "delete from jobs where kind = 'ingest_document' and payload->>'document_id' = any(%s)",
                    (doc_ids,),
                )
            await conn.execute("delete from users where id = %s", (user_id,))
