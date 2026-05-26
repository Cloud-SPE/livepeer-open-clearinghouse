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

from livepeer_open_clearinghouse import __version__
from livepeer_open_clearinghouse.dependencies import SettingsDep
from livepeer_open_clearinghouse.domains.accounts import runtime as accounts_runtime
from livepeer_open_clearinghouse.domains.admin import runtime as admin_runtime
from livepeer_open_clearinghouse.domains.admin import service as admin_service
from livepeer_open_clearinghouse.domains.api_keys import runtime as api_keys_runtime
from livepeer_open_clearinghouse.domains.billing import runtime as billing_runtime
from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.discovery import runtime as discovery_runtime
from livepeer_open_clearinghouse.domains.jobs import runtime as jobs_runtime
from livepeer_open_clearinghouse.domains.notifications import runtime as notifications_runtime
from livepeer_open_clearinghouse.domains.payments import runtime as payments_runtime
from livepeer_open_clearinghouse.domains.payments import service as payments_service
from livepeer_open_clearinghouse.domains.sessions import runtime as sessions_runtime
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.telemetry import runtime as telemetry_runtime
from livepeer_open_clearinghouse.domains.telemetry import service as telemetry_service
from livepeer_open_clearinghouse.errors import register_handlers
from livepeer_open_clearinghouse.providers.clock import DefaultClock
from livepeer_open_clearinghouse.providers.db import session_scope
from livepeer_open_clearinghouse.providers.http.gzip_request import (
    GzipRequestMiddleware,
)
from livepeer_open_clearinghouse.providers.scheduler import (
    register_interval_job,
    shutdown_scheduler,
    start_scheduler,
)
from livepeer_open_clearinghouse.providers.telemetry import (
    configure_logging,
    get_logger,
    metrics_middleware,
    render_metrics,
)
from livepeer_open_clearinghouse.settings import Settings, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: PLR0915 — composition root
    cfg: Settings = app.state.settings
    configure_logging(cfg)
    log = get_logger("livepeer_open_clearinghouse.startup")
    log.info("startup.begin", env=cfg.app_env, version=__version__)

    if cfg.admin_bootstrap_token is not None:
        async with session_scope() as db:
            await admin_service.ensure_bootstrap_operator(
                db, bootstrap_token=cfg.admin_bootstrap_token.get_secret_value()
            )
        log.info("startup.bootstrap_operator.ready")
    else:
        log.warning("startup.bootstrap_operator.skipped", reason="ADMIN_BOOTSTRAP_TOKEN unset")

    # Background jobs
    clock = DefaultClock()

    async def _expire_stale_idempotency_keys() -> None:
        async with session_scope() as db:
            n = await payments_service.expire_stale_idempotency_keys(db, clock=clock)
            if n:
                log.info("scheduler.idempotency_keys.expired", count=n)

    async def _snapshot_deposit() -> None:
        from livepeer_open_clearinghouse.dependencies import (  # noqa: PLC0415
            _default_payment_daemon,
        )

        try:
            daemon = _default_payment_daemon()
            async with session_scope() as db:
                row = await payments_service.snapshot_deposit(db, clock=clock, daemon=daemon)
                log.info(
                    "scheduler.deposit_snapshot.taken",
                    deposit_wei=str(row.deposit_wei),
                    reserve_wei=str(row.reserve_wei),
                )
        except Exception as exc:
            log.warning("scheduler.deposit_snapshot.failed", error=str(exc))

    async def _auto_replenish() -> None:
        try:
            async with session_scope() as db:
                n = await billing_service.run_auto_replenish(db, clock=clock, settings=cfg)
                if n:
                    log.info("scheduler.auto_replenish.applied", users=n)
        except Exception as exc:
            log.warning("scheduler.auto_replenish.failed", error=str(exc))

    async def _reconcile_open_sessions() -> None:
        from livepeer_open_clearinghouse.dependencies import (  # noqa: PLC0415
            _default_payment_daemon,
        )

        try:
            daemon = _default_payment_daemon()
            async with session_scope() as db:
                n = await sessions_service.reconcile_open_sessions(db, daemon=daemon, clock=clock)
                if n:
                    log.info("scheduler.reconcile_open_sessions.finalized", count=n)
        except Exception as exc:
            log.warning("scheduler.reconcile_open_sessions.failed", error=str(exc))

    async def _purge_expired_telemetry() -> None:
        try:
            async with session_scope() as db:
                n = await telemetry_service.purge_expired(
                    db,
                    retention_days=cfg.telemetry_raw_retention_days,
                    clock=clock,
                )
                if n:
                    log.info("scheduler.telemetry_retention.purged", count=n)
        except Exception as exc:
            log.warning("scheduler.telemetry_retention.failed", error=str(exc))

    register_interval_job(
        _expire_stale_idempotency_keys,
        name="expire_stale_idempotency_keys",
        seconds=300,
    )
    register_interval_job(
        _snapshot_deposit,
        name="snapshot_deposit",
        seconds=300,
    )
    if cfg.auto_replenish_check_interval_seconds > 0:
        register_interval_job(
            _auto_replenish,
            name="auto_replenish",
            seconds=cfg.auto_replenish_check_interval_seconds,
        )
    register_interval_job(
        _reconcile_open_sessions,
        name="reconcile_open_sessions",
        seconds=sessions_service.DEFAULT_JANITOR_INTERVAL_SECONDS,
    )
    if cfg.telemetry_raw_retention_days > 0:
        register_interval_job(
            _purge_expired_telemetry,
            name="telemetry_retention",
            seconds=cfg.telemetry_retention_janitor_interval_seconds,
        )
    start_scheduler()

    yield
    shutdown_scheduler()
    log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(
        title="Livepeer Open Clearinghouse",
        version=__version__,
        description="Payment clearinghouse for Livepeer applications",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = cfg

    # Decompress gzipped request bodies before they reach the handler.
    # SDK telemetry batches gzip when > 1 KiB (exec-plan 002 §"Mechanism").
    app.add_middleware(GzipRequestMiddleware)

    # SessionMiddleware backs authlib's OAuth-state storage. It uses a
    # cookie distinct from our own open_clearinghouse_session (which is
    # itsdangerous-
    # signed at the application layer).
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret.get_secret_value(),
        session_cookie="open_clearinghouse_oauth",
        same_site="lax",
        https_only=cfg.app_env != "dev",
    )
    app.middleware("http")(metrics_middleware)
    register_handlers(app)

    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__, "env": cfg.app_env})

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
    app.include_router(admin_runtime.sdk_router)
    app.include_router(discovery_runtime.router)
    app.include_router(notifications_runtime.router)
    app.include_router(payments_runtime.router)
    app.include_router(jobs_runtime.router)
    app.include_router(sessions_runtime.router)
    app.include_router(telemetry_runtime.router)
    app.include_router(telemetry_runtime.portal_router)
    app.include_router(telemetry_runtime.privacy_router)
    app.include_router(telemetry_runtime.admin_router)

    # Static SPAs — mounted under their URL prefix so hash routing works
    # and assets resolve cleanly (e.g., /portal/portal.css).
    #
    # The web/ tree is sibling-of-CWD in both layouts we care about:
    #   - dev (`make run` / `uv run uvicorn ...` from the repo root):
    #     cwd = <repo>, so cwd/web exists.
    #   - container (WORKDIR = /srv/livepeer_open_clearinghouse): cwd/web
    #     exists because the Dockerfile copies the tree there. The
    #     installed livepeer_open_clearinghouse module
    #     lives inside the venv and has no nearby web/, so we deliberately
    #     don't compute web_root from __file__.
    #
    # In dev we wrap StaticFiles to send `Cache-Control: no-cache` so the
    # browser revalidates every asset (still gets a fast 304 via ETag) and
    # iterating on the SPA doesn't require a hard refresh per change. In
    # prod we serve them with Starlette's default headers.
    class _NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache"
            return response

    StaticFilesCls = _NoCacheStaticFiles if cfg.app_env == "dev" else StaticFiles
    web_root = Path.cwd() / "web"
    portal_dir = web_root / "portal"
    admin_dir = web_root / "admin"
    if portal_dir.is_dir():
        app.mount(
            "/portal",
            StaticFilesCls(directory=portal_dir, html=True),
            name="portal",
        )
    if admin_dir.is_dir():
        app.mount(
            "/admin",
            StaticFilesCls(directory=admin_dir, html=True),
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
        "livepeer_open_clearinghouse.main:app",
        host=cfg.app_host,
        port=cfg.app_port,
        log_level=cfg.log_level,
        reload=cfg.app_env == "dev",
    )


if __name__ == "__main__":
    cli()
    sys.exit(0)
