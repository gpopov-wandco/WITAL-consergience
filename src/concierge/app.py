"""The ASGI application: routes, security headers, health, worker lifecycle.

Implements: F19, F22, F29, F31, F32, F33, 6.6

``create_app`` accepts prebuilt services (tests, demo) or the ones the runtime
builds from settings (production). Security headers: a CSP without ``unsafe-inline``
scripts, ``X-Content-Type-Options``, ``Referrer-Policy`` and ``frame-ancestors``;
HSTS is set by Caddy in front. ``/healthz`` is liveness; ``/readyz`` checks the
database, the migration head, the worker heartbeat and the master key. Demo pages
and the mock Graph server are mounted only in demo mode.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from concierge import __version__, metrics
from concierge.admin.deps import AdminGuard, Forbidden
from concierge.admin.i18n import labels
from concierge.admin.operator import build_router as build_operator_router
from concierge.admin.routes import build_router as build_admin_router
from concierge.admin.templating import templates
from concierge.channels.voice.routes import build_router as build_voice_router
from concierge.channels.web.routes import build_router as build_widget_router
from concierge.channels.whatsapp.routes import build_router as build_wa_router
from concierge.db.migrate import head_state
from concierge.demo.mock_graph import MockGraphState
from concierge.demo.mock_graph import build_router as build_mock_graph_router
from concierge.demo.routes import build_router as build_demo_router
from concierge.log import configure_logging, get_logger
from concierge.registration.routes import build_router as build_registration_router
from concierge.reservations.routes import build_router as build_reservation_router
from concierge.services import Services, schedule_registration_purge

log = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
WORKER_HEARTBEAT_MAX_S = 30.0
Startup = Callable[[], Awaitable[None]]


def create_app(
    services: Services,
    *,
    start_worker: bool = True,
    graph_state: MockGraphState | None = None,
    startup: list[Startup] | None = None,
) -> FastAPI:
    configure_logging(services.ctx.settings.gc_log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for hook in startup or []:
            await hook()
        if start_worker:
            await schedule_registration_purge(services.jobs, services.ctx)
            await services.timers.schedule_all()
            await services.maintenance.schedule_all()
            services.worker.start()
        try:
            yield
        finally:
            await services.worker.stop()

    app = FastAPI(
        title="WITALCO Guest Concierge",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = services
    app.include_router(build_wa_router(services.ingress))
    app.include_router(build_widget_router(services.widget))
    app.include_router(
        build_voice_router(
            engine=services.voice,
            postcall=services.postcall,
            tenants=services.tenants,
            settings=services.ctx.settings,
            clock=services.ctx.clock,
            secrets=services.secrets,
        )
    )
    app.include_router(
        build_reservation_router(
            services.tenants, services.secrets, services.reservations, services.ctx.clock
        )
    )
    guard = AdminGuard(services.auth, services.tenants)
    app.include_router(build_admin_router(services, services.auth, guard))
    app.include_router(build_operator_router(services, services.auth, guard))

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden) -> Response:
        return templates.TemplateResponse(
            request,
            "admin/forbidden.html",
            {"lang": "de", "t": labels("de"), "reason": str(exc)},
            status_code=403,
        )

    app.include_router(
        build_registration_router(
            services.registration,
            services.tenants,
            services.ctx.clock,
            stripe_webhook_secret=services.ctx.settings.stripe_webhook_secret.get_secret_value(),
        )
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    if services.demo:
        app.include_router(build_demo_router(services, graph_state))
    if graph_state is not None:
        app.include_router(build_mock_graph_router(graph_state))
    _add_health(app, services)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CSP.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"x-frame-options", b"DENY"),
)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware: adds the security headers to every HTTP response.

    A plain ASGI wrapper costs a function call per message; Starlette's
    ``BaseHTTPMiddleware`` would add a task group and two streams per request, which
    dominated the webhook acknowledgement time under a burst (10.5).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        no_store = not path.startswith(("/static", "/w/"))

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                if no_store and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _add_health(app: FastAPI, services: Services) -> None:
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/metrics")
    async def metrics_endpoint(request: Request) -> Response:
        token = services.ctx.settings.gc_metrics_token.get_secret_value()
        if not token:
            return Response("metrics token not configured", status_code=503)
        if not metrics.authorised(request.headers.get("authorization"), token):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return Response(await metrics.render(services), media_type="text/plain; version=0.0.4")

    @app.get("/readyz")
    async def readyz() -> Response:
        checks: dict[str, Any] = {}
        ok = True
        try:
            current, head = await head_state(services.ctx.db.engine)
            checks["database"] = "ok"
            checks["migration"] = "ok" if current == head else f"behind ({current} != {head})"
            ok = ok and current == head
        except Exception as exc:
            checks["database"] = f"error: {type(exc).__name__}"
            ok = False
        age = services.worker.heartbeat_age_s()
        if age is None:
            checks["worker"] = "not started"
            ok = ok and not services.worker.handlers
        elif age > WORKER_HEARTBEAT_MAX_S:
            checks["worker"] = f"stale heartbeat ({age:.0f}s)"
            ok = False
        else:
            checks["worker"] = "ok"
        try:
            services.ctx.settings.master_keys()
            checks["master_key"] = "ok"
        except Exception as exc:
            checks["master_key"] = f"missing: {exc}"
            ok = False
        return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)
