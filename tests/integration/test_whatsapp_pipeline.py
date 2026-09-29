"""End-to-end WhatsApp flow through the ASGI app with the fake Graph (offline)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select
from tests.unit.test_profile import SAMPLE

from concierge.app import create_app
from concierge.channels.whatsapp.fake import FakeGraph
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.db.models import Message, OperatorAlert, Outbox, Template, UsageEvent, WebhookEvent
from concierge.db.session import Database
from concierge.kb.fixtures import FIXTURES_DIR, import_kb_file
from concierge.security.crypto import Keyring
from concierge.security.signatures import sign_meta
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F18")

PN = "PN-100"
GUEST = "4915112345678"
APP_SECRET = "app-secret-for-tests"
VERIFY_TOKEN = "verify-token-for-tests"


class Pipeline:
    def __init__(
        self, services: Services, tenant_id: str, graph: FakeGraph, clock: FakeClock
    ) -> None:
        self.services = services
        self.tenant_id = tenant_id
        self.graph = graph
        self.clock = clock
        self.app = create_app(services, start_worker=False)
        self.http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url="http://testserver"
        )
        self.counter = 0

    def text_event(
        self,
        text: str,
        *,
        from_id: str = GUEST,
        wamid: str | None = None,
        ts: int | None = None,
        name: str = "Anna Muster",
    ) -> dict[str, Any]:
        self.counter += 1
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "4969000000",
                                    "phone_number_id": PN,
                                },
                                "contacts": [{"profile": {"name": name}, "wa_id": from_id}],
                                "messages": [
                                    {
                                        "from": from_id,
                                        "id": wamid or f"wamid.in.{self.counter}",
                                        "timestamp": str(ts or int(self.clock.now().timestamp())),
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

    def status_event(self, wamid: str, status: str, *, billable: bool = True) -> dict[str, Any]:
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "4969000000",
                                    "phone_number_id": PN,
                                },
                                "statuses": [
                                    {
                                        "id": wamid,
                                        "status": status,
                                        "timestamp": str(int(self.clock.now().timestamp())),
                                        "recipient_id": GUEST,
                                        "conversation": {
                                            "id": "conv-meta-1",
                                            "origin": {"type": "service"},
                                        },
                                        "pricing": {
                                            "billable": billable,
                                            "pricing_model": "PMP",
                                            "category": "service",
                                            "type": "regular",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

    async def post(
        self, payload: dict[str, Any], *, secret: str = APP_SECRET, sign: bool = True
    ) -> httpx2.Response:
        raw = json.dumps(payload).encode()
        headers = {"content-type": "application/json"}
        if sign:
            headers["x-hub-signature-256"] = sign_meta(raw, secret)
        return await self.http.post("/wa/webhook", content=raw, headers=headers)

    async def drain(self) -> int:
        return await self.services.worker.drain()

    async def outbox_rows(self) -> list[Outbox]:
        async with self.services.ctx.db.scope() as session:
            return list(
                (await session.execute(select(Outbox).order_by(Outbox.created_at))).scalars()
            )


@pytest.fixture
async def pipeline(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> Pipeline:
    clock.set(datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    graph = FakeGraph()
    services = build_services(ctx, llm=None, wa_port=graph, demo=False)
    profile = TenantProfile.model_validate(
        dict(
            SAMPLE,
            whatsapp={
                "waba_id": "WABA1",
                "phone_number_id": PN,
                "display_phone_number": "+49 69 000000",
            },
        )
    )
    tenant = await services.tenants.create(profile, actor="test")
    await import_kb_file(services.kb, tenant.id, FIXTURES_DIR / "hotel_beispiel.yaml", approve=True)
    for name, value in (
        ("wa_app_secret", APP_SECRET),
        ("wa_verify_token", VERIFY_TOKEN),
        ("wa_access_token", "EAA-test-token"),
    ):
        await services.secrets.set(tenant.id, name, value, actor="test")
    return Pipeline(services, tenant.id, graph, clock)


async def test_verification_challenge(pipeline: Pipeline) -> None:
    ok = await pipeline.http.get(
        "/wa/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "12345",
        },
    )
    assert (
        ok.status_code == 200
        and ok.text == "12345"
        and ok.headers["content-type"].startswith("text/plain")
    )
    bad = await pipeline.http.get(
        "/wa/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
    )
    assert bad.status_code == 403
    assert (await pipeline.http.get("/wa/webhook")).status_code == 403


async def test_signature_required_and_verified_before_parsing(pipeline: Pipeline) -> None:
    payload = pipeline.text_event("Hallo")
    assert (await pipeline.post(payload, sign=False)).status_code == 403
    assert (await pipeline.post(payload, secret="other")).status_code == 403
    raw = b"{not json"
    response = await pipeline.http.post(
        "/wa/webhook", content=raw, headers={"x-hub-signature-256": sign_meta(raw, APP_SECRET)}
    )
    assert response.status_code == 400
    ok = await pipeline.post(payload)
    assert ok.status_code == 200 and ok.headers["x-events"] == "1"
    duplicate = await pipeline.post(payload)
    assert duplicate.status_code == 200 and duplicate.headers["x-events"] == "0"
    unknown_number = pipeline.text_event("Hallo")
    unknown_number["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "PN-OTHER"
    assert (await pipeline.post(unknown_number)).headers["x-events"] == "0"
    async with pipeline.services.ctx.db.scope() as session:
        events = list((await session.execute(select(WebhookEvent))).scalars())
    assert (
        len(events) == 1
        and events[0].tenant_id == pipeline.tenant_id
        and events[0].status == "received"
    )


async def test_end_to_end_conversation_with_disclosure_reply_and_statuses(
    pipeline: Pipeline,
) -> None:
    payload = pipeline.text_event("Gibt es einen Parkplatz?")
    await pipeline.post(payload)
    await pipeline.post(payload)  # exact duplicate webhook (same wamid) is stored once
    processed = await pipeline.drain()
    assert processed >= 3  # inbound job plus outbox sends
    sent = pipeline.graph.messages_to(GUEST)
    assert len(sent) == 2, [m.text for m in sent]
    assert "KI-Assistent" in sent[0].text and "Hotel Beispiel" in sent[0].text
    assert "18 EUR" in sent[1].text and sent[1].payload["type"] == "interactive"
    assert sent[1].payload["interactive"]["action"]["buttons"][0]["reply"]["id"] == "gc:person"
    assert pipeline.graph.read_marks == [(PN, "wamid.in.1", True)]
    rows = await pipeline.outbox_rows()
    assert [r.state for r in rows] == ["sent", "sent"] and all(r.provider_message_id for r in rows)
    async with pipeline.services.ctx.db.scope() as session:
        outbound = list(
            (
                await session.execute(
                    select(Message).where(Message.direction == "out").order_by(Message.occurred_at)
                )
            ).scalars()
        )
        events = list((await session.execute(select(WebhookEvent))).scalars())
    assert [m.wamid for m in outbound] == [r.provider_message_id for r in rows]
    assert all(e.status == "processed" for e in events)
    # status webhooks: delivered, then an out-of-order sent, then read; usage once per status
    wamid = rows[1].provider_message_id or ""
    for status in ("delivered", "sent", "read", "read"):
        await pipeline.post(pipeline.status_event(wamid, status))
        await pipeline.drain()
    async with pipeline.services.ctx.db.scope() as session:
        message = (
            await session.execute(select(Message).where(Message.wamid == wamid))
        ).scalar_one()
        usage = list((await session.execute(select(UsageEvent))).scalars())
    assert message.status == "read"
    assert sorted(u.ref for u in usage) == [
        f"wa:{wamid}:delivered",
        f"wa:{wamid}:read",
        f"wa:{wamid}:sent",
    ]
    assert all(
        u.kind == "whatsapp" and u.category == "service" and u.meta["billable"] for u in usage
    )
    # a second guest message continues the same conversation without a second disclosure
    await pipeline.post(pipeline.text_event("Und wann gibt es Frühstück?"))
    await pipeline.drain()
    sent = pipeline.graph.messages_to(GUEST)
    assert len(sent) == 3 and "07:00" in sent[2].text


async def test_window_closed_throttle_and_token_invalid(pipeline: Pipeline) -> None:
    pipeline.graph.inject_error(131047)
    pipeline.graph.inject_error(131047)
    await pipeline.post(pipeline.text_event("Gibt es einen Parkplatz?"))
    await pipeline.drain()
    rows = await pipeline.outbox_rows()
    assert [r.state for r in rows] == ["failed", "failed"] and rows[0].error_code == "131047"
    pipeline.graph.inject_error(130429, times=5)
    await pipeline.post(pipeline.text_event("Wann gibt es Frühstück?"))
    await pipeline.drain()
    row = (await pipeline.outbox_rows())[-1]
    assert row.state == "queued" and row.attempts == 1 and row.error_code == "130429"
    for _ in range(5):
        pipeline.clock.advance(timedelta(minutes=16))
        await pipeline.drain()
    row = (await pipeline.outbox_rows())[-1]
    assert row.state == "dead" and row.attempts == 5
    async with pipeline.services.ctx.db.scope() as session:
        alerts = {a.kind for a in (await session.execute(select(OperatorAlert))).scalars()}
    assert "outbox_dead" in alerts
    pipeline.graph.inject_error(190)
    before = len(pipeline.graph.sent)
    await pipeline.post(pipeline.text_event("Wie ist die Adresse?"))
    await pipeline.drain()
    rows = await pipeline.outbox_rows()
    assert rows[-1].state == "failed" and rows[-1].error_code == "190"
    assert pipeline.tenant_id in pipeline.services.outbox.token_invalid_tenants()
    assert len(pipeline.graph.sent) == before
    async with pipeline.services.ctx.db.scope() as session:
        alerts = {a.kind for a in (await session.execute(select(OperatorAlert))).scalars()}
    assert "token_invalid" in alerts


async def test_interrupted_send_is_never_resent_and_timeout_is_unknown(pipeline: Pipeline) -> None:
    await pipeline.post(pipeline.text_event("Gibt es einen Parkplatz?"))
    # process only the inbound job, then simulate a worker killed between `sending` and `sent`
    await pipeline.services.worker.run_once()
    rows = await pipeline.outbox_rows()
    assert rows and all(r.state == "queued" for r in rows)
    async with pipeline.services.ctx.db.scope() as session:
        first = await session.get(Outbox, rows[0].id)
        assert first is not None
        first.state = "sending"
        await session.commit()
    sent_before = len(pipeline.graph.sent)
    await pipeline.drain()
    rows = await pipeline.outbox_rows()
    assert rows[0].state == "unknown" and rows[1].state == "sent"
    assert len(pipeline.graph.sent) == sent_before + 1
    # a status for the unknown row's recipient reconciles it
    await pipeline.post(pipeline.status_event("wamid.reconciled", "delivered"))
    await pipeline.drain()
    rows = await pipeline.outbox_rows()
    assert rows[0].state == "sent" and rows[0].provider_message_id == "wamid.reconciled"
    pipeline.graph.inject_error("timeout", times=2)
    await pipeline.post(pipeline.text_event("Danke"))
    await pipeline.drain()
    assert (await pipeline.outbox_rows())[-1].state == "unknown"


async def test_media_button_and_template_status(pipeline: Pipeline) -> None:
    pipeline.graph.add_media("media-1", b"\xff\xd8\xff" * 100, "image/jpeg")
    payload = pipeline.text_event("x")
    payload["entry"][0]["changes"][0]["value"]["messages"] = [
        {
            "from": GUEST,
            "id": "wamid.media",
            "timestamp": str(int(pipeline.clock.now().timestamp())),
            "type": "image",
            "image": {"id": "media-1", "mime_type": "image/jpeg", "sha256": "abc"},
        }
    ]
    await pipeline.post(payload)
    await pipeline.drain()
    sent = pipeline.graph.messages_to(GUEST)
    assert any("Sprachnachrichten" in m.text or "voice messages" in m.text for m in sent)
    button = pipeline.text_event("x")
    button["entry"][0]["changes"][0]["value"]["messages"] = [
        {
            "from": GUEST,
            "id": "wamid.btn",
            "timestamp": str(int(pipeline.clock.now().timestamp())),
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "gc:parking", "title": "Parken"},
            },
        }
    ]
    await pipeline.post(button)
    await pipeline.drain()
    assert "18 EUR" in pipeline.graph.messages_to(GUEST)[-1].text
    async with pipeline.services.ctx.db.scope() as session:
        session.add(
            Template(
                tenant_id=pipeline.tenant_id,
                name="gc_prearrival_2d",
                lang="de",
                category="utility",
                body="Hallo {{1}}",
                status="PENDING",
            )
        )
        await session.commit()
    update = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "message_template_status_update",
                        "value": {
                            "metadata": {"phone_number_id": PN},
                            "event": "APPROVED",
                            "message_template_id": 55,
                            "message_template_name": "gc_prearrival_2d",
                            "message_template_language": "de",
                        },
                    }
                ],
            }
        ],
    }
    assert (await pipeline.post(update)).headers["x-events"] == "1"
    await pipeline.drain()
    async with pipeline.services.ctx.db.scope() as session:
        template = (await session.execute(select(Template))).scalar_one()
    assert template.status == "APPROVED" and template.meta_template_id == "55"


async def test_health_endpoints_and_headers(pipeline: Pipeline) -> None:
    health = await pipeline.http.get("/healthz")
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert (
        "default-src 'self'" in health.headers["content-security-policy"]
        and "unsafe-inline" not in health.headers["content-security-policy"]
    )
    assert health.headers["x-content-type-options"] == "nosniff"
    ready = await pipeline.http.get("/readyz")
    assert ready.status_code == 503 and ready.json()["checks"]["worker"] == "not started"
    await pipeline.services.worker.run_once()
    ready = await pipeline.http.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"] == {
        "database": "ok",
        "migration": "ok",
        "worker": "ok",
        "master_key": "ok",
    }
    pipeline.clock.advance(timedelta(seconds=40))
    assert (await pipeline.http.get("/readyz")).status_code == 503
