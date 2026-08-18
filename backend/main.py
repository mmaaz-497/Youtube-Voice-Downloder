"""App assembly: error handlers, security-headers middleware (plan D4),
origin resolution (plan D6), static frontend mounted after /api routes.
"""

import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles

from backend.config import Config
from backend.models.errors import register_error_handlers
from backend.services.health import ToolProbe
from backend.services.jobs import JobStore, Sweeper
from backend.services.pool import WorkerPool

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

CSP = (
    "default-src 'self'; "
    "img-src 'self' https://i.ytimg.com; "
    "script-src 'self'; "
    "style-src 'self'"
)

# Rotates every boot so origin hashes are short-lived by construction (plan D6).
BOOT_SALT = secrets.token_hex(16)


def resolve_origin_hash(request: Request, config: Config) -> str:
    """Fairness-accounting origin: SHA-256(client IP + boot salt), truncated.

    Socket IP by default. With TRUSTED_PROXY=1, use the RIGHTMOST
    X-Forwarded-For value — the only entry the trusted proxy vouches for;
    leftmost values are client-forgeable and ignored entirely (plan D6).
    """
    ip = request.client.host if request.client else "unknown"
    if config.trusted_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            ip = forwarded.split(",")[-1].strip()
    return hashlib.sha256(f"{ip}{BOOT_SALT}".encode()).hexdigest()[:16]


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.sweeper.start()
        try:
            yield
        finally:
            # Stop the sweeper before the pool so a pass can never run
            # against a half-torn-down app.
            app.state.sweeper.stop()
            app.state.pool.shutdown()

    app = FastAPI(title="YouTube Audio Extractor API", version="1.0.0", lifespan=lifespan)
    app.state.config = config
    app.state.store = JobStore(config)
    app.state.pool = WorkerPool(config.max_concurrency)
    app.state.sweeper = Sweeper(app.state.store, config.sweep_interval_seconds)
    app.state.tool_probe = ToolProbe()
    app.state.started_at = time.monotonic()
    # Exposed via app.state (not imported by routes) to avoid an import cycle.
    app.state.resolve_origin = lambda request: resolve_origin_hash(request, config)

    register_error_handlers(app)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            response.headers["Content-Security-Policy"] = CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    # /api routes are registered before the static mount so the catch-all
    # static handler can never shadow them.
    from backend.api.routes import router as api_router

    app.include_router(api_router)

    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    app.openapi = _openapi_matching_the_contract(app)
    return app


def _openapi_matching_the_contract(app: FastAPI):
    """FastAPI documents an automatic 422 for any endpoint with validated
    input, but this app never returns one: the RequestValidationError handler
    converts it into the 400 INVALID_INPUT envelope. Stripping it keeps the
    generated schema honest and identical to contracts/openapi.yaml (T049).
    """

    def openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                operation.get("responses", {}).pop("422", None)
        app.openapi_schema = schema
        return schema

    return openapi


app = create_app()
