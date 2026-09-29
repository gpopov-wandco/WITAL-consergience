"""Widget HTTP endpoints: static files, config, session, message (F22).

Implements: F22, F31, C7

``/w/loader.js`` is the one script tag a hotel embeds. The API answers with the
tenant's CORS allow-list (exact origins) and refuses other origins with 403.
Per-IP and per-session limits return 429. Replies are whole validated messages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from concierge.channels.web.service import WidgetError, WidgetService

STATIC_DIR = Path(__file__).resolve().parents[2] / "static" / "widget"
_FILES = {
    "loader.js": "text/javascript",
    "gc-widget.js": "text/javascript",
    "gc-widget.css": "text/css",
}


def _cors(origin: str | None) -> dict[str, str]:
    if not origin:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-GC-Session",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def build_router(widget: WidgetService) -> APIRouter:
    api = APIRouter()

    @api.get("/w/{name}")
    async def static_file(name: str) -> Response:
        if name not in _FILES:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(
            STATIC_DIR / name,
            media_type=_FILES[name],
            headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
        )

    async def _guarded(
        request: Request, body: dict[str, Any] | None
    ) -> tuple[Any, Any, dict[str, str]]:
        key = (body or {}).get("key") or request.query_params.get("key")
        tenant, profile = await widget.tenant_for_key(str(key) if key else None)
        origin = request.headers.get("origin")
        if not widget.origin_allowed(profile, origin):
            raise WidgetError(403, "origin_not_allowed", "origin is not allowed for this widget")
        widget.check_ip(profile, _client_ip(request))
        return tenant, profile, _cors(origin)

    async def _json(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            raise WidgetError(400, "bad_json", "invalid JSON") from None
        if not isinstance(body, dict):
            raise WidgetError(400, "bad_json", "expected an object")
        return body

    @api.options("/w/api/{path:path}")
    async def preflight(request: Request, path: str) -> Response:
        origin = request.headers.get("origin")
        key = request.query_params.get("key")
        try:
            _tenant, profile = await widget.tenant_for_key(key)
        except WidgetError:
            return Response(
                status_code=204,
                headers=_cors(origin) if origin and origin.rstrip("/") == widget.own_origin else {},
            )
        if widget.origin_allowed(profile, origin):
            return Response(status_code=204, headers=_cors(origin))
        return Response(status_code=403)

    @api.get("/w/api/config")
    async def config(request: Request) -> Response:
        try:
            tenant, profile, cors = await _guarded(request, None)
        except WidgetError as exc:
            return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)
        return JSONResponse(widget.config(tenant, profile), headers=cors)

    @api.post("/w/api/session")
    async def session(request: Request) -> Response:
        try:
            body = await _json(request)
            tenant, profile, cors = await _guarded(request, body)
            created = await widget.start_session(tenant, profile, lang=body.get("lang"))
        except WidgetError as exc:
            return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)
        return JSONResponse(
            {
                "session": created.token,
                "lang": created.lang,
                "last_id": created.last_id,
                "messages": [
                    {
                        "text": created.disclosure,
                        "kind": "disclosure",
                        "lang": created.lang,
                        "buttons": [],
                    }
                ],
            },
            headers=cors,
        )

    @api.get("/w/api/updates")
    async def updates(request: Request) -> Response:
        """Polled by the widget while staff handle the conversation (no push channel)."""
        try:
            tenant, profile, cors = await _guarded(request, None)
            token = request.query_params.get("session") or request.headers.get("x-gc-session")
            found = await widget.updates(
                tenant,
                profile,
                token=str(token or ""),
                after=str(request.query_params.get("after", "")),
            )
        except WidgetError as exc:
            return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)
        return JSONResponse(
            {
                "messages": found.messages,
                "status": found.status,
                "handoff": found.handoff,
                "last_id": found.last_id,
            },
            headers={**cors, "Cache-Control": "no-store"},
        )

    @api.post("/w/api/message")
    async def message(request: Request) -> Response:
        try:
            body = await _json(request)
            tenant, profile, cors = await _guarded(request, body)
            token = body.get("session") or request.headers.get("x-gc-session") or ""
            reply = await widget.message(
                tenant,
                profile,
                token=str(token),
                text=str(body.get("text", "")),
                lang=body.get("lang") if isinstance(body.get("lang"), str) else None,
                button_id=body.get("button_id") if isinstance(body.get("button_id"), str) else None,
            )
        except WidgetError as exc:
            return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)
        return JSONResponse(
            {
                "messages": reply.messages,
                "lang": reply.lang,
                "handoff": reply.handoff,
                "mode": reply.mode,
                "last_id": reply.last_id,
            },
            headers=cors,
        )

    return api
