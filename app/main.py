"""FastAPI application factory.

Middleware order (outermost first): RequestId -> UploadGuard -> CORS -> app.
Routers live under /api/v1 (health at /health). The built React dashboard is served at / (single origin in prod)."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import documents, health
from app.config import settings
from app.core.db import get_async_engine
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.request_context import RequestIdMiddleware
from app.core.upload_guard import UploadGuardMiddleware

SPA_DIR = Path(__file__).parent / "static" / "spa"

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
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(documents.router, prefix="/api/v1")
    for router in _optional_routers():
        app.include_router(router, prefix="/api/v1")

    if SPA_DIR.exists():
        app.mount("/assets", StaticFiles(directory=SPA_DIR / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = (SPA_DIR / path).resolve()
            if path and candidate.is_file() and SPA_DIR.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(SPA_DIR / "index.html")  # client-side routes

    return app


app = create_app()
