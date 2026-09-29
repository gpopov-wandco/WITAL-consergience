"""The real tool backends (F02, F07, F10, F11, F12) behind the Brain and the responder."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from concierge.agent.brain import BrainResult
from concierge.channels.whatsapp.fake import FakeGraph
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.conversations.service import Inbound
from concierge.db.models import CallbackTask, GuestRequest, Handoff, Reservation, Tenant
from concierge.db.session import Database
from concierge.demo.seed import seed_demo
from concierge.llm.port import LLMRequest, ToolResult
from concierge.llm.scripted import ScriptedLLM, Step
from concierge.security.crypto import Keyring
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F02")

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
GUEST_PHONE = "4915112345678"


def reply(
    text: str, intent: str = "A:availability", refs: list[str] | None = None
) -> dict[str, Any]:
    return {
        "reply_text": text,
        "language": "de",
        "intent": intent,
        "handoff": {"needed": False, "priority": "none", "reason": ""},
        "fact_refs": refs or ["check_availability"],
        "confidence": 0.9,
        "buttons": [],
    }


class Stack:
    def __init__(
        self, services: Services, tenant: Tenant, llm: ScriptedLLM | None, clock: FakeClock
    ) -> None:
        self.services, self.tenant, self.llm, self.clock = services, tenant, llm, clock

    async def say(self, text: str, key: str = GUEST_PHONE) -> BrainResult:
        return await self.services.brain.respond(
            self.tenant,
            Inbound(
                channel="whatsapp",
                channel_key=key,
                text=text,
                kind="text",
                external_id=None,
                phone=f"+{key}",
            ),
        )

    def tool_results(self) -> list[dict[str, Any]]:
        assert self.llm is not None
        out: list[dict[str, Any]] = []
        # the last request carries the whole tool history of the turn
        for message in self.llm.requests[-1].messages:
            for part in message.content:
                if isinstance(part, ToolResult):
                    out.append(json.loads(part.content))
        return out


async def _stack(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock, llm: ScriptedLLM | None
) -> Stack:
    clock.set(NOW)
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    services = build_services(ctx, llm=llm, wa_port=FakeGraph(), demo=llm is None)
    await seed_demo(services)
    tenant = await services.tenants.require("hotel-beispiel")
    return Stack(services, tenant, llm, clock)


async def test_check_availability_tool_returns_manual_quotes(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> None:
    llm = ScriptedLLM(
        [
            Step.tools(
                (
                    "check_availability",
                    {
                        "arrival": "2026-10-06",
                        "departure": "2026-10-08",
                        "adults": 2,
                        "children": 0,
                        "room_type": "",
                    },
                )
            ),
            Step.final(
                reply(
                    "Laut unserem Kalender (Stand 29.09.2026) ist ein Doppelzimmer ab 120 EUR pro Nacht verfügbar, 240 EUR für 2 Nächte. Preise sind unverbindlich; die Buchung erfolgt über den Buchungslink oder die Rezeption.",
                    "P:availability",
                )
            ),
        ]
    )
    stack = await _stack(db, settings, keyring, clock, llm)
    result = await stack.say("Haben Sie vom 6. bis 8. Oktober ein Doppelzimmer für 2 Personen?")
    assert result.mode == "model" and "tool:check_availability" in result.events
    tool = stack.tool_results()[0]
    assert tool["source"] == "manual" and tool["status"] == "available" and tool["binding"] is False
    dz = next(q for q in tool["quotes"] if q["room_code"] == "dz")
    assert dz["per_night"] == "120" and dz["total"] == "240" and "units_left" not in dz
    assert tool["wording"].startswith("Laut unserem Kalender (Stand 29.09.2026)")
    assert tool["booking_url"].startswith(
        "https://example.invalid/book?from=2026-10-06&to=2026-10-08&adults=2"
    )
    assert "120 EUR" in result.messages[-1].text
    second: LLMRequest = llm.requests[1]
    assert isinstance(second.messages[-1].content[0], ToolResult)


async def test_verify_then_eta_request_link_and_callback(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> None:
    arrival = (date(2026, 9, 29) + timedelta(days=3)).isoformat()
    llm = ScriptedLLM(
        [
            Step.tools(("set_eta", {"time": "19:30"})),
            Step.tools(
                (
                    "verify_reservation",
                    {"reference": "RES-1001", "surname": "Guest", "arrival": arrival},
                )
            ),
            Step.tools(
                ("set_eta", {"time": "19:30"}),
                (
                    "get_booking_link",
                    {
                        "arrival": "2026-10-06",
                        "departure": "2026-10-08",
                        "adults": 2,
                        "children": 0,
                        "room_type": "dz",
                    },
                ),
            ),
            Step.tools(
                ("create_request", {"kind": "early_checkin", "details": "Ankunft gegen 11 Uhr"}),
                ("request_callback", {"reason": "Rechnungsfrage"}),
            ),
            Step.final(
                reply(
                    "Danke, ich habe 19:30 Uhr als Ankunftszeit notiert. Ihre Anfrage ist notiert und erst bestätigt, wenn die Rezeption antwortet.",
                    "A:eta",
                    ["set_eta"],
                )
            ),
        ]
    )
    stack = await _stack(db, settings, keyring, clock, llm)
    result = await stack.say("Ich komme um 19:30, Buchung RES-1001, Name Guest")
    assert result.mode == "model"
    tools = stack.tool_results()
    assert tools[0]["error"] is True and "verify_reservation" in tools[0]["instruction"]
    assert tools[1]["verified"] is True and tools[1]["confirmation_number"] == "RES-1001"
    assert tools[2]["status"] == "stored" and tools[2]["eta"] == "19:30"
    assert tools[3]["url"].startswith("https://example.invalid/book?from=2026-10-06")
    assert tools[4]["status"] == "recorded" and tools[5]["status"] == "recorded"
    async with stack.services.ctx.db.scope() as session:
        reservation = await session.scalar(
            select(Reservation).where(Reservation.res_id == "RES-1001")
        )
        requests = list(
            (
                await session.execute(select(GuestRequest).order_by(GuestRequest.created_at))
            ).scalars()
        )
        callbacks = list((await session.execute(select(CallbackTask))).scalars())
    assert (
        reservation is not None
        and reservation.eta == time(19, 30)
        and reservation.eta_set_at == NOW
    )
    assert reservation.guest_id == result.guest_id
    assert sorted(r.kind for r in requests) == ["early_checkin", "eta"]
    early = next(r for r in requests if r.kind == "early_checkin")
    assert (
        early.conversation_id == result.conversation_id and early.reservation_id == reservation.id
    )
    assert len(callbacks) == 1 and callbacks[0].phone_bidx == keyring.blind_index(
        f"+{GUEST_PHONE}", stack.tenant.id, "phone"
    )
    conversation = await stack.services.conversations.get(stack.tenant.id, result.conversation_id)
    assert conversation is not None and conversation.verified_reservation_id == reservation.id


async def test_responder_path_uses_manual_inventory_and_booking_link(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> None:
    stack = await _stack(db, settings, keyring, clock, None)
    result = await stack.say("Haben Sie vom 06.10.2026 bis 08.10.2026 ein Zimmer für 2 Erwachsene?")
    assert result.mode == "responder"
    text = result.messages[-1].text
    assert (
        text.startswith("Laut unserem Kalender (Stand 29.09.2026)")
        and "Doppelzimmer: verfügbar ab 120 EUR" in text
    )
    assert "https://example.invalid/book?from=2026-10-06&to=2026-10-08&adults=2" in text
    assert result.handoff_id is None and result.messages[-1].intent == "P:availability"
    asked = await stack.say(
        "Wie viele Zimmer sind noch frei vom 06.10.2026 bis 08.10.2026 für 2 Erwachsene?"
    )
    assert "Noch 8 frei" in asked.messages[-1].text


async def test_responder_without_live_data_hands_over_with_window(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> None:
    stack = await _stack(db, settings, keyring, clock, None)
    profile = TenantProfile.model_validate(stack.tenant.profile)
    profile = profile.model_copy(
        update={"availability": profile.availability.model_copy(update={"chain": ["none"]})}
    )
    stack.tenant = await stack.services.tenants.update_profile(
        stack.tenant.slug, profile, actor="test"
    )
    result = await stack.say(
        "Haben Sie vom 06.10.2026 bis 08.10.2026 ein Zimmer für 2 Erwachsene und 1 Kind?"
    )
    text = result.messages[-1].text
    assert text.startswith(
        "Ich habe Ihre Anfrage (06.10.2026 bis 08.10.2026, 2 Erwachsene, 1 Kinder) an die Rezeption weitergegeben"
    )
    assert "innerhalb von 2 Stunden" in text and result.handoff_id is not None
    async with stack.services.ctx.db.scope() as session:
        handoff = await session.get(Handoff, result.handoff_id)
    assert handoff is not None and handoff.priority == "P2" and "06.10.2026" in handoff.reason
