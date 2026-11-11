from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from redis.asyncio import Redis
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__, net, web
from app.agent import runner
from app.api import (
    chat,
    chats,
    events,
    favicons,
    literature,
    media,
    models,
    profiles,
    projects,
    tools,
)
from app.config import settings

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
# httpx logs every request at INFO, which drowns out the run's own narration.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("simplixity")

# Locked down because everything is served from this origin: no CDN, no inline
# script, no remote font. Vendoring the icons and the markdown renderer is what
# buys this.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    # blob: for the preview of a picture being attached, shown from the file
    # in hand before the upload has finished.
    "img-src 'self' data: blob: https:; "
    "connect-src 'self'; "
    # Both fall back to default-src today. Spelled out because the service
    # worker and the manifest are load-bearing for the installed app, and a
    # future tightening of default-src should have to disable them explicitly.
    "worker-src 'self'; "
    "manifest-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        health_check_interval=30,
    )
    await app.state.redis.ping()

    from app.store import attachments, chats, db

    app.state.db = await db.connect(settings.database_path)
    # Two halves of the same sweep: Redis tells any connected client that its
    # run died, SQLite makes sure the history agrees once the stream expires.
    await runner.reap_orphans(app.state.redis, settings.run_ttl_seconds)
    await chats.fail_stale_messages(app.state.db)
    # Files attached under a chat id whose first question was never sent.
    await attachments.reap_orphans(app.state.db)
    _report_configuration()

    try:
        yield
    finally:
        await runner.shutdown()
        await net.aclose()
        await app.state.db.close()
        await app.state.redis.aclose()


def _report_configuration() -> None:
    """Say what will and will not work, at boot, in one place.

    Discovering that no API key is set when the first answer fails is a worse
    experience than reading it in the startup log.
    """
    log.info(
        "simplixity %s up, redis=%s db=%s", app.version, settings.redis_url, settings.database_path
    )
    if not settings.openrouter_api_key:
        log.warning(
            "OPENROUTER_API_KEY is not set. Remote models will fail; only Ollama or "
            "LM Studio can answer."
        )
    # No model names here: each profile picks its own in the UI, so there is no
    # single install-wide model this line could truthfully report.
    log.info("search=%s · depths=quick|balanced|deep", settings.searxng_api_url)


app = FastAPI(
    title="Simplixity",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.include_router(profiles.router)
app.include_router(chat.router)
app.include_router(chats.router)
app.include_router(projects.router)
app.include_router(events.router)
app.include_router(models.router)
app.include_router(favicons.router)
app.include_router(media.router)
app.include_router(tools.router)
app.include_router(literature.router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Timing, security headers and a last-resort error boundary.

    An unhandled exception must still produce JSON: a client that gets an HTML
    error page from `/api/*` fails in a much more confusing way.
    """
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        response = JSONResponse(
            status_code=500, content={"detail": "Internal error. Check the server log."}
        )

    elapsed_ms = (time.monotonic() - started) * 1000
    # `setdefault`, not `update`: these are the defaults for a page, and a route
    # that set its own is doing so for a reason. The proxied-image endpoint
    # serves third-party SVG under a far stricter policy than this one, and
    # overwriting it here would silently undo that.
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    # SSE responses never end on their own, so timing them is meaningless.
    if not request.url.path.endswith("/events"):
        response.headers["X-Response-Time"] = f"{elapsed_ms:.0f}ms"
        if elapsed_ms > 2000:
            log.info("slow request %s %s took %.0fms", request.method, request.url.path, elapsed_ms)
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic's own shape is noisy in a toast; one sentence is enough.
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", ())[1:]) or "request"
    return JSONResponse(
        status_code=422, content={"detail": f"{field}: {first.get('msg', 'is invalid')}"}
    )


@app.get("/api/health")
async def health() -> JSONResponse:
    """Liveness plus dependency reachability. The compose healthcheck and a
    human with curl both use this."""
    redis_ok = True
    try:
        await app.state.redis.ping()
    except Exception:
        redis_ok = False

    db_ok = True
    try:
        await app.state.db.execute("SELECT 1")
    except Exception:
        db_ok = False

    healthy = redis_ok and db_ok
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "redis": redis_ok,
            "database": db_ok,
            "llm_configured": bool(settings.openrouter_api_key),
            "active_runs": await runner.active_count(),
            "version": app.version,
        },
    )


# Static frontend, copied not built. Mounted last so /api/* wins. See app/web.py
# for why every one of these revalidates.
if web.WEB_DIR.is_dir():
    app.mount("/static", web.RevalidatingFiles(directory=web.WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return web.page("index.html")

    @app.get("/literature", include_in_schema=False)
    async def literature_page() -> FileResponse:
        # A page of its own rather than a dialog over the chat: screening a
        # reading list is an afternoon's work with its own sidebar, and it
        # deserves an address that can be bookmarked.
        return web.page("literature.html")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        # Browsers apply heuristic freshness to favicons harder than to anything
        # else, so without the header a redesigned icon stays the old one for days.
        return web.page("assets/favicon.svg")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        # Served from the root because a worker only controls its own directory
        # and below. At /static/sw.js it could never see a navigation to "/",
        # which is the only reason it exists. Revalidating keeps a worker update
        # from waiting out a stale copy of itself.
        return web.page("sw.js", media_type="text/javascript")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return web.page("manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    async def apple_touch_icon() -> FileResponse:
        # iOS looks here by convention before it reads any markup, and asks for
        # a handful of near-miss names besides. One canonical route answers the
        # only one that matters.
        return web.page("assets/icons/apple-touch-icon.png")
