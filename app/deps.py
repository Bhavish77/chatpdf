"""Shared FastAPI dependencies: current_user, CSRF enforcement, client IP."""

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from starlette.responses import Response

from app import auth
from app.config import Settings

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_pool_dep(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


def client_ip(request: Request) -> str:
    settings: Settings = request.app.state.settings
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def current_user(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    pool: AsyncConnectionPool = request.app.state.pool
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    session = await auth.lookup_session(pool, token)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return session


async def csrf_middleware(request: Request, call_next) -> Response:
    """Every non-GET/HEAD/OPTIONS request (login and signup included) must carry
    X-Requested-With, and CORS is never enabled, so another site cannot send it.
    If an Origin header is present it must match our own origin."""
    if request.method not in _SAFE_METHODS:
        if request.headers.get("x-requested-with") != "askdocs":
            return JSONResponse({"detail": "Missing required header"}, status_code=403)
        settings: Settings = request.app.state.settings
        origin = request.headers.get("origin")
        if origin:
            expected = settings.PUBLIC_ORIGIN or f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin.rstrip("/") != expected.rstrip("/"):
                return JSONResponse({"detail": "Origin mismatch"}, status_code=403)
    return await call_next(request)


_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


async def security_headers_middleware(request: Request, call_next) -> Response:
    """No `sandbox` directive here (it would break the browser's built-in PDF
    viewer on the file-serving route - see BUILD_SPEC.md 6.7), and these are
    safe to send on every response, not just HTML ones."""
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response
