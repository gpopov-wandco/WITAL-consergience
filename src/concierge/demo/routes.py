"""Demo pages: WhatsApp simulator and widget test page (F29).

Implements: F29, F22

Mounted only in demo mode. The simulator page posts into the webhook ingress in
process (signed with the demo app secret), drains the worker and shows the transcript
from the database next to what the mock Graph server received.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from concierge.admin.templating import templates
from concierge.db.models import Conversation, Guest, Message, MessageText
from concierge.demo.mock_graph import MockGraphState
from concierge.demo.seed import DEMO_SECRETS
from concierge.demo.wa_sim import PayloadFactory, sign
from concierge.services import Services


def build_router(services: Services, graph_state: MockGraphState | None) -> APIRouter:
    api = APIRouter(prefix="/demo")
    factory_cache: dict[str, PayloadFactory] = {}

    async def _tenant() -> Any:
        return await services.tenants.get("hotel-beispiel")

    def _factory(phone_number_id: str) -> PayloadFactory:
        factory = factory_cache.get(phone_number_id)
        if factory is None:
            factory = PayloadFactory(phone_number_id)
            factory_cache[phone_number_id] = factory
        return factory

    @api.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        tenant = await _tenant()
        return templates.TemplateResponse(
            request, "demo/index.html", {"tenant": tenant, "graph": graph_state is not None}
        )

    @api.get("/widget", response_class=HTMLResponse)
    async def widget_page(request: Request) -> Response:
        tenant = await _tenant()
        key = tenant.public_key if tenant else "pk_demo"
        return templates.TemplateResponse(
            request, "demo/widget.html", {"public_key": key, "tenant": tenant}
        )

    @api.get("/wa-sim", response_class=HTMLResponse)
    async def wa_sim_page(request: Request) -> Response:
        tenant = await _tenant()
        sender = request.query_params.get("from", "4915112345678")
        transcript = await _transcript(tenant.id, sender) if tenant else []
        return templates.TemplateResponse(
            request,
            "demo/wa_sim.html",
            {
                "tenant": tenant,
                "sender": sender,
                "transcript": transcript,
                "sent": graph_state.messages_to(sender)[-20:] if graph_state else [],
                "errors": [131047, 130429, 190],
            },
        )

    @api.post("/wa-sim/send")
    async def wa_sim_send(
        request: Request,
        sender: str = Form("4915112345678"),
        text: str = Form(""),
        kind: str = Form("text"),
        duplicate: str = Form(""),
    ) -> Response:
        tenant = await _tenant()
        if tenant is None or not tenant.phone_number_id:
            return JSONResponse({"error": "demo tenant not seeded"}, status_code=400)
        factory = _factory(tenant.phone_number_id)
        sender = "".join(ch for ch in sender if ch.isdigit()) or "4915112345678"
        if kind == "button":
            payload = factory.button(sender, "gc:person", text or "Mitarbeiter")
        elif kind == "list":
            payload = factory.list_reply(sender, "gc:parking", text or "Parken")
        elif kind == "media":
            payload = factory.media(sender)
        elif kind == "location":
            payload = factory.location(sender)
        else:
            payload = factory.text(sender, text or "Hallo")
        raw, signature = sign(payload, DEMO_SECRETS["wa_app_secret"])
        await services.ingress.ingest(raw, signature)
        if duplicate:
            await services.ingress.ingest(raw, signature)
        await services.worker.drain()
        return RedirectResponse(f"/demo/wa-sim?from={sender}", status_code=303)

    @api.post("/wa-sim/status")
    async def wa_sim_status(
        request: Request, sender: str = Form("4915112345678"), status: str = Form("delivered")
    ) -> Response:
        tenant = await _tenant()
        if tenant is None or graph_state is None:
            return RedirectResponse("/demo/wa-sim", status_code=303)
        last = graph_state.messages_to(sender)
        if last:
            factory = _factory(tenant.phone_number_id or "")
            payload = factory.status(last[-1]["wamid"], status, sender)
            raw, signature = sign(payload, DEMO_SECRETS["wa_app_secret"])
            await services.ingress.ingest(raw, signature)
            await services.worker.drain()
        return RedirectResponse(f"/demo/wa-sim?from={sender}", status_code=303)

    @api.post("/wa-sim/inject")
    async def wa_sim_inject(
        request: Request, sender: str = Form("4915112345678"), code: str = Form("131047")
    ) -> Response:
        if graph_state is not None:
            graph_state.inject(int(code) if code.isdigit() else code)
        return RedirectResponse(f"/demo/wa-sim?from={sender}", status_code=303)

    async def _transcript(tenant_id: str, sender: str) -> list[dict[str, Any]]:
        async with services.ctx.db.tenant_scope(tenant_id) as session:
            guest = await session.scalar(
                select(Guest).where(
                    Guest.tenant_id == tenant_id,
                    Guest.channel == "whatsapp",
                    Guest.channel_key == sender,
                )
            )
            if guest is None:
                return []
            rows = (
                await session.execute(
                    select(Message, MessageText, Conversation)
                    .join(MessageText, MessageText.id == Message.id)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(Conversation.guest_id == guest.id)
                    .order_by(Message.occurred_at, Message.created_at)
                )
            ).all()
        return [
            {
                "direction": m.direction,
                "text": t.text,
                "status": m.status,
                "kind": m.kind,
                "intent": m.intent or "",
                "lang": m.lang or "",
                "conversation_status": c.status,
            }
            for m, t, c in rows[-40:]
        ]

    return api
