"""Widget API through the ASGI app: config, CORS matrix, sessions, messages, limits, static files."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from tests.unit.test_profile import SAMPLE

from concierge.app import create_app
from concierge.channels.whatsapp.fake import FakeGraph
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.db.session import Database
from concierge.kb.fixtures import FIXTURES_DIR, import_kb_file
from concierge.security.crypto import Keyring
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F22")

STATIC = Path(__file__).resolve().parents[2] / "src" / "concierge" / "static" / "widget"
HOTEL_ORIGIN = "https://hotel-beispiel.invalid"


@pytest.fixture
async def widget(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> tuple[httpx2.AsyncClient, Services]:
    clock.set(datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    services = build_services(ctx, llm=None, wa_port=FakeGraph())
    profile = TenantProfile.model_validate(
        dict(
            SAMPLE,
            widget={"public_key": "pk_demo", "origins": [HOTEL_ORIGIN], "primary_color": "#C22113"},
            limits={
                "widget_requests_per_ip_per_minute": 6,
                "widget_messages_per_session_per_hour": 3,
            },
        )
    )
    tenant = await services.tenants.create(profile, actor="test")
    await import_kb_file(services.kb, tenant.id, FIXTURES_DIR / "hotel_beispiel.yaml", approve=True)
    app = create_app(services, start_worker=False)
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    )
    return client, services


async def test_static_files_and_inert_loader(widget: tuple[httpx2.AsyncClient, Services]) -> None:
    client, _ = widget
    loader = await client.get("/w/loader.js")
    assert loader.status_code == 200 and loader.headers["content-type"].startswith(
        "text/javascript"
    )
    assert loader.headers["access-control-allow-origin"] == "*"
    text = loader.text
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "sessionStorage",
        "localStorage",
        "document.cookie",
    ):
        assert forbidden not in text, forbidden
    assert 'addEventListener("click"' in text and "gc-widget.js" in text
    css = await client.get("/w/gc-widget.css")
    assert (
        css.status_code == 200
        and "prefers-reduced-motion" in css.text
        and "prefers-color-scheme: dark" in css.text
    )
    js = await client.get("/w/gc-widget.js")
    assert (
        js.status_code == 200
        and 'role="log"' in js.text
        and "aria-live" in js.text
        and "Escape" in js.text
    )
    assert "localStorage" not in js.text and "document.cookie" not in js.text
    assert (await client.get("/w/nope.js")).status_code == 404
    total = sum(
        len((STATIC / name).read_text(encoding="utf-8").splitlines())
        for name in ("loader.js", "gc-widget.js", "gc-widget.css")
    )
    assert total <= 700


async def test_config_and_cors_matrix(widget: tuple[httpx2.AsyncClient, Services]) -> None:
    client, _ = widget
    same = await client.get("/w/api/config", params={"key": "pk_demo"})
    assert (
        same.status_code == 200
        and same.json()["hotel"] == "Hotel Beispiel"
        and "access-control-allow-origin" not in same.headers
    )
    allowed = await client.get(
        "/w/api/config", params={"key": "pk_demo"}, headers={"origin": HOTEL_ORIGIN}
    )
    assert (
        allowed.status_code == 200
        and allowed.headers["access-control-allow-origin"] == HOTEL_ORIGIN
        and allowed.headers["vary"] == "Origin"
    )
    own = await client.get(
        "/w/api/config", params={"key": "pk_demo"}, headers={"origin": "http://127.0.0.1:8000"}
    )
    assert own.status_code == 200
    denied = await client.get(
        "/w/api/config", params={"key": "pk_demo"}, headers={"origin": "https://evil.invalid"}
    )
    assert denied.status_code == 403 and "access-control-allow-origin" not in denied.headers
    unknown = await client.get("/w/api/config", params={"key": "pk_nope"})
    assert unknown.status_code == 404
    preflight = await client.options(
        "/w/api/message",
        params={"key": "pk_demo"},
        headers={"origin": HOTEL_ORIGIN, "access-control-request-method": "POST"},
    )
    assert (
        preflight.status_code == 204
        and preflight.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
    )
    assert (
        await client.options(
            "/w/api/message", params={"key": "pk_demo"}, headers={"origin": "https://evil.invalid"}
        )
    ).status_code == 403
    body = allowed.json()
    assert (
        body["languages"] == ["de", "en", "ru", "uk", "tr", "it"]
        and body["labels"]["uk"]["ai"] == "ШІ-асистент"
    )
    assert "KI-Assistent" in body["greeting"]["de"] and body["phone"] == "+49 69 000000"


async def test_session_message_buttons_and_limits(
    widget: tuple[httpx2.AsyncClient, Services],
) -> None:
    client, _services = widget
    headers = {"origin": HOTEL_ORIGIN}
    session = await client.post(
        "/w/api/session", json={"key": "pk_demo", "lang": "en"}, headers=headers
    )
    assert session.status_code == 200
    data = session.json()
    token = data["session"]
    assert (
        data["lang"] == "en"
        and data["messages"][0]["kind"] == "disclosure"
        and "AI assistant" in data["messages"][0]["text"]
    )
    assert re.fullmatch(r"[A-Za-z0-9_\-]{40,}", token)
    reply = await client.post(
        "/w/api/message",
        json={
            "key": "pk_demo",
            "session": token,
            "text": "Is there parking at the hotel?",
            "lang": "en",
        },
        headers=headers,
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["mode"] == "responder" and body["lang"] == "en" and not body["handoff"]
    assert len(body["messages"]) == 1 and "EUR 18" in body["messages"][0]["text"]
    assert body["messages"][0]["buttons"] == [{"id": "person", "title": "Staff"}]
    # a quick reply sends the button title; the language menu overrides detection
    button = await client.post(
        "/w/api/message",
        json={
            "key": "pk_demo",
            "session": token,
            "text": "Staff",
            "button_id": "person",
            "lang": "de",
        },
        headers=headers,
    )
    assert button.status_code == 200 and button.json()["handoff"] and button.json()["lang"] == "de"
    third = await client.post(
        "/w/api/message",
        json={"key": "pk_demo", "session": token, "text": "Hallo"},
        headers=headers,
    )
    assert third.status_code == 200
    limited = await client.post(
        "/w/api/message",
        json={"key": "pk_demo", "session": token, "text": "noch eine"},
        headers=headers,
    )
    assert limited.status_code == 429 and limited.json()["error"] == "rate_limited"
    bad = await client.post(
        "/w/api/message",
        json={"key": "pk_demo", "session": "not-a-session", "text": "Hallo"},
        headers=headers,
    )
    assert bad.status_code == 400 and bad.json()["error"] == "bad_session"
    empty = await client.post(
        "/w/api/message", json={"key": "pk_demo", "session": token, "text": "   "}, headers=headers
    )
    assert empty.status_code in {400, 429}
    assert (
        await client.post(
            "/w/api/message",
            content=b"{bad",
            headers={**headers, "content-type": "application/json"},
        )
    ).status_code in {400, 404}


async def test_ip_limit_and_stored_session_is_hashed(
    widget: tuple[httpx2.AsyncClient, Services],
) -> None:
    client, services = widget
    codes = []
    for _ in range(8):
        response = await client.get(
            "/w/api/config", params={"key": "pk_demo"}, headers={"x-forwarded-for": "203.0.113.9"}
        )
        codes.append(response.status_code)
    assert codes[:6] == [200] * 6 and codes[6:] == [429, 429]
    session = await client.post(
        "/w/api/session", json={"key": "pk_demo"}, headers={"x-forwarded-for": "203.0.113.10"}
    )
    token = session.json()["session"]
    from sqlalchemy import select

    from concierge.db.models import Guest

    async with services.ctx.db.scope() as db_session:
        guests = list(
            (await db_session.execute(select(Guest).where(Guest.channel == "web"))).scalars()
        )
    assert len(guests) == 1 and guests[0].channel_key != token and len(guests[0].channel_key) == 64
