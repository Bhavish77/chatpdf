"""FastAPI app: lifespan (pool + migrations + optional embedded worker),
middleware, route registration."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI

if sys.platform == "win32":
    # psycopg's async waiting needs loop.add_reader/add_writer, which the default
    # ProactorEventLoop on Windows does not implement. Only matters running outside
    # Docker on a Windows dev machine; the Linux container is unaffected.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app import db, worker
from app.config import get_settings
from app.deps import csrf_middleware
from app.logutil import configure_logging, request_id_middleware
from app.routes import auth as auth_routes
from app.routes import documents as documents_routes
from app.routes import meta as meta_routes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    app.state.settings = settings
    await db.run_migrations(settings.DATABASE_URL)
    pool = await db.open_pool(settings, max_size=5)
    app.state.pool = pool
    app.state.worker_last_seen = None
    logger.info("startup complete")

    worker_stop_event: asyncio.Event | None = None
    worker_task: asyncio.Task | None = None
    if settings.EMBEDDED_WORKER:
        def _tick() -> None:
            app.state.worker_last_seen = datetime.now(UTC).isoformat()

        worker_stop_event = asyncio.Event()
        worker_task = asyncio.create_task(worker.run_worker(worker_stop_event, settings, on_tick=_tick))
        logger.info("embedded worker started")

    try:
        yield
    finally:
        if worker_stop_event is not None:
            worker_stop_event.set()
        if worker_task is not None:
            await asyncio.wait_for(worker_task, timeout=15)
        await db.close_pool()


app = FastAPI(title="AskDocs", lifespan=lifespan)

# Registered so request_id_middleware (added last) is outermost: it tags every
# response - including a 403 from the CSRF check - with the same request id.
app.middleware("http")(csrf_middleware)
app.middleware("http")(request_id_middleware)

app.include_router(meta_routes.router)
app.include_router(auth_routes.router)
app.include_router(documents_routes.router)
