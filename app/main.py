"""FastAPI app: lifespan (pool + migrations), middleware, route registration."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

if sys.platform == "win32":
    # psycopg's async waiting needs loop.add_reader/add_writer, which the default
    # ProactorEventLoop on Windows does not implement. Only matters running outside
    # Docker on a Windows dev machine; the Linux container is unaffected.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app import db
from app.config import get_settings
from app.deps import csrf_middleware
from app.logutil import configure_logging, request_id_middleware
from app.routes import auth as auth_routes
from app.routes import meta as meta_routes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    app.state.settings = settings
    pool = await db.open_pool(settings, max_size=5)
    await db.run_migrations(pool)
    app.state.pool = pool
    app.state.worker_last_seen = None
    logger.info("startup complete")
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(title="AskDocs", lifespan=lifespan)

# Registered so request_id_middleware (added last) is outermost: it tags every
# response - including a 403 from the CSRF check - with the same request id.
app.middleware("http")(csrf_middleware)
app.middleware("http")(request_id_middleware)

app.include_router(meta_routes.router)
app.include_router(auth_routes.router)
