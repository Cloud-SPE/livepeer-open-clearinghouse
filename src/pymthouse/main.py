"""FastAPI application factory and entrypoint.

Wires the FastAPI app, registers domain routers (added in later phases), and
exposes a single ASGI `app` for uvicorn. Phase 2 only includes `/health` and
the OpenAPI surface; vertical-slice routes are added in Phase 4+.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from pymthouse import __version__
from pymthouse.settings import Settings, get_settings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook. Providers will register their lifecycles here."""
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI application instance."""
    cfg = settings or get_settings()

    app = FastAPI(
        title="PymtHouse",
        version=__version__,
        description="Payment clearinghouse for Livepeer applications",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "version": __version__, "env": cfg.app_env}
        )

    return app


app = create_app()


def cli() -> None:
    """Module entrypoint for `uv run pymthouse` (see [project.scripts])."""
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "pymthouse.main:app",
        host=cfg.app_host,
        port=cfg.app_port,
        log_level=cfg.log_level,
        reload=cfg.app_env == "dev",
    )


if __name__ == "__main__":
    cli()
    sys.exit(0)
