"""Agent tools (schema contract, bad-input matrix, session authority) and reply validators."""

from __future__ import annotations

import json
from datetime import date, time
from typing import Any

import pytest
from tests.unit.test_profile import SAMPLE

from concierge.agent.schemas import AgentReply, Button, HandoffDecision, output_schema
from concierge.agent.tools import CHAT_TOOLS, VOICE_TOOLS, ToolBackends, ToolRunner, ToolSession
from concierge.agent.validators import (
    ValidationContext,
    corrective_message,
    extract_facts,
    validate_reply,
)
from concierge.clock import FakeClock
from concierge.db.session import Database
from concierge.kb.fixtures import FIXTURES_DIR, import_kb_file
from concierge.kb.store import KbService
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

pytestmark = pytest.mark.feature("F02")


def _walk(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
        assert set(schema.get("required", [])) == set(schema["properties"])
        for prop in schema["properties"].values():
            _walk(prop)
    for forbidden in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
        assert forbidden not in schema
    if "items" in schema:
        _walk(schema["items"])


def test_tool_schemas_are_strict_and_within_limits() -> None:
    assert len(CHAT_TOOLS) <= 20
    names = [t.name for t in CHAT_TOOLS]
    assert names == [
        "check_availability",
        "lookup_kb",
        "search_kb",
        "verify_reservation",
        "get_booking_link",
        "create_request",
        "set_eta",
        "create_handoff",
        "request_callback",
        "set_language",
    ]
    for tool in CHAT_TOOLS + VOICE_TOOLS:
        assert tool.strict
        _walk(tool.input_schema)
    assert {t.name for t in VOICE_TOOLS} == {
        "check_availability",
        "lookup_kb",
        "request_callback",
        "set_language",
        "transfer_to_human",
        "end_call",
    }
    _walk(output_schema())


@pytest.fixture
async def runner(db: Database, clock: FakeClock) -> tuple[ToolRunner, ToolSession, str]:
    tenant = await TenantService(db, clock).create(TenantProfile.model_validate(SAMPLE), actor="t")
    kb = KbService(db, clock)
    await import_kb_file(kb, tenant.id, FIXTURES_DIR / "hotel_beispiel.yaml", approve=True)
    calls: dict[str, Any] = {}

    async def verify(reference: str, surname: str, arrival: date) -> dict[str, Any] | None:
        calls["verify"] = (reference, surname, arrival)
        if reference == "R1" and surname.lower() == "muster" and arrival == date(2026, 10, 3):
            return {
                "res_id": "R1",
                "arrival": "2026-10-03",
                "departure": "2026-10-05",
                "confirmation_number": "C-1",
            }
        return None

    async def availability(
        arrival: date, departure: date, adults: int, children: int, room_type: str
    ) -> dict[str, Any]:
        return {
            "source": "manual",
            "availability": "available",
            "arrival": arrival.isoformat(),
            "adults": adults,
        }

    async def link(
        arrival: date, departure: date, adults: int, children: int, room_type: str
    ) -> str | None:
        return f"https://example.invalid/book?from={arrival}&to={departure}&adults={adults}"

    async def request(kind: str, details: str) -> str:
        calls.setdefault("requests", []).append((kind, details))
        return "req_1"

    async def handoff(priority: str, reason: str) -> str:
        calls.setdefault("handoffs", []).append((priority, reason))
        return "hand_1"

    async def eta(value: time) -> None:
        calls["eta"] = value

    backends = ToolBackends(
        check_availability=availability,
        verify_reservation=verify,
        booking_link=link,
        create_request=request,
        create_handoff=handoff,
        set_eta=eta,
    )
    session = ToolSession(
        tenant_id=tenant.id,
        conversation_id="conv_1",
        guest_id="gst_1",
        lang="de",
        channel="whatsapp",
    )
    runner = ToolRunner(kb, backends)
    runner.calls = calls  # type: ignore[attr-defined]
    return runner, session, tenant.id


async def test_lookup_respects_verification_and_tenant(
    runner: tuple[ToolRunner, ToolSession, str], db: Database, clock: FakeClock
) -> None:
    tool_runner, session, _tenant_id = runner
    parking = json.loads((await tool_runner.run("lookup_kb", {"key": "parking"}, session)).content)
    assert parking["found"] and "18" in parking["text"] and parking["source"] == "kb"
    wifi = await tool_runner.run("lookup_kb", {"key": "wifi"}, session)
    assert wifi.is_error and "verified" in wifi.content
    missing = json.loads(
        (await tool_runner.run("lookup_kb", {"key": "house_rules.pool"}, session)).content
    )
    assert missing["found"] is False
    # another tenant with no KB gets nothing, even for the same key
    other = await TenantService(db, clock).create(
        TenantProfile.model_validate(dict(SAMPLE, slug="other", widget={"public_key": "pk_other"})),
        actor="t",
    )
    foreign = ToolSession(
        tenant_id=other.id, conversation_id="c", guest_id="g", lang="de", channel="whatsapp"
    )
    assert (
        json.loads((await tool_runner.run("lookup_kb", {"key": "parking"}, foreign)).content)[
            "found"
        ]
        is False
    )


async def test_verify_reservation_unlocks_sensitive_facts_and_limits_tries(
    runner: tuple[ToolRunner, ToolSession, str],
) -> None:
    tool_runner, session, _ = runner
    bad = json.loads(
        (
            await tool_runner.run(
                "verify_reservation",
                {"reference": "R1", "surname": "Falsch", "arrival": "2026-10-03"},
                session,
            )
        ).content
    )
    assert bad["verified"] is False and not session.verified
    good = await tool_runner.run(
        "verify_reservation",
        {"reference": "R1", "surname": "Muster", "arrival": "2026-10-03"},
        session,
    )
    assert json.loads(good.content)["verified"] and session.verified and good.binding_confirmation
    wifi = json.loads((await tool_runner.run("lookup_kb", {"key": "wifi"}, session)).content)
    assert wifi["found"] and "beispiel2026" in wifi["text"]
    session.verify_attempts = 3
    locked = await tool_runner.run(
        "verify_reservation",
        {"reference": "R1", "surname": "Muster", "arrival": "2026-10-03"},
        session,
    )
    assert locked.is_error and "locked" in locked.content


@pytest.mark.parametrize(
    ("name", "args", "fragment"),
    [
        (
            "check_availability",
            {
                "arrival": "next friday",
                "departure": "2026-10-05",
                "adults": 2,
                "children": 0,
                "room_type": "",
            },
            "YYYY-MM-DD",
        ),
        (
            "check_availability",
            {
                "arrival": "2026-10-05",
                "departure": "2026-10-05",
                "adults": 2,
                "children": 0,
                "room_type": "",
            },
            "after arrival",
        ),
        (
            "check_availability",
            {
                "arrival": "2026-10-05",
                "departure": "2026-10-06",
                "adults": 0,
                "children": 0,
                "room_type": "",
            },
            "adults",
        ),
        (
            "check_availability",
            {
                "arrival": "2026-10-05",
                "departure": "2026-12-06",
                "adults": 2,
                "children": 0,
                "room_type": "",
            },
            "30 nights",
        ),
        (
            "check_availability",
            {
                "arrival": "2026-02-30",
                "departure": "2026-03-02",
                "adults": 2,
                "children": 0,
                "room_type": "",
            },
            "calendar",
        ),
        ("set_eta", {"time": "25:00"}, "HH:MM"),
        ("create_request", {"kind": "spa", "details": "x"}, "one of"),
        ("create_handoff", {"priority": "P9", "reason": "x"}, "one of"),
        ("lookup_kb", {"key": ""}, "non-empty"),
        ("set_language", {"language": "fr"}, "one of"),
        ("transfer_to_human", {"reason": "x"}, "unknown tool"),
        ("no_such_tool", {}, "unknown tool"),
    ],
)
async def test_bad_input_matrix_returns_instructions(
    runner: tuple[ToolRunner, ToolSession, str], name: str, args: dict[str, Any], fragment: str
) -> None:
    tool_runner, session, _ = runner
    outcome = await tool_runner.run(name, args, session)
    assert outcome.is_error
    assert fragment in json.loads(outcome.content)["instruction"]


async def test_queue_tools_record_and_never_confirm(
    runner: tuple[ToolRunner, ToolSession, str],
) -> None:
    tool_runner, session, _ = runner
    avail = json.loads(
        (
            await tool_runner.run(
                "check_availability",
                {
                    "arrival": "2026-10-05",
                    "departure": "2026-10-07",
                    "adults": 2,
                    "children": 1,
                    "room_type": "",
                },
                session,
            )
        ).content
    )
    assert avail["availability"] == "available" and avail["adults"] == 2
    link = json.loads(
        (
            await tool_runner.run(
                "get_booking_link",
                {
                    "arrival": "2026-10-05",
                    "departure": "2026-10-07",
                    "adults": 2,
                    "children": 0,
                    "room_type": "dz",
                },
                session,
            )
        ).content
    )
    assert (
        link["url"].startswith("https://example.invalid/book?")
        and "never that it is booked" in link["instruction"]
    )
    req = json.loads(
        (
            await tool_runner.run(
                "create_request", {"kind": "late_checkout", "details": "until 13:00"}, session
            )
        ).content
    )
    assert req["status"] == "recorded" and "not confirmed" in req["instruction"]
    eta = json.loads((await tool_runner.run("set_eta", {"time": "23:30"}, session)).content)
    assert eta["eta"] == "23:30" and tool_runner.calls["eta"] == time(23, 30)  # type: ignore[attr-defined]
    hand = json.loads(
        (
            await tool_runner.run(
                "create_handoff", {"priority": "P2", "reason": "invoice change"}, session
            )
        ).content
    )
    assert hand["status"] == "created"
    lang = json.loads((await tool_runner.run("set_language", {"language": "uk"}, session)).content)
    assert lang["language"] == "uk" and session.lang == "uk"
    assert session.events == ["request:late_checkout", "eta", "handoff:P2", "language:uk"]
    cb = await tool_runner.run("request_callback", {"reason": "x"}, session)
    assert cb.is_error


async def test_voice_tools_without_backends(db: Database, clock: FakeClock) -> None:
    runner = ToolRunner(KbService(db, clock))
    session = ToolSession(
        tenant_id="t", conversation_id="c", guest_id="g", lang="de", channel="voice"
    )
    assert (await runner.run("transfer_to_human", {"reason": "x"}, session)).is_error
    ended = await runner.run("end_call", {"reason": "done"}, session)
    assert not ended.is_error and session.events == ["end_call"]
    unknown = await runner.run("create_handoff", {"priority": "P1", "reason": "x"}, session)
    assert unknown.is_error
    avail = json.loads(
        (
            await runner.run(
                "check_availability",
                {
                    "arrival": "2026-10-05",
                    "departure": "2026-10-06",
                    "adults": 1,
                    "children": 0,
                    "room_type": "",
                },
                session,
            )
        ).content
    )
    assert avail["availability"] == "unknown"


# ------------------------------------------------------------------ validators
def _reply(text: str, lang: str = "de", **kw: Any) -> AgentReply:
    return AgentReply(
        reply_text=text,
        language=lang,
        intent=kw.get("intent", "A:parking"),
        handoff=HandoffDecision(needed=False),
        fact_refs=[],
        confidence=0.9,
        buttons=kw.get("buttons", []),
    )


def _ctx(**kw: Any) -> ValidationContext:
    base = {
        "lang": "de",
        "canary": "gc-canary-abc",
        "kb_text": "## parking\nGarage 18 EUR pro Nacht, Einfahrtshöhe 2,10 m. Telefon +49 69 000000. https://example.invalid/book\n## checkin\nab 15:00 Uhr",
        "allowed_button_ids": frozenset({"person", "book"}),
    }
    base.update(kw)
    return ValidationContext(**base)


def test_extract_facts_normalises() -> None:
    facts = extract_facts(
        "Garage 18 EUR, Höhe 2,10 m, ab 9:00, am 3.10.2026 oder 2026-10-04, +49 69 000000, https://example.invalid/book/"
    )
    assert facts["number"] >= {"18", "2.1"}
    assert facts["time"] == {"09:00"}
    assert facts["date"] == {"2026-10-03", "2026-10-04"}
    assert facts["phone"] == {"4969000000"}
    assert facts["url"] == {"https://example.invalid/book"}


def test_grounded_reply_passes() -> None:
    result = validate_reply(
        _reply("Die Garage kostet 18 EUR pro Nacht bei 2.10 m Höhe. Check-in ab 15:00 Uhr."), _ctx()
    )
    assert result.ok, result.violations


def test_invented_numbers_and_urls_fail() -> None:
    result = validate_reply(
        _reply("Die Garage kostet 25 EUR pro Nacht. Buchen Sie unter https://other.invalid"), _ctx()
    )
    assert any("numbers not in facts: 25" in v for v in result.violations)
    assert any("url not in facts" in v for v in result.violations)
    corrective = corrective_message(result)
    assert "rejected" in corrective and "25" in corrective


def test_guest_numbers_and_tool_results_are_allowed() -> None:
    ctx = _ctx(
        guest_text="Wir kommen am 12.10.2026 mit 3 Personen",
        tool_texts=['{"price_cents": 9900, "arrival": "2026-10-12", "adults": 3}'],
    )
    result = validate_reply(
        _reply("Für 3 Personen am 12.10.2026: der Preis liegt bei 99 EUR laut Buchungssystem."), ctx
    )
    assert any("99" in v for v in result.violations)  # 9900 cents is not 99 EUR as a token
    ok = validate_reply(_reply("Für 3 Personen am 12.10.2026 prüfe ich die Verfügbarkeit."), ctx)
    assert ok.ok


def test_forbidden_binding_words_unless_confirmed() -> None:
    result = validate_reply(_reply("Ihr Zimmer ist reserviert und garantiert."), _ctx())
    assert any("binding words" in v for v in result.violations)
    negated = validate_reply(_reply("Der Parkplatz wird nicht garantiert."), _ctx())
    assert negated.ok
    confirmed = validate_reply(_reply("Ihr Zimmer ist reserviert."), _ctx(allow_binding_words=True))
    assert confirmed.ok


def test_identity_secrets_script_length_buttons_and_voice() -> None:
    assert (
        "claims to be human"
        in validate_reply(_reply("Ich bin ein Mensch und helfe gern."), _ctx()).violations
    )
    assert (
        "card number in reply"
        in validate_reply(
            _reply("Ihre Karte 4111 1111 1111 1111 ist hinterlegt."), _ctx()
        ).violations
    )
    assert "canary marker in reply" in validate_reply(_reply("gc-canary-abc"), _ctx()).violations
    assert any(
        "script" in v
        for v in validate_reply(_reply("Заезд с 15:00.", lang="de"), _ctx()).violations
    )
    assert any(
        "language" in v
        for v in validate_reply(_reply("Check-in from 15:00.", lang="en"), _ctx()).violations
    )
    long = validate_reply(_reply("x" * 400), _ctx())
    assert long.ok and long.warnings
    assert any("700" in v for v in validate_reply(_reply("x" * 701), _ctx()).violations)
    bad_button = validate_reply(
        _reply("Check-in ab 15:00 Uhr.", buttons=[Button(id="pay", title="Zahlen")]), _ctx()
    )
    assert any("button" in v for v in bad_button.violations)
    voice = validate_reply(
        _reply(
            "Check-in ab 15:00 Uhr. https://example.invalid/book",
            buttons=[Button(id="person", title="Mitarbeiter")],
        ),
        _ctx(channel="voice"),
    )
    assert any("voice" in v for v in voice.violations)


def test_reply_schema_validation() -> None:
    with pytest.raises(ValueError, match="intent"):
        _reply("x", intent="parking")
    with pytest.raises(ValueError, match="buttons"):
        _reply("x", buttons=[Button(id=str(i), title="t") for i in range(4)])
    with pytest.raises(ValueError, match="20"):
        Button(id="a", title="x" * 21)
    reply = _reply("ok")
    assert reply.intent_code == "A" and reply.topic == "parking"
