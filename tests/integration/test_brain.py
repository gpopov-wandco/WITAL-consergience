"""The conversation engine with the scripted model: orchestration, guardrails, failover, caps."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from tests.unit.test_profile import SAMPLE

from concierge.agent.brain import Brain, BrainResult, ModelConfig
from concierge.agent.guardrails import Guardrails
from concierge.agent.prompting import PromptAssembler, PromptTexts
from concierge.agent.resilience import CircuitBreaker, DailyBudget
from concierge.agent.responder import Responder
from concierge.agent.texts import guest_texts
from concierge.clock import FakeClock
from concierge.conversations.service import ConversationService, Inbound
from concierge.db.models import Handoff, Message, MessageText, Suppression, Tenant
from concierge.db.session import Database
from concierge.handoff.service import HandoffService
from concierge.kb.fixtures import FIXTURES_DIR, import_kb_file
from concierge.kb.store import KbService
from concierge.llm.port import LLMRequest, TextPart, ToolResult
from concierge.llm.scripted import ScriptedLLM, Step
from concierge.security.crypto import Keyring
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

pytestmark = pytest.mark.feature("F01")

PARKING_REPLY = {
    "reply_text": "Die Tiefgarage kostet 18 EUR pro Nacht, die Einfahrtshöhe beträgt 2,10 m.",
    "language": "de",
    "intent": "A:parking",
    "handoff": {"needed": False, "priority": "none", "reason": ""},
    "fact_refs": ["parking"],
    "confidence": 0.9,
    "buttons": [],
}


class Harness:
    def __init__(
        self,
        db: Database,
        keyring: Keyring,
        clock: FakeClock,
        tenant: Tenant,
        llm: ScriptedLLM | None,
        **kw: Any,
    ) -> None:
        self.db, self.keyring, self.clock, self.tenant, self.llm = db, keyring, clock, tenant, llm
        self.kb = KbService(db, clock)
        self.conversations = ConversationService(db, keyring, clock)
        self.handoffs = HandoffService(db, clock)
        self.alerts: list[tuple[str, str, dict[str, Any]]] = []
        self.usage: list[dict[str, Any]] = []

        async def alert(kind: str, tenant_id: str, details: dict[str, Any]) -> None:
            self.alerts.append((kind, tenant_id, details))

        async def usage(tenant_id: str, kind: str, usage: Any, meta: dict[str, Any]) -> None:
            self.usage.append({"tenant": tenant_id, "kind": kind, "meta": meta})

        self.brain = Brain(
            kb=self.kb,
            conversations=self.conversations,
            handoffs=self.handoffs,
            texts=guest_texts(),
            prompts=PromptAssembler(PromptTexts.load(), canary="gc-canary-test"),
            responder=Responder(self.kb, guest_texts()),
            guardrails=Guardrails(),
            clock=clock,
            keyring=keyring,
            llm=llm,
            models=kw.get("models", ModelConfig(turn_timeout_s=2.0)),
            alert_sink=alert,
            usage_sink=usage,
            breaker=kw.get("breaker"),
            budget=kw.get("budget"),
            demo_banner=kw.get("demo_banner", False),
        )

    async def say(
        self,
        text: str,
        *,
        key: str = "u1",
        channel: str = "whatsapp",
        kind: str = "text",
        phone: str | None = None,
    ) -> BrainResult:
        return await self.brain.respond(
            self.tenant,
            Inbound(
                channel=channel,
                channel_key=key,
                text=text,
                kind=kind,
                phone=phone,
                external_id=None,
            ),
        )

    async def handoffs_rows(self) -> list[Handoff]:
        async with self.db.scope() as session:
            return list(
                (await session.execute(select(Handoff).order_by(Handoff.created_at))).scalars()
            )

    async def texts_out(self) -> list[str]:
        async with self.db.scope() as session:
            rows = (
                await session.execute(
                    select(Message, MessageText)
                    .join(MessageText, MessageText.id == Message.id)
                    .where(Message.direction == "out")
                    .order_by(Message.occurred_at, Message.created_at)
                )
            ).all()
            return [t.text for _, t in rows]

    async def refreshed_tenant(self) -> Tenant:
        async with self.db.scope() as session:
            tenant = await session.get(Tenant, self.tenant.id)
            assert tenant is not None
            return tenant


@pytest.fixture
async def tenant(db: Database, clock: FakeClock) -> Tenant:
    service = TenantService(db, clock)
    created = await service.create(TenantProfile.model_validate(SAMPLE), actor="t")
    await import_kb_file(
        KbService(db, clock), created.id, FIXTURES_DIR / "hotel_beispiel.yaml", approve=True
    )
    return created


def _harness(
    db: Database,
    keyring: Keyring,
    clock: FakeClock,
    tenant: Tenant,
    steps: list[Step] | None,
    **kw: Any,
) -> Harness:
    llm = ScriptedLLM(steps) if steps is not None else None
    return Harness(db, keyring, clock, tenant, llm, **kw)


async def test_first_contact_discloses_ai_then_answers_with_tools(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    h = _harness(
        db,
        keyring,
        clock,
        tenant,
        [Step.tools(("lookup_kb", {"key": "parking"})), Step.final(PARKING_REPLY)],
    )
    result = await h.say("Gibt es einen Parkplatz?")
    assert result.mode == "model" and "disclosed" in result.events
    assert (
        result.messages[0].kind == "disclosure"
        and "KI-Assistent" in result.messages[0].text
        and "Hotel Beispiel" in result.messages[0].text
    )
    assert result.messages[1].text.startswith("Die Tiefgarage kostet 18 EUR")
    assert result.events.count("tool:lookup_kb") == 1
    assert len(result.usage) == 2 and h.usage[0]["meta"]["model"] == "claude-sonnet-5-5"
    first, second = h.llm.requests  # type: ignore[union-attr]
    assert [t.name for t in first.tools] == [t.name for t in second.tools]
    assert first.system == second.system and first.system[-1].cache is True
    assert (
        "[context]" in first.messages[-1].content[0].text
        and "reply_language: de" in first.messages[-1].content[0].text
    )  # type: ignore[union-attr]
    assert isinstance(second.messages[-1].content[0], ToolResult)
    assert second.messages[-2].raw is not None
    conversation = await h.conversations.get(tenant.id, result.conversation_id)
    assert (
        conversation is not None
        and conversation.ai_disclosed_at is not None
        and conversation.status == "bot_handling"
    )
    # the second turn rebuilds history as text-only turns and does not disclose again
    h.llm.load(
        [
            Step.final(
                {
                    **PARKING_REPLY,
                    "reply_text": "Check-in ist ab 15:00 Uhr möglich.",
                    "intent": "A:checkin",
                }
            )
        ]
    )  # type: ignore[union-attr]
    again = await h.say("Und ab wann ist Check-in?")
    assert "disclosed" not in again.events and again.messages[0].kind == "text"
    request: LLMRequest = h.llm.requests[-1]  # type: ignore[union-attr]
    assert request.messages[0].role == "user" and all(m.raw is None for m in request.messages)
    assert all(isinstance(p, TextPart) for m in request.messages for p in m.content)
    assert request.system == first.system


async def test_validator_regenerates_once_then_hands_off(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    bad = {**PARKING_REPLY, "reply_text": "Die Garage kostet 25 EUR und Ihr Platz ist reserviert."}
    h = _harness(db, keyring, clock, tenant, [Step.final(bad), Step.final(PARKING_REPLY)])
    result = await h.say("Gibt es einen Parkplatz?")
    assert result.mode == "model" and result.messages[-1].text.startswith("Die Tiefgarage")
    corrective = h.llm.requests[1].messages[-1].content[0].text  # type: ignore[union-attr]
    assert "rejected" in corrective and "25" in corrective and "reserviert" in corrective
    h2 = _harness(db, keyring, clock, tenant, [Step.final(bad), Step.final(bad)])
    result2 = await h2.say("Gibt es einen Parkplatz?", key="u2")
    assert result2.mode == "responder" and any(
        e.startswith("model_failure:validation") for e in result2.events
    )
    assert "18 EUR" in result2.messages[-1].text


async def test_refusal_and_invalid_json_fall_back_to_responder(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    h = _harness(db, keyring, clock, tenant, [Step.final("I refuse", stop_reason="refusal")])
    result = await h.say("Wann gibt es Frühstück?")
    assert result.mode == "responder" and "model_failure:stop_reason:refusal" in result.events
    assert "07:00" in result.messages[-1].text
    h2 = _harness(db, keyring, clock, tenant, [Step.final("not json"), Step.final(PARKING_REPLY)])
    result2 = await h2.say("Gibt es einen Parkplatz?", key="u2")
    assert result2.mode == "model" and len(h2.llm.requests) == 2  # type: ignore[union-attr]


async def test_failover_chain_spend_cap_and_all_failed(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    h = _harness(db, keyring, clock, tenant, [Step.fail("overloaded"), Step.final(PARKING_REPLY)])
    result = await h.say("Gibt es einen Parkplatz?")
    assert result.mode == "model" and result.models == ["claude-sonnet-5"]
    assert "llm_error:claude-sonnet-5-5:overloaded" in result.events
    h2 = _harness(db, keyring, clock, tenant, [Step.fail("spend_cap")])
    result2 = await h2.say("Gibt es einen Parkplatz?", key="u2")
    assert result2.mode == "responder" and h2.alerts[0][0] == "spend_cap"
    assert len(h2.llm.requests) == 1  # type: ignore[union-attr]
    h3 = _harness(
        db,
        keyring,
        clock,
        tenant,
        [Step.fail("timeout"), Step.fail("server"), Step.fail("rate_limit")],
        models=ModelConfig(cheap="claude-haiku-4-5", turn_timeout_s=2.0),
    )
    result3 = await h3.say("Gibt es einen Parkplatz?", key="u3")
    assert (
        result3.mode == "responder"
        and "model_failure:all_models_failed:rate_limit" in result3.events
    )
    assert h3.llm.requests[-1].model == "claude-haiku-4-5" and h3.llm.requests[-1].effort is None  # type: ignore[union-attr]


async def test_loop_caps_and_tool_budget(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    h = _harness(
        db,
        keyring,
        clock,
        tenant,
        [Step.tools(("lookup_kb", {"key": "parking"})) for _ in range(6)],
    )
    result = await h.say("Gibt es einen Parkplatz?")
    assert result.mode == "responder" and "model_failure:call_cap_reached" in result.events
    assert len(h.llm.requests) == 5  # type: ignore[union-attr]
    many = Step.tools(*[("lookup_kb", {"key": "parking"})] * 7)
    h2 = _harness(db, keyring, clock, tenant, [many, Step.final(PARKING_REPLY)])
    result2 = await h2.say("Gibt es einen Parkplatz?", key="u2")
    assert result2.mode == "model" and result2.events.count("tool:lookup_kb") == 6
    tool_results = [p for p in h2.llm.requests[1].messages[-1].content if isinstance(p, ToolResult)]  # type: ignore[union-attr]
    assert len(tool_results) == 7 and tool_results[-1].is_error


async def test_killed_model_call_still_replies_and_opens_breaker(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    breaker = CircuitBreaker(clock, failure_threshold=2)
    h = _harness(
        db,
        keyring,
        clock,
        tenant,
        [Step.hang(5)],
        models=ModelConfig(turn_timeout_s=0.3),
        breaker=breaker,
    )
    result = await h.say("Gibt es einen Parkplatz?")
    assert result.mode == "responder" and "model_failure:turn_timeout" in result.events
    h.llm.load([Step.hang(5)])  # type: ignore[union-attr]
    await h.say("Gibt es einen Parkplatz?", key="u2")
    assert breaker.is_open
    h.llm.load([Step.final(PARKING_REPLY)])  # type: ignore[union-attr]
    third = await h.say("Gibt es einen Parkplatz?", key="u3")
    assert third.mode == "responder" and len(h.llm.requests) == 2  # type: ignore[union-attr]
    clock.advance(timedelta(seconds=61))
    fourth = await h.say("Gibt es einen Parkplatz?", key="u4")
    assert fourth.mode == "model"


async def test_budget_exhausted_uses_responder(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    budget = DailyBudget(clock)
    budget.record(tenant.id, "Europe/Berlin", 5.0)
    h = _harness(db, keyring, clock, tenant, [Step.final(PARKING_REPLY)], budget=budget)
    result = await h.say("Gibt es einen Parkplatz?")
    assert result.mode == "responder" and h.llm.requests == []  # type: ignore[union-attr]


async def test_emergency_static_reply_without_model(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    h = _harness(db, keyring, clock, tenant, [Step.final(PARKING_REPLY)])
    result = await h.say("Hilfe, es brennt in meinem Zimmer")
    assert h.llm.requests == []  # type: ignore[union-attr]
    assert "112" in result.messages[0].text and result.messages[1].kind == "disclosure"
    rows = await h.handoffs_rows()
    assert rows[0].priority == "P0" and rows[0].trigger == "emergency"
    conversation = await h.conversations.get(tenant.id, result.conversation_id)
    assert conversation is not None and conversation.status == "needs_staff"


async def test_stop_person_other_guest_and_card(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    h = _harness(db, keyring, clock, tenant, [Step.final(PARKING_REPLY)] * 4)
    stop = await h.say("STOP", key="s1", phone="+49 151 1111111")
    assert "stop" in stop.events and "automatischen Nachrichten" in stop.messages[-1].text
    async with db.scope() as session:
        suppression = (await session.execute(select(Suppression))).scalar_one()
    assert (
        suppression.subject_bidx == keyring.blind_index("+49 151 1111111", tenant.id, "phone")
        and suppression.reason == "stop"
    )
    person = await h.say("Ich möchte mit einem Mitarbeiter sprechen", key="p1")
    assert (
        any(e.startswith("handoff:P2:person_requested") for e in person.events)
        and "Hotelteam" in person.messages[-1].text
    )
    other = await h.say("In welchem Zimmer wohnt Herr Müller?", key="o1")
    assert "andere Gäste" in other.messages[-1].text and any(
        e.startswith("handoff:P3:other_guest") for e in other.events
    )
    assert h.llm.requests == []  # type: ignore[union-attr]
    card = await h.say("Meine Kartennummer ist 4111 1111 1111 1111, CVV 123", key="c1")
    assert "redacted:card,cvv" in card.events
    assert any("Karten- oder Ausweisdaten" in m.text for m in card.messages)
    async with db.scope() as session:
        stored = [
            t.text
            for t in (await session.execute(select(MessageText))).scalars()
            if "Kartennummer" in t.text
        ]
    assert stored and all("4111" not in t and "[redacted]" in t for t in stored)
    # the redacted text may reach the model, the card number never does
    for request in h.llm.requests:  # type: ignore[union-attr]
        payload = json.dumps(
            [[getattr(p, "text", "") for p in m.content] for m in request.messages]
        )
        assert "4111" not in payload and "[redacted]" in payload


async def test_bot_is_silent_while_staff_handle(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    h = _harness(db, keyring, clock, tenant, [Step.final(PARKING_REPLY)] * 3)
    first = await h.say("Ich möchte mit einem Mitarbeiter sprechen")
    second = await h.say("Hallo? Ist jemand da?")
    assert (
        second.silent
        and len(second.messages) == 1
        and "Mitarbeiter kümmert sich" in second.messages[0].text
    )
    third = await h.say("Hallo??")
    assert third.silent and third.messages == []
    rows = await h.handoffs_rows()
    await h.handoffs.resolve(tenant.id, rows[0].id, user_id="usr_1")
    fourth = await h.say("Gibt es einen Parkplatz?")
    assert not fourth.silent and fourth.mode == "model"
    assert first.conversation_id == fourth.conversation_id


async def test_kill_switch_and_faq_only_and_demo_banner(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    service = TenantService(db, clock)
    await service.set_kill_switch(tenant.slug, True, actor="op")
    h = _harness(db, keyring, clock, tenant, [Step.final(PARKING_REPLY)])
    h.tenant = await h.refreshed_tenant()
    result = await h.say("Gibt es einen Parkplatz?", key="u2")
    assert result.mode == "degraded" and "eingeschränkt" in result.messages[-1].text
    assert h.llm.requests == []  # type: ignore[union-attr]
    assert any(e.startswith("handoff:P2:degraded") for e in result.events)
    await service.set_kill_switch(tenant.slug, False, actor="op")
    profile = TenantProfile.model_validate(dict(SAMPLE, ai_mode="faq_only"))
    await service.update_profile(tenant.slug, profile, actor="op")
    h.tenant = await h.refreshed_tenant()
    h.brain.demo_banner = True
    result = await h.say("Gibt es einen Parkplatz?", key="u3")
    assert result.mode == "responder" and h.llm.requests == []  # type: ignore[union-attr]
    assert any(m.kind == "banner" for m in result.messages)
    again = await h.say("Wann gibt es Frühstück?", key="u3")
    assert not any(m.kind == "banner" for m in again.messages)


async def test_unsupported_language_gets_english_note(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    profile = TenantProfile.model_validate(dict(SAMPLE, languages=["de", "en"]))
    await TenantService(db, clock).update_profile(tenant.slug, profile, actor="op")
    h = _harness(db, keyring, clock, tenant, None)
    h.tenant = await h.refreshed_tenant()
    result = await h.say("Buongiorno, a che ora è la colazione domani mattina?")
    assert result.language == "en" and any(
        e.startswith("unsupported_language:it") for e in result.events
    )
    assert any("Deutsch, English" in m.text for m in result.messages)
    assert result.messages[-1].lang == "en" and "07:00" in result.messages[-1].text


async def test_same_question_off_topic_and_unresolved_turns(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    off_topic = {
        **PARKING_REPLY,
        "reply_text": "Dazu kann ich nichts sagen.",
        "intent": "R:off_topic",
        "fact_refs": [],
    }
    h = _harness(
        db,
        keyring,
        clock,
        tenant,
        [Step.final(off_topic), Step.final(off_topic), Step.final(off_topic)],
    )
    first = await h.say("Wie wird das Wetter morgen?")
    assert "Anreise" in first.messages[-1].text and first.handoff_id is None
    second = await h.say("Was ist die Hauptstadt von Frankreich?")
    assert "Mitarbeiter sprechen" in second.messages[-1].text and second.handoff_id is None
    third = await h.say("Erzähl mir einen Witz")
    assert third.handoff_id is not None and any(
        e.startswith("handoff:P3:unresolved_turns") for e in third.events
    )
    question = {
        **PARKING_REPLY,
        "reply_text": "Für welche Daten möchten Sie übernachten?",
        "intent": "Q:availability",
        "fact_refs": [],
    }
    h2 = _harness(db, keyring, clock, tenant, [Step.final(question), Step.final(question)])
    a = await h2.say("Haben Sie ein Zimmer frei?", key="q1")
    assert a.handoff_id is None and "same_question" not in a.events
    b = await h2.say("Haben Sie ein Zimmer frei?", key="q1")
    assert "same_question" in b.events and "Mitarbeiter sprechen" in b.messages[-1].text
    assert [x.id for x in b.messages[-1].buttons] == ["person"]
    # the responder path answers a repeated unknown question with a handoff every time
    h3 = _harness(db, keyring, clock, tenant, None)
    c = await h3.say("Gibt es einen Pool?", key="q2")
    assert c.handoff_id is not None and c.mode == "responder"


async def test_media_gets_text_fallback(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    h = _harness(db, keyring, clock, tenant, [Step.final(PARKING_REPLY)])
    result = await h.say("", kind="audio")
    assert (
        "Sprachnachrichten" in result.messages[-1].text
        or "voice messages" in result.messages[-1].text
    )
    assert h.llm.requests == []  # type: ignore[union-attr]


async def test_concurrent_turns_are_serialised(
    db: Database, keyring: Keyring, clock: FakeClock, tenant: Tenant
) -> None:
    h = _harness(db, keyring, clock, tenant, None)
    results = await asyncio.gather(*(h.say(f"Gibt es einen Parkplatz? {i}") for i in range(4)))
    assert len({r.conversation_id for r in results}) == 1
    outs = await h.texts_out()
    assert sum(1 for t in outs if "KI-Assistent" in t) == 1
    assert sum(1 for t in outs if "18 EUR" in t) == 4
    assert json.dumps([r.mode for r in results]) == json.dumps(["responder"] * 4)
