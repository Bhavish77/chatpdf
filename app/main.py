"""FastAPI app: lifespan (pool + migrations + optional embedded worker),
middleware, route registration."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

if sys.platform == "win32":
    # psycopg's async waiting needs loop.add_reader/add_writer, which the default
    # ProactorEventLoop on Windows does not implement. Only matters running outside
    # Docker on a Windows dev machine; the Linux container is unaffected.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app import db, seed, worker
from app.config import get_settings
from app.deps import csrf_middleware, security_headers_middleware
from app.llm import GeminiClient
from app.logutil import configure_logging, request_id_middleware
from app.rag.graph import build_graph
from app.routes import auth as auth_routes
from app.routes import chat as chat_routes
from app.routes import documents as documents_routes
from app.routes import meta as meta_routes
from app.vectorindex import PgVectorIndex

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    app.state.settings = settings
    await db.run_migrations(settings.DATABASE_URL)
    pool = await db.open_pool(settings, max_size=5)
    app.state.pool = pool
    app.state.worker_last_seen = None
    await seed.ensure_seed_document(pool)

    llm = GeminiClient(settings)
    vectors = PgVectorIndex(pool)
    app.state.graph = build_graph(llm, vectors, settings)
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
app.middleware("http")(security_headers_middleware)
app.middleware("http")(request_id_middleware)

app.include_router(meta_routes.router)
app.include_router(auth_routes.router)
app.include_router(documents_routes.router)
app.include_router(chat_routes.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
