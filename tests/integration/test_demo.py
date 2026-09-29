"""Demo mode: seeding, simulator flow through the real Graph client and the mock server, pages."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx2
import pytest
from fastapi import FastAPI

from concierge.app import create_app
from concierge.channels.whatsapp.graph import GraphClient
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.db.session import Database
from concierge.demo.mock_graph import MockGraphState
from concierge.demo.mock_graph import build_router as build_mock_router
from concierge.demo.seed import DEMO_SECRETS, DemoRefused, seed_demo
from concierge.demo.wa_sim import PayloadFactory, WaSimClient, sign
from concierge.security.crypto import Keyring
from concierge.services import Services, build_services
from concierge.settings import Settings

pytestmark = pytest.mark.feature("F29")

GUEST = "4915112345678"


@pytest.fixture
async def demo(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> tuple[httpx2.AsyncClient, Services, MockGraphState]:
    clock.set(datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    state = MockGraphState()
    mock_app = FastAPI()
    mock_app.include_router(build_mock_router(state))
    graph = GraphClient(
        version="v26.0",
        base_url="http://mock/mock-graph",
        transport=httpx2.ASGITransport(app=mock_app),
    )
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    services = build_services(ctx, llm=None, wa_port=graph, demo=True)
    result = await seed_demo(services)
    assert result.created and result.kb_rows > 150 and result.users
    again = await seed_demo(services)
    assert not again.created and again.kb_rows == 0
    app = create_app(services, start_worker=False, graph_state=state)
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    )
    return client, services, state


async def test_seed_refused_in_production(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> None:
    production = settings.model_copy(
        update={"gc_env": "production", "gc_public_base_url": "https://concierge.example.invalid"}
    )
    ctx = AppContext(settings=production, db=db, keyring=keyring, clock=clock)
    services = build_services(ctx, llm=None, wa_port=GraphClient(version="v26.0"), demo=True)
    with pytest.raises(DemoRefused):
        await seed_demo(services)


async def test_simulator_conversation_through_real_graph_client(
    demo: tuple[httpx2.AsyncClient, Services, MockGraphState],
) -> None:
    client, services, state = demo
    tenant = await services.tenants.require("hotel-beispiel")
    factory = PayloadFactory(tenant.phone_number_id or "")
    sim = WaSimClient(
        "http://testserver", DEMO_SECRETS["wa_app_secret"], transport=client._transport
    )
    response = await sim.post(factory.text(GUEST, "Gibt es einen Parkplatz?"))
    assert response.status_code == 200 and response.headers["x-events"] == "1"
    await services.worker.drain()
    sent = state.messages_to(GUEST)
    assert len(sent) == 3, [m["payload"] for m in sent]
    assert "KI-Assistent" in sent[0]["payload"]["text"]["body"]
    assert "Demo-Modus" in sent[1]["payload"]["text"]["body"]
    assert "18 EUR" in sent[2]["payload"]["interactive"]["body"]["text"]
    assert state.read_marks and state.read_marks[0]["typing"] is True
    # statuses flow back through the same signed webhook path
    await sim.post(factory.status(sent[2]["wamid"], "delivered", GUEST))
    await services.worker.drain()
    listing = await sim.sent()
    assert len(listing["sent"]) == 3
    # error injection through the mock server's control endpoint
    await sim.inject(131047)
    await sim.post(factory.text(GUEST, "Wann gibt es Frühstück?"))
    await services.worker.drain()
    counts = await services.outbox.counts(tenant.id)
    # the breakfast answer hits the injected error; the breakfast upsell offer that follows is sent
    assert counts.get("failed") == 1 and counts.get("sent") == 4
    await sim.aclose()


async def test_demo_pages_and_form_flow(
    demo: tuple[httpx2.AsyncClient, Services, MockGraphState],
) -> None:
    client, _services, state = demo
    index = await client.get("/demo/")
    assert (
        index.status_code == 200 and "Hotel Beispiel" in index.text and "Demo-Modus" in index.text
    )
    widget = await client.get("/demo/widget")
    assert (
        widget.status_code == 200
        and 'data-key="pk_demo"' in widget.text
        and "/w/loader.js" in widget.text
    )
    page = await client.get("/demo/wa-sim")
    assert page.status_code == 200 and "Noch keine Nachrichten" in page.text
    sent = await client.post(
        "/demo/wa-sim/send",
        data={
            "sender": GUEST,
            "text": "Wie komme ich vom Flughafen zum Hotel?",
            "kind": "text",
            "duplicate": "1",
        },
    )
    assert sent.status_code == 303
    page = await client.get("/demo/wa-sim", params={"from": GUEST})
    assert page.status_code == 200 and "S8" in page.text and "wamid.mock." in page.text
    assert page.text.count("Wie komme ich vom Flughafen") == 1
    status = await client.post("/demo/wa-sim/status", data={"sender": GUEST, "status": "read"})
    assert status.status_code == 303
    inject = await client.post("/demo/wa-sim/inject", data={"sender": GUEST, "code": "190"})
    assert inject.status_code == 303 and list(state.injections) == [190]
    button = await client.post(
        "/demo/wa-sim/send", data={"sender": GUEST, "text": "Mitarbeiter", "kind": "button"}
    )
    assert button.status_code == 303
    page = await client.get("/demo/wa-sim", params={"from": GUEST})
    assert "needs_staff" in page.text
    assert (await client.get("/static/admin.css")).status_code == 200
    assert (await client.get("/static/vendor/htmx.min.js")).status_code == 200
    raw, signature = sign(PayloadFactory("demo-phone-number-id").text(GUEST, "x"), "wrong-secret")
    assert (
        await client.post("/wa/webhook", content=raw, headers={"x-hub-signature-256": signature})
    ).status_code == 403
