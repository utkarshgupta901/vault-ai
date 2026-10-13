"""FastAPI application factory.

Middleware order (outermost first): RequestId -> UnhandledError -> RequestLog -> UploadGuard -> CORS -> app.
UnhandledError sits directly inside RequestId so a 500 still carries the request id (see core/errors.py).
Routers live under /api/v1 (health at /health). The built React dashboard is served at / (single origin in prod)."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import documents, health
from app.config import settings
from app.core.db import get_async_engine
from app.core.errors import AppError, ErrorCode, UnhandledErrorMiddleware, install_error_handlers
from app.core.logging import configure_logging
from app.core.request_context import RequestIdMiddleware
from app.core.upload_guard import UploadGuardMiddleware

IMAGE_SPA_DIR = Path("/srv/spa")    # Dockerfile copies the build here: outside ./app, so the dev bind mount can't hide it
REPO_SPA_DIR = Path(__file__).parent / "static" / "spa"

# Only for the dashboard's own files (index.html, /assets, favicon), not the API or /docs (Swagger UI loads from a
# CDN). The SPA loads scripts only from its own origin and calls only its own API, so 'self' is enough for both.
# 'unsafe-inline' is allowed for styles only (style attributes / injected <style>), never for scripts.
# frame-ancestors 'none' stops clickjacking; Referrer-Policy same-origin keeps document URLs from leaking to other sites.
# Verified in headless Chrome against a production build: no CSP violations on /, /chat, /metrics.
SPA_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; connect-src 'self'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def spa_dir() -> Path:
    if settings.SPA_DIR:
        return Path(settings.SPA_DIR)
    return IMAGE_SPA_DIR if IMAGE_SPA_DIR.exists() else REPO_SPA_DIR


class _WithSecurityHeaders:
    """Adds SPA_SECURITY_HEADERS to every response of the wrapped ASGI app (the /assets static files)."""

    def __init__(self, app: ASGIApp):
        self.app = app
        self.headers = [(k.lower().encode(), v.encode()) for k, v in SPA_SECURITY_HEADERS.items()]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *self.headers]
            await send(message)

        await self.app(scope, receive, send_with_headers)

DESCRIPTION = """
**Vault AI** — upload documents, get AI insights, and chat with them (RAG) with page-level citations.

Authenticate every `/api/v1` call with the `X-API-Key` header (click **Authorize**). Errors always use the envelope
`{"error": {"code", "message", "details", "request_id"}}`.
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    yield
    await get_async_engine().dispose()


def _optional_routers() -> list:
    """Routers owned by later work packages; included when present so the skeleton boots on its own."""
    import importlib

    routers = []
    for name in ("insights", "chat", "search", "metrics"):
        try:
            routers.append(importlib.import_module(f"app.api.{name}").router)
        except ModuleNotFoundError:
            pass
    return routers


def create_app() -> FastAPI:
    app = FastAPI(title="Vault AI", version="0.1.0", description=DESCRIPTION, lifespan=lifespan,
                  openapi_tags=[{"name": "documents"}, {"name": "insights"}, {"name": "chat"}, {"name": "search"},
                                {"name": "metrics"}, {"name": "health"}])
    install_error_handlers(app)

    app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["X-Request-ID", "Location"])
    app.add_middleware(UploadGuardMiddleware)
    try:
        from app.core.middleware import RequestLogMiddleware  # WP4

        app.add_middleware(RequestLogMiddleware)
    except ModuleNotFoundError:
        pass
    app.add_middleware(UnhandledErrorMiddleware)   # directly inside RequestId (the last added is the outermost)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(documents.router, prefix="/api/v1")
    for router in _optional_routers():
        app.include_router(router, prefix="/api/v1")

    root = spa_dir()
    if (root / "index.html").exists():
        app.mount("/assets", _WithSecurityHeaders(StaticFiles(directory=root / "assets")), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            if path.startswith(("api/", "health/")):  # unknown API path: a JSON 404, never the dashboard HTML
                raise AppError(ErrorCode.NOT_FOUND, 404, f"No route for /{path}.")
            candidate = (root / path).resolve()
            if path and candidate.is_file() and root.resolve() in candidate.parents:
                return FileResponse(candidate, headers=SPA_SECURITY_HEADERS)
            return FileResponse(root / "index.html", headers=SPA_SECURITY_HEADERS)  # client-side routes

    return app


app = create_app()
