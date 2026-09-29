"""Honest upsells (F16): the rule table and golden conversations through the Brain."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from concierge.agent.brain import BrainResult
from concierge.agent.texts import guest_texts
from concierge.agent.upsells import UpsellContext, UpsellPolicy
from concierge.channels.whatsapp.fake import FakeGraph
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.conversations.service import Inbound
from concierge.db.models import Conversation, GuestRequest, Tenant
from concierge.db.session import Database
from concierge.demo.seed import seed_demo
from concierge.kb.store import KbView
from concierge.security.crypto import Keyring
from concierge.services import Services, build_services
from concierge.settings import Settings

pytestmark = pytest.mark.feature("F16")

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


def _conversation(**kw: Any) -> Conversation:
    base: dict[str, Any] = {
        "tenant_id": "t",
        "guest_id": "g",
        "channel": "whatsapp",
        "meta": {},
        "upsell_offered": False,
    }
    base.update(kw)
    return Conversation(**base)


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (UpsellContext(_conversation(), "A", "breakfast", False, None), "ok"),
        (UpsellContext(_conversation(), "A", "checkout", False, "P3"), "ok"),
        (UpsellContext(_conversation(), "Q", "breakfast", False, None), "not_an_answer"),
        (UpsellContext(_conversation(), "A", "parking", False, None), "topic_not_relevant"),
        (
            UpsellContext(_conversation(upsell_offered=True), "A", "breakfast", False, None),
            "already_offered",
        ),
        (
            UpsellContext(
                _conversation(meta={"upsell_declined": True}), "A", "breakfast", False, None
            ),
            "declined_before",
        ),
        (UpsellContext(_conversation(), "A", "breakfast", True, None), "handoff_or_blocked_topic"),
        (UpsellContext(_conversation(), "A", "complaint", False, None), "topic_not_relevant"),
        (UpsellContext(_conversation(), "A", "breakfast", False, "P1"), "open_urgent_handoff"),
        (
            UpsellContext(
                _conversation(meta={"complaint_open": True}), "A", "breakfast", False, None
            ),
            "complaint_thread",
        ),
    ],
)
def test_rule_table(ctx: UpsellContext, expected: str) -> None:
    ok, reason = UpsellPolicy.allowed(ctx)
    assert reason == expected and ok == (expected == "ok")


def test_offer_needs_price_includes_and_text() -> None:
    policy = UpsellPolicy(kb=None, texts=guest_texts())  # type: ignore[arg-type]
    good = KbView(
        "upsells.breakfast",
        "de",
        "Frühstück für 16 EUR pro Person.",
        {"id": "breakfast", "price_cents": 1600, "includes": "buffet", "conditions": "by 20:00"},
        "authoritative_kb",
        "public",
        1,
        "e1",
    )
    offer = policy.offer_from_view(good, "de")
    assert (
        offer is not None
        and offer.price_cents == 1600
        and offer.text.endswith("erst dann gilt sie.")
    )
    assert [b.id for b in offer.buttons] == ["upsell_yes", "upsell_no"] and offer.buttons[
        0
    ].title == "Ja, anfragen"
    assert (
        policy.offer_from_view(
            KbView(
                "upsells.x", "de", "text", {"includes": "x"}, "authoritative_kb", "public", 1, "e"
            ),
            "de",
        )
        is None
    )
    assert (
        policy.offer_from_view(
            KbView(
                "upsells.x",
                "de",
                "text",
                {"price_cents": 5, "includes": ""},
                "authoritative_kb",
                "public",
                1,
                "e",
            ),
            "de",
        )
        is None
    )
    assert "knapp" not in offer.text.lower() and "nur noch" not in offer.text.lower()


class Chat:
    def __init__(self, services: Services, tenant: Tenant) -> None:
        self.services, self.tenant = services, tenant

    async def say(
        self, text: str, *, key: str = "4915170000001", button_id: str | None = None
    ) -> BrainResult:
        return await self.services.brain.respond(
            self.tenant,
            Inbound(
                channel="whatsapp",
                channel_key=key,
                text=text,
                kind="button" if button_id else "text",
                external_id=None,
                phone=f"+{key}",
                button_id=button_id,
            ),
        )


@pytest.fixture
async def chat(db: Database, settings: Settings, keyring: Keyring, clock: FakeClock) -> Chat:
    clock.set(NOW)
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    services = build_services(ctx, llm=None, wa_port=FakeGraph(), demo=True)
    await seed_demo(services)
    return Chat(services, await services.tenants.require("hotel-beispiel"))


async def test_golden_offer_then_yes_records_a_request(chat: Chat) -> None:
    result = await chat.say("Wann gibt es Frühstück?")
    texts = [m.text for m in result.messages]
    assert any(e == "upsell_offered:breakfast" for e in result.events)
    offer = result.messages[-1]
    assert (
        offer.intent == "A:upsell"
        and "16 EUR" in offer.text
        and "bei der Rezeption anfragen" in offer.text
    )
    assert [b.id for b in offer.buttons] == ["upsell_yes", "upsell_no"]
    assert all("knapp" not in t.lower() for t in texts)
    yes = await chat.say("Ja, anfragen", button_id="upsell_yes")
    assert any(e.startswith("upsell_requested:breakfast:") for e in yes.events)
    assert (
        "nicht bestätigt" in yes.messages[-1].text or "erst als bestätigt" in yes.messages[-1].text
    )
    async with chat.services.ctx.db.scope() as session:
        rows = list(
            (
                await session.execute(select(GuestRequest).where(GuestRequest.kind == "upsell"))
            ).scalars()
        )
    assert (
        len(rows) == 1 and rows[0].details["upsell_id"] == "breakfast" and rows[0].status == "open"
    )
    again = await chat.say("Und Frühstück am Sonntag?")
    assert not any(e.startswith("upsell_offered") for e in again.events)


async def test_golden_no_and_complaint_threads_get_no_offer(chat: Chat) -> None:
    first = await chat.say("Bis wann ist Check-out?", key="4915170000002")
    assert "upsell_offered:late_checkout" in first.events and "20 EUR" in first.messages[-1].text
    no = await chat.say("Nein, danke", key="4915170000002", button_id="upsell_no")
    assert (
        "upsell_declined:late_checkout" in no.events
        and no.messages[-1].text == "Alles klar, kein Problem."
    )
    again = await chat.say("Wann gibt es Frühstück?", key="4915170000002")
    assert not any(e.startswith("upsell_offered") for e in again.events)
    complaint = await chat.say(
        "Die Dusche ist kaputt und das Zimmer ist schmutzig", key="4915170000003"
    )
    assert complaint.handoff_id is not None
    later = await chat.say("Wann gibt es Frühstück?", key="4915170000003")
    assert not any(e.startswith("upsell_offered") for e in later.events)
