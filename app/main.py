"""FastAPI app: middleware, routers, static frontend."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.dataset import load_tickets
from app.deps import Providers, configure_limits
from app.routers import meta, race, review
from app.security import register_secret

logger = logging.getLogger("jev-race")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MAX_BODY_BYTES = 32 * 1024
# Pasted tickets (up to 50k chars) and code (16k chars) need more room than
# any other request. The character caps in the schemas are the real bound;
# this only keeps an oversized body from being read at all.
LARGE_BODY_BYTES = 256 * 1024
LARGE_BODY_ROUTES = frozenset({"/api/race", "/api/review"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})

_ROOT = Path(__file__).resolve().parent.parent
# In the container the SPA is baked in at static/. Running from a checkout,
# fall back to the Vite build output so `npm run build` + uvicorn serves the
# whole app from one process instead of answering 404 at the root.
STATIC_CANDIDATES = (_ROOT / "static", _ROOT / "web" / "dist")


def find_static_dir() -> Path | None:
    return next((p for p in STATIC_CANDIDATES if (p / "index.html").is_file()), None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    # The only secrets the server itself holds. Visitor keys arrive per
    # request and are never registered, logged, or kept past their run.
    for key in (settings.openai_compat_api_key, settings.race_token):
        register_secret(key)
    app.state.tickets = load_tickets()
    app.state.provider_factory = Providers
    configure_limits(settings)
    logger.info(
        "started: dataset=%d openai_compat=%s rate_limiting=%s",
        len(app.state.tickets),
        settings.openai_compat_enabled,
        settings.rate_limiting,
    )
    yield


def create_app(static_dir: Path | None = None) -> FastAPI:
    static_dir = static_dir if static_dir is not None else find_static_dir()
    settings = get_settings()
    app = FastAPI(
        title="Jev Triage Race",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    @app.middleware("http")
    async def guards(request: Request, call_next):
        if request.method in WRITE_METHODS:
            # A body without a declared length (chunked) would slip past the
            # cap. Browsers and HTTP clients send Content-Length for string
            # and JSON bodies, so requiring it costs legitimate callers nothing.
            length = request.headers.get("content-length")
            if length is None or not length.isdigit() or "transfer-encoding" in request.headers:
                return JSONResponse({"error": "length required"}, status_code=411)
            cap = LARGE_BODY_BYTES if request.url.path in LARGE_BODY_ROUTES else MAX_BODY_BYTES
            if int(length) > cap:
                return JSONResponse({"error": "payload too large"}, status_code=413)
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Never echo `input`: it can be 16k chars of pasted code, and a lone
        # surrogate in it would make this very response unencodable.
        detail = [
            {"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse({"detail": detail}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # Never leak str(exc) — it can carry upstream URLs, headers, or keys.
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"error": "internal_error"}, status_code=500)

    app.include_router(meta.router)
    app.include_router(race.router)
    app.include_router(review.router)

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    else:
        @app.get("/", include_in_schema=False)
        async def no_frontend():
            return JSONResponse(
                {
                    "error": "frontend_not_built",
                    "detail": (
                        "The API is running but no built frontend was found. "
                        "Use ./dev for hot reload (UI on http://localhost:5173), "
                        "or run `cd web && npm run build` to serve it from here."
                    ),
                },
                status_code=503,
            )

    return app


app = create_app()
