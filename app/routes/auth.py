"""Signup, login, logout, me. See BUILD_SPEC.md section 6.2 for the exact rules."""

import logging
import secrets

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app import auth
from app.config import Settings
from app.deps import client_ip, current_user, get_pool_dep, get_settings_dep
from app.logutil import mask_email
from app.ratelimit import FixedWindowCounter, LoginThrottle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Process-local, in-memory. # PROD: Redis-backed so limits hold across replicas.
_signup_rate = FixedWindowCounter(window_seconds=3600)
_login_throttle_cache: dict[int, LoginThrottle] = {}


def _login_throttle(settings: Settings) -> LoginThrottle:
    throttle = _login_throttle_cache.get(settings.LOGIN_MAX_FAILURES)
    if throttle is None:
        throttle = LoginThrottle(max_failures=settings.LOGIN_MAX_FAILURES)
        _login_throttle_cache[settings.LOGIN_MAX_FAILURES] = throttle
    return throttle


class SignupBody(BaseModel):
    email: str
    password: str
    invite_code: str | None = None


class LoginBody(BaseModel):
    email: str
    password: str


class DeleteMeBody(BaseModel):
    password: str


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.SESSION_TTL_DAYS * 86400,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(key=settings.session_cookie_name, path="/")


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupBody,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings_dep),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
) -> dict:
    if not settings.SIGNUPS_ENABLED:
        raise HTTPException(status_code=403, detail="Signups are closed")

    ip = client_ip(request)
    signup_key = f"signup:{ip}"
    if _signup_rate.increment(signup_key) > settings.SIGNUP_RATE_PER_HOUR:
        retry_after = _signup_rate.retry_after(signup_key) or 3600
        raise HTTPException(
            status_code=429,
            detail="Too many signups from this address, try again later",
            headers={"Retry-After": str(retry_after)},
        )

    if settings.SIGNUP_INVITE_CODE and not secrets.compare_digest(
        body.invite_code or "", settings.SIGNUP_INVITE_CODE
    ):
        raise HTTPException(status_code=403, detail="Invalid invite code")

    email = auth.normalize_email(body.email)
    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError:
        raise HTTPException(status_code=422, detail="Invalid email address") from None

    if not (10 <= len(body.password) <= 128):
        raise HTTPException(status_code=422, detail="Password must be 10 to 128 characters")
    if body.password == email:
        raise HTTPException(status_code=422, detail="Password must not equal the email")

    if await auth.get_user_by_email(pool, email) is not None:
        # Leaks account existence; accepted limitation without email verification (see README).
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    password_hash = await auth.hash_password(body.password)
    user = await auth.create_user(pool, email, password_hash)
    token, _ = await auth.create_session(
        pool,
        user["id"],
        ttl_days=settings.SESSION_TTL_DAYS,
        user_agent=request.headers.get("user-agent"),
        ip=ip,
    )
    _set_session_cookie(response, settings, token)
    logger.info("signup success email=%s", mask_email(email))
    return {"id": str(user["id"]), "email": user["email"]}


@router.post("/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings_dep),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
) -> dict:
    ip = client_ip(request)
    email = auth.normalize_email(body.email)
    throttle = _login_throttle(settings)

    retry_after = throttle.retry_after(email, ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts, try again later",
            headers={"Retry-After": str(retry_after)},
        )

    user = await auth.get_user_by_email(pool, email)
    if user is None:
        # Still hash-verify against a dummy so timing can't reveal that this email is unknown.
        await auth.verify_dummy(body.password)
        throttle.record_failure(email, ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not await auth.verify_password(user["password_hash"], body.password):
        throttle.record_failure(email, ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    throttle.reset(email, ip)

    if auth.needs_rehash(user["password_hash"]):
        await auth.update_password_hash(pool, user["id"], await auth.hash_password(body.password))

    await auth.touch_last_login(pool, user["id"])
    token, _ = await auth.create_session(
        pool,
        user["id"],
        ttl_days=settings.SESSION_TTL_DAYS,
        user_agent=request.headers.get("user-agent"),
        ip=ip,
    )
    _set_session_cookie(response, settings, token)
    logger.info("login success email=%s", mask_email(email))
    return {"id": str(user["id"]), "email": user["email"]}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings_dep),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
    user: dict = Depends(current_user),
) -> dict:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await auth.revoke_session(pool, token)
    _clear_session_cookie(response, settings)
    return {"status": "ok"}


@router.get("/me")
async def me(
    user: dict = Depends(current_user),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """select
                 (select count(*) from documents where owner_id = %(uid)s) as docs,
                 (select coalesce(sum(size_bytes), 0) from documents where owner_id = %(uid)s)
                   as storage_bytes,
                 (select count(*) from messages m join threads t on t.id = m.thread_id
                    where t.owner_id = %(uid)s and m.role = 'user'
                      and m.created_at >= date_trunc('day', now())) as chats_today
            """,
            {"uid": user["id"]},
        )
        row = await cur.fetchone()
    return {
        "id": str(user["id"]),
        "email": user["email"],
        "created_at": user["created_at"].isoformat(),
        "usage": {
            "docs": row["docs"],
            "storage_mb": round(float(row["storage_bytes"]) / 1_000_000, 2),
            "chats_today": row["chats_today"],
        },
        "limits": {
            "max_docs": settings.MAX_DOCS_PER_USER,
            "max_storage_mb": settings.MAX_USER_STORAGE_MB,
            "chat_per_day": settings.USER_CHAT_PER_DAY,
        },
    }


@router.delete("/me")
async def delete_me(
    body: DeleteMeBody,
    response: Response,
    user: dict = Depends(current_user),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    full_user = await auth.get_user_by_email(pool, user["email"])
    if full_user is None or not await auth.verify_password(full_user["password_hash"], body.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    await auth.delete_account(pool, user["id"])
    _clear_session_cookie(response, settings)
    return {"status": "ok"}
