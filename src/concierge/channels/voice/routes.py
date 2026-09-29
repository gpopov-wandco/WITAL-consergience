"""HTTP and WebSocket endpoints for the voice providers (F26).

Implements: F26, F31, 6.3

Tenant resolution never trusts the caller: Retell webhooks resolve by agent id or
dialled number after the signature check; the custom-LLM socket carries the tenant's
public key in its path (set at provisioning) and the call id; Twilio requests are
signature-checked against the public URL and resolve by the dialled number.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from concierge.channels.voice import retell, twilio_cr
from concierge.channels.voice.postcall import PostCall
from concierge.channels.voice.retell import RetellProtocol
from concierge.channels.voice.session import VoiceEngine
from concierge.channels.voice.twilio_cr import TwilioRelayProtocol
from concierge.clock import Clock
from concierge.db.models import Tenant
from concierge.log import get_logger
from concierge.settings import Settings
from concierge.tenants.service import TenantService

log = get_logger(__name__)


class StarletteSocket:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    async def receive(self) -> dict[str, Any] | None:
        try:
            raw = await self.websocket.receive_text()
        except WebSocketDisconnect:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def send(self, message: dict[str, Any]) -> None:
        await self.websocket.send_text(json.dumps(message, ensure_ascii=False))


def build_router(
    *,
    engine: VoiceEngine,
    postcall: PostCall,
    tenants: TenantService,
    settings: Settings,
    clock: Clock,
) -> APIRouter:
    api = APIRouter(prefix="/voice")
    public = settings.public_base_url()

    async def _tenant_for_call(agent_id: str | None, number: str | None) -> Tenant | None:
        return await tenants.by_voice(agent_id=agent_id, number=number)

    # ------------------------------------------------------------- retell
    @api.post("/retell/webhook")
    async def retell_webhook(request: Request) -> Response:
        body = await request.body()
        api_key = settings.retell_api_key.get_secret_value()
        now_ms = int(clock.now().timestamp() * 1000)
        if not retell.verify_signature(
            body, request.headers.get("x-retell-signature"), api_key, now_ms=now_ms
        ):
            return Response(status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        event = retell.parse_event(payload)
        if event is None:
            return Response(content='{"ignored":true}', media_type="application/json")
        tenant = await _tenant_for_call(event.start.agent_id, event.start.to_number)
        if tenant is None:
            log.warning("voice.retell_unknown_tenant", agent=event.start.agent_id or "-")
            return Response(status_code=404)
        if event.kind == "call_ended" and event.end is not None:
            await postcall.end_by_provider(tenant, event.end)
        return Response(content='{"ok":true}', media_type="application/json")

    @api.websocket("/retell/llm/{public_key}/{call_id}")
    async def retell_llm(websocket: WebSocket, public_key: str, call_id: str) -> None:
        tenant = await tenants.by_public_key(public_key)
        if tenant is None or not call_id or len(call_id) > 128:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        await RetellProtocol(engine, tenant, StarletteSocket(websocket), call_id).run()

    # ------------------------------------------------------------- twilio
    async def _twilio_ok(request: Request, form: dict[str, str], body: bytes) -> bool:
        token = settings.twilio_auth_token.get_secret_value()
        url = public + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        return twilio_cr.verify_signature(
            token, url, form, request.headers.get("x-twilio-signature"), body=body
        )

    async def _form(request: Request) -> tuple[dict[str, str], bytes]:
        body = await request.body()
        form = await request.form()
        return {k: str(v) for k, v in form.items() if isinstance(v, str)}, body

    @api.post("/twilio/incoming")
    async def twilio_incoming(request: Request) -> Response:
        form, body = await _form(request)
        if not await _twilio_ok(request, form, body):
            return Response(status_code=403)
        tenant = await _tenant_for_call(None, form.get("To"))
        if tenant is None:
            return Response(twilio_cr.hangup_twiml(), media_type="application/xml", status_code=404)
        profile = tenants.profile_of(tenant)
        ws_base = public.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        twiml = twilio_cr.relay_twiml(
            websocket_url=f"{ws_base}/voice/twilio/relay/{tenant.public_key}",
            action_url=f"{public}/voice/twilio/handoff",
            language=profile.default_language,
            status_callback=f"{public}/voice/twilio/status",
        )
        return Response(twiml, media_type="application/xml")

    @api.websocket("/twilio/relay/{public_key}")
    async def twilio_relay(websocket: WebSocket, public_key: str) -> None:
        tenant = await tenants.by_public_key(public_key)
        if tenant is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        await TwilioRelayProtocol(engine, tenant, StarletteSocket(websocket)).run()

    @api.post("/twilio/handoff")
    async def twilio_handoff(request: Request) -> Response:
        form, body = await _form(request)
        if not await _twilio_ok(request, form, body):
            return Response(status_code=403)
        data = twilio_cr.parse_handoff_data(form.get("HandoffData"))
        number = str(data.get("transfer", "")) if data else ""
        if number:
            return Response(twilio_cr.dial_twiml(number), media_type="application/xml")
        return Response(twilio_cr.hangup_twiml(), media_type="application/xml")

    @api.post("/twilio/status")
    async def twilio_status(request: Request) -> Response:
        form, body = await _form(request)
        if not await _twilio_ok(request, form, body):
            return Response(status_code=403)
        if form.get("CallStatus") not in {"completed", "busy", "failed", "no-answer", "canceled"}:
            return Response(status_code=204)
        tenant = await _tenant_for_call(None, form.get("To"))
        if tenant is None:
            return Response(status_code=404)
        try:
            duration = int(form.get("CallDuration", "0") or 0)
        except ValueError:
            duration = 0
        from concierge.channels.voice.port import CallEnd

        await postcall.end_by_provider(
            tenant,
            CallEnd(
                provider="twilio_cr",
                call_id=form.get("CallSid", ""),
                duration_s=duration,
                disconnect_reason=form.get("CallStatus", ""),
            ),
        )
        return Response(status_code=204)

    return api
