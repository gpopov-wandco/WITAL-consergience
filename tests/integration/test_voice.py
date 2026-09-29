"""Voice fallback (F26) through the simulators: disclosure, filler, cancellation,
tools, transfer, callback, decline, end-of-call pipeline, overage, EU gate."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from concierge.app import create_app
from concierge.channels.voice import retell, twilio_cr
from concierge.channels.voice.port import CallEnd
from concierge.channels.whatsapp.fake import FakeGraph
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.db.models import (
    CallbackTask,
    Handoff,
    Message,
    MessageText,
    Tenant,
    UsageEvent,
    VoiceCall,
)
from concierge.db.session import Database
from concierge.demo.seed import seed_demo
from concierge.demo.voice_sim import RetellSimClient, TwilioSimClient
from concierge.llm.scripted import ScriptedLLM, Step
from concierge.notify.port import FakeEmail
from concierge.security.crypto import Keyring
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F26")

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)  # 12:00 Berlin, staffed hours
NIGHT = datetime(2026, 9, 29, 23, 30, tzinfo=UTC)  # 01:30 Berlin, unstaffed
RETELL_KEY = "rk_test_key"
TWILIO_TOKEN = "tw_test_token"
NUMBER = "+4969000009"
CALLER = "+4915120000009"
PARKING = {
    "reply_text": "Die Tiefgarage kostet 18 EUR pro Nacht, Einfahrtshöhe 2,10 m. Reservieren Sie unter https://example.invalid/p. Drittens.",
    "language": "de",
    "intent": "A:parking",
    "handoff": {"needed": False, "priority": "none", "reason": ""},
    "fact_refs": ["parking"],
    "confidence": 0.9,
    "buttons": [],
}


def reply(text: str, lang: str = "de", intent: str = "A:parking") -> dict[str, Any]:
    return {**PARKING, "reply_text": text, "language": lang, "intent": intent}


@dataclass
class VoiceStack:
    services: Services
    tenant: Tenant
    clock: FakeClock
    llm: ScriptedLLM | None
    client: httpx2.AsyncClient
    settings: Settings

    async def refresh(self) -> None:
        self.tenant = await self.services.tenants.require("hotel-beispiel")

    async def set_voice(self, **voice: Any) -> None:
        data = TenantProfile.model_validate(self.tenant.profile).model_dump(mode="json")
        data["voice"].update(voice)
        await self.services.tenants.update_profile(
            "hotel-beispiel", TenantProfile.model_validate(data), actor="test"
        )
        await self.refresh()

    async def calls(self) -> list[VoiceCall]:
        async with self.services.ctx.db.scope() as session:
            return list(
                (await session.execute(select(VoiceCall).order_by(VoiceCall.created_at))).scalars()
            )

    async def usage(self) -> list[UsageEvent]:
        async with self.services.ctx.db.scope() as session:
            return list(
                (
                    await session.execute(
                        select(UsageEvent)
                        .where(UsageEvent.kind == "voice")
                        .order_by(UsageEvent.occurred_at)
                    )
                ).scalars()
            )

    async def callbacks(self) -> list[CallbackTask]:
        async with self.services.ctx.db.scope() as session:
            return list((await session.execute(select(CallbackTask))).scalars())

    async def handoffs(self) -> list[Handoff]:
        async with self.services.ctx.db.scope() as session:
            return list((await session.execute(select(Handoff))).scalars())

    def signed(self, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(payload).encode()
        header = retell.sign(body, RETELL_KEY, int(self.clock.now().timestamp() * 1000))
        return body, {"X-Retell-Signature": header, "Content-Type": "application/json"}


async def _build(
    db: Database,
    settings: Settings,
    keyring: Keyring,
    clock: FakeClock,
    llm: ScriptedLLM | None,
) -> AsyncIterator[VoiceStack]:
    clock.set(NOW)
    voice_settings = Settings(
        _env_file=None,
        **{
            **settings.model_dump(exclude={"retell_api_key", "twilio_auth_token"}),
            "retell_api_key": RETELL_KEY,
            "twilio_account_sid": "AC_test",
            "twilio_auth_token": TWILIO_TOKEN,
        },
    )
    ctx = AppContext(settings=voice_settings, db=db, keyring=keyring, clock=clock)
    services = build_services(ctx, llm=llm, wa_port=FakeGraph(), demo=True, email=FakeEmail())
    await seed_demo(services)
    tenant = await services.tenants.require("hotel-beispiel")
    stack = VoiceStack(services, tenant, clock, llm, None, voice_settings)  # type: ignore[arg-type]
    await stack.set_voice(
        provider="retell",
        number=NUMBER,
        agent_id="agent_demo",
        voice_id="voice_demo",
        filler_ms=100,
    )
    app = create_app(services, start_worker=False)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        stack.client = client
        yield stack


@pytest.fixture
async def voice(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> AsyncIterator[VoiceStack]:
    """The scripted responder answers (no model)."""
    async for stack in _build(db, settings, keyring, clock, None):
        yield stack


@pytest.fixture
async def voice_llm(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> AsyncIterator[VoiceStack]:
    """The scripted model answers; each test loads its steps."""
    async for stack in _build(db, settings, keyring, clock, ScriptedLLM([])):
        yield stack


# ------------------------------------------------------------ disclosure
@pytest.mark.parametrize(
    ("question", "spoken"),
    [
        ("Haben Sie einen Parkplatz?", "Garage"),
        ("Do you have parking?", "garage"),
        ("У вас есть парковка?", "гараж"),
    ],
)
async def test_disclosure_first_and_content_complete_last_only(
    voice: VoiceStack, question: str, spoken: str
) -> None:
    async with RetellSimClient(voice.services.voice, voice.tenant) as call:
        assert call.greeting is not None
        assert call.greeting["response_id"] == 0 and call.greeting["content_complete"] is True
        # the disclosure is spoken in the hotel's default language before any caller turn
        assert "KI-System, keine Person" in call.greeting["content"]
        assert not call.greeting["end_call"]
        assert voice.services.voice.state_for("retell", call.call_id) is not None
        pong = await call.ping()
        assert pong["response_type"] == "ping_pong"
        chunks = await call.say(question)
        assert [c["content_complete"] for c in chunks][-1] is True
        assert all(not c["content_complete"] for c in chunks[:-1])
        text = chunks[-1]["content"]
        assert spoken.lower() in text.lower()
        assert "http" not in text and text.count(".") <= 3
        assert len([s for s in text.replace("?", ".").split(". ") if s]) <= 2
    calls = await voice.calls()
    assert len(calls) == 1 and calls[0].disclosure_spoken_at == NOW
    assert calls[0].caller_ref.startswith("ph_") and CALLER not in calls[0].caller_ref
    async with voice.services.ctx.db.scope() as session:
        kinds = [
            m.kind
            for m in (await session.execute(select(Message).order_by(Message.created_at))).scalars()
        ]
    assert kinds[0] == "disclosure"


async def test_filler_after_delay_and_newer_response_cancels(voice_llm: VoiceStack) -> None:
    llm = voice_llm.llm
    assert llm is not None
    llm.load([Step.final(reply("Ja, wir haben eine Garage."), delay_s=0.5)])
    async with RetellSimClient(voice_llm.services.voice, voice_llm.tenant) as call:
        chunks = await call.say("Haben Sie einen Parkplatz?")
        assert [c["content_complete"] for c in chunks] == [False, True]
        assert chunks[0]["content"] == "Einen Moment bitte."
        assert chunks[1]["content"] == "Ja, wir haben eine Garage."

        # a newer response id cancels the running generation: id 1 never completes,
        # id 2 is answered, and the engine keeps working afterwards
        llm.load(
            [
                Step.final(reply("Antwort."), delay_s=1.0),
                Step.final(reply("Antwort.")),
                Step.final(reply("Antwort.")),
            ]
        )
        first = call.speak("Erste Frage?")
        second = call.speak("Zweite Frage?")
        done = await call.responses(second)
        assert done[-1]["content"] == "Antwort." and done[-1]["content_complete"]
        stale = [
            m
            for m in call.socket.sent
            if m.get("response_type") == "response" and m.get("response_id") == first
        ]
        assert all(not m["content_complete"] for m in stale)
        last_request = llm.requests[-1]
        assert "Zweite Frage?" in str(last_request.messages[-1].content[-1])
        third = await call.say("Dritte Frage?")
        assert third[-1]["content"] == "Antwort." and third[-1]["content_complete"]
        # the cancelled generation consumed at most one scripted step
        assert len(llm.steps) <= 1


async def test_tool_round_trip_and_transfer_in_staffed_hours(voice_llm: VoiceStack) -> None:
    llm = voice_llm.llm
    assert llm is not None
    llm.load(
        [
            Step.tools(
                (
                    "check_availability",
                    {
                        "arrival": "2026-10-10",
                        "departure": "2026-10-12",
                        "adults": 2,
                        "children": 0,
                        "room_type": "",
                    },
                )
            ),
            Step.final(
                reply("Für diese Nächte ist ein Doppelzimmer frei.", intent="A:availability")
            ),
            Step.tools(("transfer_to_human", {"reason": "caller wants to book"})),
            Step.final(reply("Ich verbinde Sie mit der Rezeption.", intent="H:transfer")),
        ]
    )
    async with RetellSimClient(voice_llm.services.voice, voice_llm.tenant) as call:
        chunks = await call.say("Haben Sie vom zehnten bis zwölften Oktober ein Zimmer?")
        assert chunks[-1]["content"] == "Für diese Nächte ist ein Doppelzimmer frei."
        tool_result = llm.requests[1].messages[-1].content[0]
        assert '"status": "available"' in str(getattr(tool_result, "content", tool_result))
        chunks = await call.say("Dann buchen Sie das bitte.")
        final = chunks[-1]
        assert final["transfer_number"] == "+4969000000" and final["content_complete"]
        assert final["content"] == "Ich verbinde Sie mit der Rezeption."
    calls = await voice_stack_calls(voice_llm)
    assert calls[0].outcome == "transferred"


async def voice_stack_calls(stack: VoiceStack) -> list[VoiceCall]:
    return await stack.calls()


async def test_transfer_outside_staffed_hours_becomes_callback(voice_llm: VoiceStack) -> None:
    voice_llm.clock.set(NIGHT)
    llm = voice_llm.llm
    assert llm is not None
    llm.load(
        [
            Step.tools(("transfer_to_human", {"reason": "wants a person"})),
            Step.tools(("request_callback", {"reason": "wants a person, night"})),
            Step.final(reply("Das Team ruft Sie morgen früh zurück.", intent="H:callback")),
        ]
    )
    async with RetellSimClient(voice_llm.services.voice, voice_llm.tenant) as call:
        chunks = await call.say("Können Sie mich mit jemandem verbinden, der bucht?")
        assert "transfer_number" not in chunks[-1]
        assert chunks[-1]["content"] == "Das Team ruft Sie morgen früh zurück."
        error_result = llm.requests[1].messages[-1].content[0]
        assert getattr(error_result, "is_error", False) is True
    callbacks = await voice_llm.callbacks()
    assert len(callbacks) == 1 and callbacks[0].lang == "de"
    assert callbacks[0].phone_bidx is not None


async def test_person_keyword_transfers_without_the_model(voice: VoiceStack) -> None:
    async with RetellSimClient(voice.services.voice, voice.tenant) as call:
        chunks = await call.say("Ich möchte bitte mit einem Mitarbeiter sprechen.")
        assert chunks[-1]["transfer_number"] == "+4969000000"
        assert chunks[-1]["content"] == "Ich verbinde Sie mit der Rezeption."
        # after the transfer the engine answers nothing more on this call
        ignored = call.speak("Hallo?")
        await asyncio.sleep(0.2)
        assert not [m for m in call.socket.sent if m.get("response_id") == ignored]
    calls = await voice.calls()
    assert calls[0].outcome == "transferred"


async def test_person_at_night_captures_callback_and_notifies_staff(voice: VoiceStack) -> None:
    voice.clock.set(NIGHT)
    async with RetellSimClient(voice.services.voice, voice.tenant) as call:
        chunks = await call.say("Ich will einen Mitarbeiter sprechen")
        assert "transfer_number" not in chunks[-1]
        assert "Rückruf" in chunks[-1]["content"]
    callbacks = await voice.callbacks()
    assert len(callbacks) == 1 and callbacks[0].voice_call_id == (await voice.calls())[0].id
    payload = {
        "event": "call_ended",
        "call": {
            "call_id": "call_sim_1",
            "agent_id": "agent_demo",
            "from_number": CALLER,
            "to_number": NUMBER,
            "start_timestamp": int(NIGHT.timestamp() * 1000),
            "end_timestamp": int(NIGHT.timestamp() * 1000) + 90_000,
        },
    }
    body, headers = voice.signed(payload)
    response = await voice.client.post("/voice/retell/webhook", content=body, headers=headers)
    assert response.status_code == 200
    handoffs = await voice.handoffs()
    assert len(handoffs) == 1 and handoffs[0].trigger == "voice_callback"
    assert handoffs[0].priority == "P2" and "Anruf (de)" in handoffs[0].summary
    alerts = [r for r in voice.services.alerts_out.sent if r.handoff_id == handoffs[0].id]
    assert [r.channel for r in alerts] == ["email"]  # P2 at night: e-mail only


async def test_decline_captures_callback_without_transcript(voice: VoiceStack) -> None:
    async with RetellSimClient(voice.services.voice, voice.tenant) as call:
        chunks = await call.say("Ich bin damit nicht einverstanden, keine KI bitte.")
        assert chunks[-1]["end_call"] is True and "kein Gespräch" in chunks[-1]["content"]
    body, headers = voice.signed(
        {
            "event": "call_ended",
            "call": {
                "call_id": "call_sim_1",
                "agent_id": "agent_demo",
                "start_timestamp": int(NOW.timestamp() * 1000),
                "end_timestamp": int(NOW.timestamp() * 1000) + 30_000,
                "transcript_object": [{"role": "user", "content": "keine KI bitte"}],
            },
        }
    )
    assert (
        await voice.client.post("/voice/retell/webhook", content=body, headers=headers)
    ).status_code == 200
    call_row = (await voice.calls())[0]
    assert call_row.outcome == "declined" and call_row.transcript is None
    assert call_row.transcript_expires_at is None and call_row.ended_at is not None
    assert "abgelehnt" in call_row.summary_de and "keine KI" not in call_row.summary_de
    assert len(await voice.callbacks()) == 1


async def test_end_of_call_pipeline_replay_and_signature(voice: VoiceStack) -> None:
    async with RetellSimClient(voice.services.voice, voice.tenant) as call:
        await call.say("Wann ist Check-in?")
        await call.say("Danke, das war alles.")
    payload = {
        "event": "call_ended",
        "call": {
            "call_id": "call_sim_1",
            "agent_id": "agent_demo",
            "from_number": CALLER,
            "start_timestamp": int(NOW.timestamp() * 1000),
            "end_timestamp": int(NOW.timestamp() * 1000) + 125_000,
            "call_cost": {"combined_cost": 4.0},
        },
    }
    body, headers = voice.signed(payload)
    bad = await voice.client.post(
        "/voice/retell/webhook", content=body, headers={**headers, "X-Retell-Signature": "v=1,d=00"}
    )
    assert bad.status_code == 401
    unknown = await voice.client.post(
        "/voice/retell/webhook",
        content=voice.signed(
            {**payload, "call": {**payload["call"], "agent_id": "nope", "to_number": ""}}
        )[0],
        headers=voice.signed(
            {**payload, "call": {**payload["call"], "agent_id": "nope", "to_number": ""}}
        )[1],
    )
    assert unknown.status_code == 404
    ok = await voice.client.post("/voice/retell/webhook", content=body, headers=headers)
    assert ok.status_code == 200
    again = await voice.client.post("/voice/retell/webhook", content=body, headers=headers)
    assert again.status_code == 200
    row = (await voice.calls())[0]
    assert row.ended_at == NOW and row.duration_s == 125 and row.billable_s == 125
    assert row.transcript is not None and "user: Wann ist Check-in?" in row.transcript
    assert row.transcript_expires_at == NOW + timedelta(days=30)
    assert row.provider_cost_micro_eur == 40_000 and row.outcome == "completed"
    assert row.summary_de.startswith("Anruf (de), 3 Min., 2 Anliegen.")
    usage = await voice.usage()
    assert len(usage) == 1 and usage[0].quantity == 125 and usage[0].unit == "second"
    assert usage[0].cost_micro_eur == 0 and usage[0].ref == f"voice:{row.id}"
    assert voice.services.voice.state_for("retell", "call_sim_1") is None
    assert await voice.handoffs() == []


async def test_overage_after_included_minutes(voice: VoiceStack) -> None:
    services, tenant = voice.services, voice.tenant
    profile = services.tenants.profile_of(tenant)
    for index, seconds in enumerate((300 * 60, 600), start=1):
        state, _reply = await services.voice.begin(
            tenant, "retell", f"call_{index}", from_number=CALLER
        )
        row = await services.postcall.finish(
            tenant,
            profile,
            call_row_id=state.call_row_id,
            duration_s=seconds,
            transcript=[("user", "x")],
            provider_cost_micro_eur=None,
            outcome="completed",
            retain_transcript=True,
            callback_needed=False,
        )
        assert row is not None
        services.voice.forget(state)
    usage = await voice.usage()
    assert [u.cost_micro_eur for u in usage] == [0, 1_900_000]  # 10 minutes x 0.19 EUR
    assert usage[1].meta["overage_s"] == 600
    # minute rounding bills the started minute
    await voice.set_voice(rounding="minute")
    profile = services.tenants.profile_of(voice.tenant)
    state, _reply = await services.voice.begin(voice.tenant, "retell", "call_3", from_number=CALLER)
    row = await services.postcall.finish(
        voice.tenant,
        profile,
        call_row_id=state.call_row_id,
        duration_s=61,
        transcript=[],
        provider_cost_micro_eur=None,
        outcome="completed",
        retain_transcript=True,
        callback_needed=False,
    )
    assert row is not None and row.billable_s == 120
    assert (await voice.usage())[-1].cost_micro_eur == 380_000


async def test_eu_only_gate_refuses_retell_calls(voice: VoiceStack) -> None:
    async with voice.services.ctx.db.scope() as session:
        row = await session.get(Tenant, voice.tenant.id)
        assert row is not None
        profile = dict(row.profile)
        profile["voice"] = {**profile["voice"], "eu_only": True, "us_transfer_acknowledged": False}
        row.profile = profile
        await session.commit()
    await voice.refresh()
    with pytest.raises(ValueError, match="eu_only"):
        TenantProfile.model_validate(voice.tenant.profile)  # the profile itself is invalid
    voice.services.tenants.profile_of = lambda tenant: TenantProfile.model_validate(  # type: ignore[method-assign]
        {**tenant.profile, "voice": {**tenant.profile["voice"], "us_transfer_acknowledged": True}}
    ).model_copy(
        update={
            "voice": TenantProfile.model_validate(
                {
                    **tenant.profile,
                    "voice": {**tenant.profile["voice"], "us_transfer_acknowledged": True},
                }
            ).voice.model_copy(update={"us_transfer_acknowledged": False})
        }
    )
    state, greeting = await voice.services.voice.begin(
        voice.tenant, "retell", "call_eu", from_number=CALLER
    )
    assert greeting.end_call is True and "+49 69 000000" in greeting.text
    assert state.outcome == "refused_eu_only" and state.ended
    assert (await voice.calls())[0].outcome == "refused_eu_only"


async def test_paused_tenant_refuses_calls(voice: VoiceStack) -> None:
    await voice.services.tenants.set_kill_switch("hotel-beispiel", True, actor="test")
    await voice.refresh()
    async with RetellSimClient(voice.services.voice, voice.tenant) as call:
        assert call.greeting is not None
        assert call.greeting["end_call"] is True and "nicht verfügbar" in call.greeting["content"]


async def test_reconcile_closes_calls_without_an_end_event(voice: VoiceStack) -> None:
    services = voice.services
    state, _greeting = await services.voice.begin(
        voice.tenant, "retell", "call_lost", from_number=CALLER
    )
    assert state.conversation_id
    assert await services.postcall.reconcile(voice.tenant) == 0
    voice.clock.advance(timedelta(hours=7))
    assert await services.postcall.reconcile(voice.tenant) == 1
    row = (await voice.calls())[0]
    assert row.outcome == "unreconciled" and row.ended_at is not None and row.billable_s == 0
    assert services.voice.state_for("retell", "call_lost") is None
    await services.timers.schedule(voice.tenant.id)
    await services.worker.drain()  # the minute job runs the reconciliation hook without error


# ------------------------------------------------------------------ twilio
async def test_twilio_relay_setup_prompt_interrupt_and_handoff(voice_llm: VoiceStack) -> None:
    llm = voice_llm.llm
    assert llm is not None
    llm.load(
        [
            Step.final(reply("Check-in ist ab 15:00 Uhr möglich.", intent="A:checkin")),
            Step.final(reply("Langsam.", intent="A:x"), delay_s=1.0),
            Step.tools(("transfer_to_human", {"reason": "booking"})),
            Step.final(reply("Ich verbinde Sie.", intent="H:transfer")),
        ]
    )
    async with TwilioSimClient(voice_llm.services.voice, voice_llm.tenant) as call:
        assert call.greeting is not None and "KI-System" in call.greeting["token"]
        messages = await call.say("Ab wann ist Check-in?")
        texts = [m for m in messages if m["type"] == "text"]
        assert texts[-1] == {
            "type": "text",
            "token": "Check-in ist ab 15 Uhr möglich.",
            "last": True,
        }
        assert all(not t["last"] for t in texts[:-1])
        call.prompt("Und wie lange?")
        for _ in range(200):  # interrupt once the slow generation is in flight
            if len(llm.requests) >= 2:
                break
            await asyncio.sleep(0.02)
        call.interrupt()
        messages = await call.say("Bitte buchen Sie mir das Zimmer.")
        assert messages[-1]["type"] == "end"
        assert json.loads(messages[-1]["handoffData"]) == {"transfer": "+4969000000"}
    assert (await voice_llm.calls())[0].outcome == "transferred"


async def test_twilio_routes_check_signatures_and_finish_calls(voice: VoiceStack) -> None:
    base = "http://127.0.0.1:8000"
    form = {"From": CALLER, "To": NUMBER, "CallSid": "CA123"}
    good = twilio_cr.compute_signature(TWILIO_TOKEN, f"{base}/voice/twilio/incoming", form)
    denied = await voice.client.post("/voice/twilio/incoming", data=form)
    assert denied.status_code == 403
    response = await voice.client.post(
        "/voice/twilio/incoming", data=form, headers={"X-Twilio-Signature": good}
    )
    assert response.status_code == 200 and response.headers["content-type"].startswith(
        "application/xml"
    )
    assert "<ConversationRelay" in response.text
    assert (
        f'url="ws://127.0.0.1:8000/voice/twilio/relay/{voice.tenant.public_key}/demo-voice-socket-secret"'
        in response.text
    )
    assert 'language="de-DE"' in response.text

    unknown = {"From": CALLER, "To": "+4900000000", "CallSid": "CA124"}
    signature = twilio_cr.compute_signature(TWILIO_TOKEN, f"{base}/voice/twilio/incoming", unknown)
    missing = await voice.client.post(
        "/voice/twilio/incoming", data=unknown, headers={"X-Twilio-Signature": signature}
    )
    assert missing.status_code == 404 and "<Hangup/>" in missing.text

    handoff = {"HandoffData": json.dumps({"transfer": "+4969000000"}), "CallSid": "CA123"}
    signature = twilio_cr.compute_signature(TWILIO_TOKEN, f"{base}/voice/twilio/handoff", handoff)
    dial = await voice.client.post(
        "/voice/twilio/handoff", data=handoff, headers={"X-Twilio-Signature": signature}
    )
    assert "<Dial>+4969000000</Dial>" in dial.text

    async with TwilioSimClient(voice.services.voice, voice.tenant, call_sid="CA123") as call:
        await call.say("Gibt es Frühstück?")
    status = {"CallSid": "CA123", "CallStatus": "completed", "CallDuration": "77", "To": NUMBER}
    signature = twilio_cr.compute_signature(TWILIO_TOKEN, f"{base}/voice/twilio/status", status)
    done = await voice.client.post(
        "/voice/twilio/status", data=status, headers={"X-Twilio-Signature": signature}
    )
    assert done.status_code == 204
    row = (await voice.calls())[0]
    assert row.provider == "twilio_cr" and row.duration_s == 77 and row.outcome == "completed"
    assert row.transcript is not None and "Frühstück" in row.transcript


async def test_stored_end_without_live_state_and_unknown_call(voice: VoiceStack) -> None:
    services = voice.services
    state, _greeting = await services.voice.begin(
        voice.tenant, "retell", "call_restart", from_number=CALLER
    )
    services.voice.forget(state)  # the process restarted: only the row remains
    row = await services.postcall.end_by_provider(
        voice.tenant,
        CallEnd(
            provider="retell", call_id="call_restart", duration_s=40, transcript=[("user", "Hallo")]
        ),
    )
    assert row is not None and row.duration_s == 40 and "user: Hallo" in (row.transcript or "")
    assert (
        await services.postcall.end_by_provider(
            voice.tenant, CallEnd(provider="retell", call_id="never_started", duration_s=1)
        )
        is None
    )


async def test_caller_card_numbers_are_redacted_before_storage(voice: VoiceStack) -> None:
    async with RetellSimClient(voice.services.voice, voice.tenant) as call:
        chunks = await call.say("Meine Karte ist 4111 1111 1111 1111, ich möchte einen Mitarbeiter")
        assert chunks[-1]["content"]
    async with voice.services.ctx.db.scope() as session:
        texts = [t.text for t in (await session.execute(select(MessageText))).scalars()]
    inbound = [t for t in texts if "Karte" in t]
    assert inbound and all("4111" not in t and "[redacted]" in t for t in inbound)
    for row in await voice.calls():
        assert "4111" not in json.dumps(row.transcript) and "4111" not in (row.summary_de or "")
