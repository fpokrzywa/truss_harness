"""FastAPI application factory (skeleton: health only; module ports add their routers)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shifde import __version__
from shifde.core.db import Database
from shifde.core.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Serves `GET /healthz` (liveness, no database access)."""
    settings = settings or get_settings()
    db = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        db.dispose()

    app = FastAPI(
        title="SHIFDE API",
        version=__version__,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
