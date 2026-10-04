"""Public, unauthenticated endpoints."""

from fastapi import APIRouter, Depends, Request
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.deps import get_pool_dep, get_settings_dep

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/config")
async def get_config(settings: Settings = Depends(get_settings_dep)) -> dict:
    return {
        "max_file_mb": settings.MAX_FILE_MB,
        "allowed_types": ["pdf", "docx", "txt", "md"],
        "doc_ttl_days": settings.DOC_TTL_DAYS,
        "account_ttl_days": settings.ACCOUNT_TTL_DAYS,
        "models": {
            "generation": settings.GEN_MODEL,
            "helper": settings.HELPER_MODEL,
            "embedding": settings.EMBED_MODEL,
        },
        "privacy_notice": (
            "Demo app. Use a throwaway password. Uploaded content is sent to Google's free "
            "Gemini API, which may use it to improve Google products. Data is deleted after "
            f"{settings.DOC_TTL_DAYS} days; accounts after {settings.ACCOUNT_TTL_DAYS} days "
            "of inactivity."
        ),
        "signups_enabled": settings.SIGNUPS_ENABLED,
        "invite_required": bool(settings.SIGNUP_INVITE_CODE),
    }


@router.get("/health")
async def health(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    pool: AsyncConnectionPool = Depends(get_pool_dep),
) -> dict:
    db_ok = True
    try:
        async with pool.connection() as conn:
            await conn.execute("select 1")
    except Exception:
        db_ok = False
    return {
        "db": "ok" if db_ok else "error",
        "gemini_key": "present" if settings.GEMINI_API_KEY else "missing",
        "worker": getattr(request.app.state, "worker_last_seen", None),
        "models": {
            "generation": settings.GEN_MODEL,
            "helper": settings.HELPER_MODEL,
            "embedding": settings.EMBED_MODEL,
        },
    }
