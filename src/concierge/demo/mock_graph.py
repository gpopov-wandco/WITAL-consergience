"""Mock Graph API server for the offline demo (F29).

Implements: F29, 10.4

Mounted at ``/mock-graph`` in demo mode; the real Graph client points at it, so the
whole send path runs offline. It records sends, hands out message ids, serves phone,
subscription, template and media endpoints, and injects Meta errors on demand
(131047 window closed, 130429 throttled, 190 token invalid) or timeouts.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

ERROR_BODIES: dict[int, dict[str, Any]] = {
    131047: {
        "message": "Re-engagement message",
        "type": "OAuthException",
        "code": 131047,
        "error_subcode": 2494010,
    },
    130429: {"message": "Rate limit hit", "type": "OAuthException", "code": 130429},
    190: {"message": "Invalid OAuth access token", "type": "OAuthException", "code": 190},
}
ERROR_STATUS: dict[int, int] = {131047: 400, 130429: 429, 190: 401}


@dataclass
class MockGraphState:
    sent: list[dict[str, Any]] = field(default_factory=list)
    read_marks: list[dict[str, Any]] = field(default_factory=list)
    templates: list[dict[str, Any]] = field(default_factory=list)
    injections: deque[int | str] = field(default_factory=deque)
    media: dict[str, bytes] = field(default_factory=dict)
    valid_token: str = "demo-access-token"
    counter: int = 0

    def inject(self, code: int | str, times: int = 1) -> None:
        for _ in range(times):
            self.injections.append(code)

    def next_id(self) -> str:
        self.counter += 1
        return f"wamid.mock.{self.counter}"

    def messages_to(self, to: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if str(m["payload"].get("to")) == to]


def _token_of(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth.removeprefix("Bearer ").strip()


def build_router(state: MockGraphState) -> APIRouter:
    api = APIRouter(prefix="/mock-graph")

    @api.post("/_inject")
    async def inject(request: Request) -> Response:
        body = await request.json()
        code = body.get("code")
        times = int(body.get("times", 1))
        if code not in ERROR_BODIES and code != "timeout":
            return JSONResponse({"error": "unknown code"}, status_code=400)
        state.inject(code, times)
        return JSONResponse({"queued": times})

    @api.get("/_sent")
    async def sent() -> Response:
        return JSONResponse({"sent": state.sent[-50:], "read_marks": state.read_marks[-50:]})

    @api.post("/_reset")
    async def reset() -> Response:
        state.sent.clear()
        state.read_marks.clear()
        state.injections.clear()
        state.counter = 0
        return JSONResponse({"ok": True})

    @api.post("/{version}/{node}/messages")
    async def messages(version: str, node: str, request: Request) -> Response:
        if _token_of(request) != state.valid_token:
            return JSONResponse({"error": ERROR_BODIES[190]}, status_code=401)
        body = await request.json()
        if body.get("status") == "read":
            state.read_marks.append(
                {
                    "phone_number_id": node,
                    "message_id": body.get("message_id"),
                    "typing": "typing_indicator" in body,
                }
            )
            return JSONResponse({"success": True})
        # injected errors hit the next real send, never a read mark, so the demo page's
        # "inject" button shows up on the outbox row of the reply that follows
        if state.injections:
            code = state.injections.popleft()
            if code == "timeout":
                await asyncio.sleep(30)
                return JSONResponse({"error": {"message": "timeout", "code": -1}}, status_code=504)
            assert isinstance(code, int)
            headers = {"retry-after": "2"} if code == 130429 else {}
            return JSONResponse(
                {"error": ERROR_BODIES[code]}, status_code=ERROR_STATUS[code], headers=headers
            )
        wamid = state.next_id()
        state.sent.append({"phone_number_id": node, "payload": body, "wamid": wamid})
        return JSONResponse(
            {
                "messaging_product": "whatsapp",
                "contacts": [{"input": body.get("to"), "wa_id": body.get("to")}],
                "messages": [{"id": wamid}],
            }
        )

    @api.get("/{version}/{node}/subscribed_apps")
    async def subscribed(version: str, node: str, request: Request) -> Response:
        if _token_of(request) != state.valid_token:
            return JSONResponse({"error": ERROR_BODIES[190]}, status_code=401)
        return JSONResponse(
            {
                "data": [
                    {
                        "whatsapp_business_api_data": {
                            "id": "demo-app",
                            "name": "Guest Concierge demo",
                        }
                    }
                ]
            }
        )

    @api.get("/{version}/{node}/message_templates")
    async def list_templates(version: str, node: str, request: Request) -> Response:
        if _token_of(request) != state.valid_token:
            return JSONResponse({"error": ERROR_BODIES[190]}, status_code=401)
        return JSONResponse({"data": state.templates, "paging": {}})

    @api.post("/{version}/{node}/message_templates")
    async def create_template(version: str, node: str, request: Request) -> Response:
        if _token_of(request) != state.valid_token:
            return JSONResponse({"error": ERROR_BODIES[190]}, status_code=401)
        body = await request.json()
        template_id = f"tpl_{len(state.templates) + 1}"
        state.templates.append({**body, "id": template_id, "status": "APPROVED"})
        return JSONResponse(
            {"id": template_id, "status": "APPROVED", "category": body.get("category", "UTILITY")}
        )

    @api.get("/media/{media_id}")
    async def media_download(media_id: str, request: Request) -> Response:
        if _token_of(request) != state.valid_token:
            return JSONResponse({"error": ERROR_BODIES[190]}, status_code=401)
        data = state.media.get(media_id)
        if data is None:
            return JSONResponse({"error": {"message": "not found", "code": 100}}, status_code=404)
        return Response(content=data, media_type="image/jpeg")

    @api.get("/{version}/{node}")
    async def node_info(version: str, node: str, request: Request) -> Response:
        if _token_of(request) != state.valid_token:
            return JSONResponse({"error": ERROR_BODIES[190]}, status_code=401)
        if node in state.media:
            import hashlib

            data = state.media[node]
            base = str(request.base_url).rstrip("/")
            return JSONResponse(
                {
                    "url": f"{base}/mock-graph/media/{node}",
                    "mime_type": "image/jpeg",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "file_size": len(data),
                    "id": node,
                }
            )
        fields = request.query_params.get("fields", "")
        info = {
            "id": node,
            "display_phone_number": "+49 69 000000",
            "verified_name": "Hotel Beispiel (demo)",
            "quality_rating": "GREEN",
            "messaging_limit_tier": "TIER_1K",
        }
        if fields:
            info = {k: v for k, v in info.items() if k == "id" or k in fields.split(",")}
        return JSONResponse(info)

    return api
