"""FastAPI application factory and entrypoint."""

from __future__ import annotations

import hmac
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from pymthouse import __version__
from pymthouse.dependencies import SettingsDep
from pymthouse.domains.accounts import runtime as accounts_runtime
from pymthouse.domains.admin import runtime as admin_runtime
from pymthouse.domains.admin import service as admin_service
from pymthouse.domains.api_keys import runtime as api_keys_runtime
from pymthouse.domains.billing import runtime as billing_runtime
from pymthouse.domains.discovery import runtime as discovery_runtime
from pymthouse.domains.payments import runtime as payments_runtime
from pymthouse.domains.payments import service as payments_service
from pymthouse.domains.usage import runtime as usage_runtime
from pymthouse.errors import register_handlers
from pymthouse.providers.clock import DefaultClock
from pymthouse.providers.db import session_scope
from pymthouse.providers.scheduler import (
    register_interval_job,
    shutdown_scheduler,
    start_scheduler,
)
from pymthouse.providers.telemetry import (
    configure_logging,
    get_logger,
    metrics_middleware,
    render_metrics,
)
from pymthouse.settings import Settings, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: Settings = app.state.settings
    configure_logging(cfg)
    log = get_logger("pymthouse.startup")
    log.info("startup.begin", env=cfg.app_env, version=__version__)

    if cfg.admin_bootstrap_token is not None:
        async with session_scope(cfg) as db:
            await admin_service.ensure_bootstrap_operator(
                db, bootstrap_token=cfg.admin_bootstrap_token.get_secret_value()
            )
        log.info("startup.bootstrap_operator.ready")
    else:
        log.warning("startup.bootstrap_operator.skipped",
                    reason="ADMIN_BOOTSTRAP_TOKEN unset")

    # Background jobs
    clock = DefaultClock()

    async def _expire_stale_idempotency_keys() -> None:
        async with session_scope(cfg) as db:
            n = await payments_service.expire_stale_idempotency_keys(db, clock=clock)
            if n:
                log.info("scheduler.idempotency_keys.expired", count=n)

    register_interval_job(
        _expire_stale_idempotency_keys,
        name="expire_stale_idempotency_keys",
        seconds=300,
    )
    start_scheduler()

    yield
    shutdown_scheduler()
    log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(
        title="PymtHouse",
        version=__version__,
        description="Payment clearinghouse for Livepeer applications",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = cfg

    # SessionMiddleware backs authlib's OAuth-state storage. It uses a
    # cookie distinct from our own pymthouse_session (which is itsdangerous-
    # signed at the application layer).
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret.get_secret_value(),
        session_cookie="pymthouse_oauth",
        same_site="lax",
        https_only=cfg.app_env != "dev",
    )
    app.middleware("http")(metrics_middleware)
    register_handlers(app)

    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "version": __version__, "env": cfg.app_env}
        )

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def metrics_endpoint(
        cfg_dep: SettingsDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        expected = "Bearer " + cfg_dep.metrics_token.get_secret_value()
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    app.include_router(accounts_runtime.router)
    app.include_router(api_keys_runtime.router)
    app.include_router(billing_runtime.router)
    app.include_router(admin_runtime.router)
    app.include_router(discovery_runtime.router)
    app.include_router(payments_runtime.router)
    app.include_router(usage_runtime.router)

    # Static SPAs — mounted under their URL prefix so hash routing works
    # and assets resolve cleanly (e.g., /portal/portal.css).
    web_root = Path(__file__).resolve().parents[2] / "web"
    portal_dir = web_root / "portal"
    admin_dir = web_root / "admin"
    if portal_dir.is_dir():
        app.mount(
            "/portal",
            StaticFiles(directory=portal_dir, html=True),
            name="portal",
        )
    if admin_dir.is_dir():
        app.mount(
            "/admin",
            StaticFiles(directory=admin_dir, html=True),
            name="admin",
        )

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse("/portal/")

    return app


app = create_app()


def cli() -> None:
    import uvicorn  # noqa: PLC0415

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
