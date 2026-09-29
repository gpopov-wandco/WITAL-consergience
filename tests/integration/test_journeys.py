"""Journey engine (F13) and template registry (F14) against the database and the fake Graph.

Planning (DST days, quiet hours, caps), jobs and re-planning, send-time gates (opt-in,
suppression, cancellation, review conditions), template refusal with e-mail fallback,
registry sync and statuses, replies to journey buttons, and the end-to-end journey.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from concierge.channels.whatsapp.fake import FakeGraph
from concierge.channels.whatsapp.formatting import language_code
from concierge.clock import FakeClock, format_date
from concierge.context import AppContext
from concierge.conversations.service import Inbound
from concierge.db.models import GuestRequest, Handoff, Message, Outbox, Reservation, Tenant
from concierge.db.session import Database
from concierge.demo.seed import seed_demo
from concierge.journeys.service import (
    JOB_KIND,
    PHASES,
    dedupe_key,
    shift_out_of_quiet_hours,
)
from concierge.notify.port import FakeEmail
from concierge.reservations.service import ReservationInput
from concierge.security.crypto import Keyring
from concierge.security.limiter import MovingWindowLimiter
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.templates.service import TemplateRefused, TemplateService
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F13")

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
PHONE = "+4915120000001"
LANGS = ["de", "en", "ru", "uk", "tr", "it"]


@dataclass
class Stack:
    services: Services
    tenant: Tenant
    graph: FakeGraph
    email: FakeEmail
    clock: FakeClock

    @property
    def profile(self) -> TenantProfile:
        return TenantProfile.model_validate(self.tenant.profile)

    async def refresh(self) -> None:
        tenant = await self.services.tenants.get(self.tenant.slug)
        assert tenant is not None
        self.tenant = tenant

    async def set_profile(self, **journeys: Any) -> None:
        data = self.profile.model_dump(mode="json")
        data["journeys"].update(journeys)
        profile = TenantProfile.model_validate(data)
        await self.services.tenants.update_profile(self.tenant.slug, profile, actor="test")
        await self.refresh()

    async def reservation(self, **kw: Any) -> Reservation:
        base: dict[str, Any] = {
            "res_id": "J-1",
            "guest_name": "Anna Muster",
            "phone": PHONE,
            "email": "anna@example.invalid",
            "lang": "de",
            "arrival": date(2026, 10, 6),
            "departure": date(2026, 10, 8),
            "room_type": "dz",
            "source": "website",
            "adults": 2,
            "whatsapp_optin": True,
            "optin_source": "website_form",
            "optin_at": NOW - timedelta(hours=1),
            "optin_wording_version": "v1",
            "marketing_optin": True,
        }
        base.update(kw)
        return (
            await self.services.reservations.upsert(
                self.tenant.id, ReservationInput(**base), actor="test"
            )
        ).reservation

    async def pending_keys(self) -> list[str]:
        jobs = await self.services.jobs.pending(tenant_id=self.tenant.id, kind=JOB_KIND)
        return [j.dedupe_key or "" for j in jobs]

    async def advance_to(self, when: datetime) -> int:
        self.clock.set(when)
        return await self.services.worker.drain()

    async def outbox_rows(self) -> list[Outbox]:
        async with self.services.ctx.db.scope() as session:
            return list(
                (await session.execute(select(Outbox).order_by(Outbox.created_at))).scalars()
            )


@pytest.fixture
async def stack(db: Database, settings: Settings, keyring: Keyring, clock: FakeClock) -> Stack:
    clock.set(NOW)
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    graph = FakeGraph()
    email = FakeEmail()
    services = build_services(ctx, llm=None, wa_port=graph, demo=True, email=email)
    await seed_demo(services)
    tenant = await services.tenants.require("hotel-beispiel")
    # the fake Graph answers PENDING on creation; approve the standard set as Meta would
    for row in await services.templates.rows(tenant.id):
        await services.templates.set_status(tenant.id, row.name, row.lang, "APPROVED")
    # the fixture reservations of the demo are not part of these tests
    for res_id in ("RES-1001", "RES-1002"):
        row = await services.reservations.by_res_id(tenant.id, res_id)
        assert row is not None
        await services.jobs.cancel(
            dedupe_prefix=services.reservations.job_prefix(tenant.id, row.res_id)
        )
    return Stack(services, tenant, graph, email, clock)


# ------------------------------------------------------------- planning
def test_quiet_hour_shift_helper() -> None:
    window = (
        datetime.strptime("21:00", "%H:%M").time(),
        datetime.strptime("08:00", "%H:%M").time(),
    )
    late = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
    shifted, moved = shift_out_of_quiet_hours(late, window)
    assert moved and shifted == datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    early = datetime(2026, 10, 6, 7, 30, tzinfo=UTC)
    assert shift_out_of_quiet_hours(early, window) == (
        datetime(2026, 10, 6, 8, 0, tzinfo=UTC),
        True,
    )
    fine = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    assert shift_out_of_quiet_hours(fine, window) == (fine, False)


async def test_schedule_is_dst_correct_and_orders_steps(stack: Stack) -> None:
    engine = stack.services.journeys
    await stack.set_profile(caps={"before": 4, "during": 1, "after": 2})
    reservation = await stack.reservation(
        res_id="DST-1", arrival=date(2026, 10, 25), departure=date(2026, 10, 27)
    )
    steps = {p.step: p for p in engine.schedule(stack.profile, reservation, now=NOW)}
    assert steps["booking_followup"].run_at == NOW + timedelta(minutes=15)
    assert steps["t_minus_7"].run_at == datetime(2026, 10, 18, 8, 0, tzinfo=UTC)  # CEST
    assert steps["t_minus_2"].run_at == datetime(2026, 10, 23, 8, 0, tzinfo=UTC)
    assert steps["registration_link"].run_at == datetime(2026, 10, 23, 8, 2, tzinfo=UTC)
    assert steps["t_0"].run_at == datetime(
        2026, 10, 25, 9, 0, tzinfo=UTC
    )  # clocks went back at 03:00
    assert steps["stay_check"].run_at == datetime(2026, 10, 26, 17, 0, tzinfo=UTC)
    assert steps["checkout"].run_at == datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
    assert steps["review_request"].run_at == datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
    assert steps["review_request"].category == "marketing" and steps["t_0"].category == "utility"
    assert all(
        k.dedupe_key == dedupe_key(stack.tenant.id, "DST-1", k.step, date(2026, 10, 25))
        for k in steps.values()
    )
    spring = await stack.reservation(
        res_id="DST-2", arrival=date(2027, 3, 28), departure=date(2027, 3, 30)
    )
    steps = {p.step: p for p in engine.schedule(stack.profile, spring, now=NOW)}
    assert steps["t_minus_2"].run_at == datetime(2027, 3, 26, 9, 0, tzinfo=UTC)  # CET
    assert steps["t_0"].run_at == datetime(
        2027, 3, 28, 8, 0, tzinfo=UTC
    )  # CEST after 02:00 -> 03:00
    assert steps["checkout"].run_at == datetime(2027, 3, 30, 6, 0, tzinfo=UTC)
    ordered = [p.step for p in engine.schedule(stack.profile, spring, now=NOW)]
    assert ordered == [
        "booking_followup",
        "t_minus_7",
        "t_minus_2",
        "registration_link",
        "t_0",
        "stay_check",
        "checkout",
        "review_request",
    ]


async def test_schedule_respects_quiet_hours_caps_and_optin(stack: Stack) -> None:
    engine = stack.services.journeys
    reservation = await stack.reservation(res_id="Q-1")
    await stack.set_profile(checkout={"at": "07:30"})
    steps = {p.step: p for p in engine.schedule(stack.profile, reservation, now=NOW)}
    assert steps["checkout"].shifted and steps["checkout"].run_at == datetime(
        2026, 10, 8, 6, 0, tzinfo=UTC
    )
    late_evening = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)  # 22:00 local, inside 21:00-08:00
    steps = {p.step: p for p in engine.schedule(stack.profile, reservation, now=late_evening)}
    assert steps["booking_followup"].shifted and steps["booking_followup"].run_at == datetime(
        2026, 9, 30, 6, 0, tzinfo=UTC
    )
    # before-arrival cap 3 with registration enabled: T-7 is dropped first (here already past)
    far = await stack.reservation(
        res_id="Q-2", arrival=date(2026, 11, 10), departure=date(2026, 11, 12)
    )
    names = [
        p.step for p in engine.schedule(stack.profile, far, now=NOW) if PHASES[p.step] == "before"
    ]
    assert names == ["booking_followup", "t_minus_2", "registration_link"]
    await stack.set_profile(caps={"before": 2, "during": 0, "after": 1})
    names = [p.step for p in engine.schedule(stack.profile, far, now=NOW)]
    assert names == ["t_minus_2", "registration_link", "t_0", "checkout"]
    await stack.set_profile(caps={"before": 3, "during": 1, "after": 2})
    no_optin = await stack.reservation(res_id="Q-3", whatsapp_optin=False)
    assert engine.schedule(stack.profile, no_optin, now=NOW) == []
    unrecorded = await stack.reservation(res_id="Q-4", optin_source=None, optin_at=None)
    assert (
        unrecorded.whatsapp_optin is False
        and engine.schedule(stack.profile, unrecorded, now=NOW) == []
    )
    no_marketing = await stack.reservation(res_id="Q-5", marketing_optin=False)
    assert "review_request" not in [
        p.step for p in engine.schedule(stack.profile, no_marketing, now=NOW)
    ]
    short = await stack.reservation(res_id="Q-6", departure=date(2026, 10, 7))
    assert "stay_check" not in [p.step for p in engine.schedule(stack.profile, short, now=NOW)]


async def test_plan_enqueues_replans_and_cancels(stack: Stack) -> None:
    reservation = await stack.reservation()
    keys = await stack.pending_keys()
    assert len(keys) == 7 and all(k.startswith(f"journey:{stack.tenant.id}:J-1:") for k in keys)
    assert dedupe_key(stack.tenant.id, "J-1", "t_0", date(2026, 10, 6)) in keys
    await stack.services.journeys.plan(stack.tenant, reservation)
    assert len(await stack.pending_keys()) == 7
    moved = await stack.reservation(arrival=date(2026, 10, 13), departure=date(2026, 10, 15))
    keys = await stack.pending_keys()
    assert (
        len(keys) == 7
        and all(k.endswith(":2026-10-13") for k in keys)
        and moved.arrival == date(2026, 10, 13)
    )
    await stack.reservation(
        arrival=date(2026, 10, 13), departure=date(2026, 10, 15), status="cancelled"
    )
    assert await stack.pending_keys() == []
    await stack.reservation(
        arrival=date(2026, 10, 13), departure=date(2026, 10, 15), status="booked"
    )
    assert len(await stack.pending_keys()) == 7


# --------------------------------------------------------------- sending
async def test_end_to_end_journey_in_six_languages(stack: Stack) -> None:
    engine = stack.services.journeys
    for index, lang in enumerate(LANGS):
        phone = f"+49151{index:08d}"
        res_id = f"E2E-{lang}"
        reservation = await stack.reservation(
            res_id=res_id, phone=phone, lang=lang, email=f"{lang}@example.invalid"
        )
        plan = engine.schedule(stack.profile, reservation, now=NOW)
        assert [p.step for p in plan] == [
            "booking_followup",
            "t_minus_2",
            "registration_link",
            "t_0",
            "stay_check",
            "checkout",
            "review_request",
        ]
        sent_before = len(stack.graph.sent)
        for step in plan:
            await stack.advance_to(step.run_at)
            messages = stack.graph.messages_to(phone.lstrip("+"))
            assert messages, (lang, step.step)
            payload = messages[-1].payload
            assert payload["type"] == "template" and payload["template"]["name"] == step.template
            assert payload["template"]["language"]["code"] == language_code(lang)
            params = {
                p["parameter_name"]: p["text"]
                for c in payload["template"].get("components", [])
                if c["type"] == "body"
                for p in c["parameters"]
            }
            if "arrival" in params:
                assert params["arrival"] == format_date(date(2026, 10, 6), lang)
            if step.step == "registration_link":
                assert params["link"].startswith("http://127.0.0.1:8000/r/")
            if step.step == "review_request":
                assert params["review_url"] == "https://example.invalid/review"
            assert 6 <= step.local_at.hour < 21
        assert len(stack.graph.sent) - sent_before == 7
        stack.clock.set(NOW)
    rows = await stack.outbox_rows()
    categories = {
        r.payload["template"]["name"]: r.category
        for r in rows
        if r.payload.get("type") == "template"
    }
    assert (
        categories["gc_review_request"] == "marketing" and categories["gc_arrival_day"] == "utility"
    )
    assert all(r.state == "sent" for r in rows)
    async with stack.services.ctx.db.scope() as session:
        stored = list(
            (await session.execute(select(Message).where(Message.kind == "template"))).scalars()
        )
    assert len(stored) == 42 and all(m.direction == "out" for m in stored)
    outcomes = [o.outcome for o in engine.outcomes]
    assert outcomes.count("sent") == 42 and "cap" not in outcomes


async def test_send_time_gates_stop_cancel_and_review_conditions(stack: Stack) -> None:
    engine = stack.services.journeys
    reservation = await stack.reservation()
    plan = {p.step: p for p in engine.schedule(stack.profile, reservation, now=NOW)}
    await stack.advance_to(plan["booking_followup"].run_at)
    assert len(stack.graph.messages_to(PHONE.lstrip("+"))) == 1
    # the guest writes STOP: pending steps are cancelled, a later step would be suppressed
    result = await stack.services.brain.respond(
        stack.tenant,
        Inbound(
            channel="whatsapp",
            channel_key=PHONE.lstrip("+"),
            text="STOP",
            kind="text",
            external_id=None,
            phone=PHONE,
        ),
    )
    assert "stop" in result.events and await stack.pending_keys() == []
    await stack.services.jobs.enqueue(
        JOB_KIND,
        {
            "tenant_id": stack.tenant.id,
            "reservation_id": reservation.id,
            "res_id": "J-1",
            "step": "t_0",
            "arrival": "2026-10-06",
        },
        run_at=plan["t_0"].run_at,
        tenant_id=stack.tenant.id,
        dedupe_key="manual:t0",
    )
    await stack.advance_to(plan["t_0"].run_at)
    assert (
        engine.outcomes[-1].outcome in {"suppressed", "paused"}
        and len(stack.graph.messages_to(PHONE.lstrip("+"))) == 1
    )
    # a second guest with an open complaint gets no review request
    other = await stack.reservation(res_id="J-2", phone="+4915199999999")
    plan2 = {p.step: p for p in engine.schedule(stack.profile, other, now=stack.clock.now())}
    await stack.advance_to(plan2["stay_check"].run_at)
    assert (
        stack.graph.messages_to("4915199999999")[-1].payload["template"]["name"] == "gc_stay_check"
    )
    issue = await stack.services.brain.respond(
        stack.tenant,
        Inbound(
            channel="whatsapp",
            channel_key="4915199999999",
            text="Es gibt ein Problem",
            kind="button",
            external_id=None,
            phone="+4915199999999",
            button_id="stay_issue",
        ),
    )
    assert issue.handoff_id is not None and "handoff:P1:stay_check" in issue.events
    async with stack.services.ctx.db.scope() as session:
        handoff = await session.get(Handoff, issue.handoff_id)
    assert handoff is not None and handoff.priority == "P1" and handoff.trigger == "stay_check"
    await stack.advance_to(plan2["review_request"].run_at)
    names = [m.payload["template"]["name"] for m in stack.graph.messages_to("4915199999999")]
    assert "gc_review_request" not in names and engine.outcomes[-1].outcome == "open_complaint"
    # cancellation stops everything for a third guest
    third = await stack.reservation(
        res_id="J-3", phone="+4915188888888", arrival=date(2026, 11, 3), departure=date(2026, 11, 5)
    )
    assert len([k for k in await stack.pending_keys() if ":J-3:" in k]) == 7
    await stack.reservation(
        res_id="J-3",
        phone="+4915188888888",
        arrival=date(2026, 11, 3),
        departure=date(2026, 11, 5),
        status="cancelled",
    )
    assert [
        k for k in await stack.pending_keys() if ":J-3:" in k
    ] == [] and third.status == "booked"


async def test_eta_button_records_window_and_request(stack: Stack) -> None:
    reservation = await stack.reservation()
    result = await stack.services.brain.respond(
        stack.tenant,
        Inbound(
            channel="whatsapp",
            channel_key=PHONE.lstrip("+"),
            text="18 bis 22 Uhr",
            kind="button",
            external_id=None,
            phone=PHONE,
            button_id="eta_18_22",
        ),
    )
    assert "journey:eta_18_22" in result.events
    assert result.messages[-1].text.startswith(
        "Danke, ich habe „18 bis 22 Uhr“ als Ankunftszeit notiert"
    )
    async with stack.services.ctx.db.scope() as session:
        row = await session.get(Reservation, reservation.id)
        requests = list(
            (
                await session.execute(select(GuestRequest).where(GuestRequest.kind == "eta"))
            ).scalars()
        )
    assert row is not None and row.meta["eta_window"] == "18_22" and row.guest_id == result.guest_id
    assert len(requests) == 1 and requests[0].details["window"] == "18_22"
    stranger = await stack.services.brain.respond(
        stack.tenant,
        Inbound(
            channel="whatsapp",
            channel_key="4917700000000",
            text="Vor 18 Uhr",
            kind="button",
            external_id=None,
            phone="+4917700000000",
            button_id="eta_before_18",
        ),
    )
    assert "keine Buchung" in stranger.messages[-1].text


async def test_template_refusal_uses_email_fallback_or_skips_with_alert(stack: Stack) -> None:
    engine = stack.services.journeys
    await stack.services.templates.set_status(
        stack.tenant.id, "gc_arrival_day", "de", "REJECTED", reason="policy"
    )
    with pytest.raises(TemplateRefused):
        await stack.services.templates.require_approved(stack.tenant.id, "gc_arrival_day", "de")
    reservation = await stack.reservation()
    plan = {p.step: p for p in engine.schedule(stack.profile, reservation, now=NOW)}
    await stack.advance_to(plan["t_0"].run_at)
    assert engine.outcomes[-1].outcome == "email_fallback"
    mail = stack.email.to("anna@example.invalid")
    assert (
        len(mail) == 1
        and "Willkommen im Hotel Beispiel" in mail[0].text
        and mail[0].reply_to == "reception@example.invalid"
    )
    assert not any(
        m.payload["template"]["name"] == "gc_arrival_day"
        for m in stack.graph.messages_to(PHONE.lstrip("+"))
    )
    no_mail = await stack.reservation(res_id="J-9", phone="+4915177777777", email=None)
    plan = {p.step: p for p in engine.schedule(stack.profile, no_mail, now=stack.clock.now())}
    await stack.advance_to(plan["t_0"].run_at)
    assert engine.outcomes[-1].outcome == "skipped"
    assert any(kind == "journey_step_skipped" for kind, _, _ in stack.services.alerts)


async def test_registry_sync_statuses_and_budget(stack: Stack) -> None:
    services = stack.services
    rows = await services.templates.rows(stack.tenant.id)
    assert len(rows) == 60 and all(r.status == "APPROVED" for r in rows)
    assert await services.templates.install_standard(stack.tenant.id, actor="test") == 0
    assert len(stack.graph.templates) == 60
    report = await services.templates.sync(stack.tenant.id, "demo-waba", actor="test")
    assert len(report.existing) == 60 and report.created == [] and report.errors == []
    rows = await services.templates.rows(stack.tenant.id)
    assert all(r.status == "PENDING" and r.meta_template_id for r in rows)
    assert await services.templates.approved(stack.tenant.id, "gc_stay_check", "it") is None
    row = await services.templates.set_status(stack.tenant.id, "gc_stay_check", "it", "APPROVED")
    assert row.last_status_at == stack.clock.now()
    payload, text = await services.templates.payload(
        stack.tenant.id, "4915100000000", "gc_stay_check", "it", {"hotel": "Hotel Beispiel"}
    )
    assert payload["template"]["name"] == "gc_stay_check" and "Hotel Beispiel" in text
    limited = TemplateService(
        services.ctx.db,
        services.ctx.clock,
        FakeGraph(),
        services.secrets,
        limiter=MovingWindowLimiter(),
    )
    other = await services.tenants.create(
        TenantProfile.model_validate(
            dict(
                stack.profile.model_dump(mode="json"),
                slug="other",
                name="Other",
                whatsapp={
                    "waba_id": "W2",
                    "phone_number_id": "pn2",
                    "display_phone_number": "+49 69 000002",
                },
                widget={"public_key": "pk_other"},
            )
        ),
        actor="test",
    )
    await services.secrets.set(other.id, "wa_access_token", "tok", actor="test")
    assert await limited.install_standard(other.id, actor="test") == 60
    limited.limiter.hit(f"tpl:{other.id}", "100 per hour")
    for _ in range(99):
        limited.limiter.hit(f"tpl:{other.id}", "100 per hour")
    report = await limited.sync(other.id, "W2", actor="test")
    assert len(report.skipped_budget) == 60 and report.created == []


async def test_preview_and_test_send(stack: Stack) -> None:
    engine = stack.services.journeys
    reservation = await stack.reservation()
    lines = await engine.preview(stack.tenant, reservation)
    assert [line.step for line in lines][:2] == ["booking_followup", "t_minus_2"]
    followup = lines[0]
    assert followup.text.startswith(
        "Guten Tag Anna, vielen Dank für Ihre Buchung im Hotel Beispiel vom 06.10.2026 bis 08.10.2026"
    )
    assert all("{{" not in line.text for line in lines)
    _outbox_id, text = await engine.test_send(stack.tenant, "+4915100000001", "t_0", "en")
    await stack.services.worker.drain()
    sent = stack.graph.messages_to("4915100000001")
    assert (
        len(sent) == 1
        and sent[0].payload["template"]["name"] == "gc_arrival_day"
        and "Welcome to Hotel Beispiel" in text
    )


async def test_key_safe_code_only_after_late_eta_in_verified_conversation(stack: Stack) -> None:
    """F11: the key-safe code goes out at T-0 12:00, only for a late ETA and a verified chat."""
    services = stack.services
    reservation = await stack.reservation()
    assert (
        "journey:" + stack.tenant.id + ":J-1:key_safe:2026-10-06" not in await stack.pending_keys()
    )
    result = await services.brain.respond(
        stack.tenant,
        Inbound(
            channel="whatsapp",
            channel_key=PHONE.lstrip("+"),
            text="Nach 22 Uhr",
            kind="button",
            external_id=None,
            phone=PHONE,
            button_id="eta_after_22",
        ),
    )
    row = await services.reservations.get(stack.tenant.id, reservation.id)
    assert row is not None and row.meta["late_arrival"] is True and row.guest_id == result.guest_id
    key = dedupe_key(stack.tenant.id, "J-1", "key_safe", date(2026, 10, 6))
    assert key in await stack.pending_keys()
    silence = await services.jobs.pending(tenant_id=stack.tenant.id, kind="eta_silence")
    assert len(silence) == 1
    # 23:59 local plus two hours -> 01:59 local on 2026-10-07 = 23:59 UTC on 2026-10-06
    assert silence[0].run_at == datetime(2026, 10, 6, 23, 59, tzinfo=UTC)

    # not verified: the step runs at 12:00 local and is refused
    await stack.advance_to(datetime(2026, 10, 6, 10, 0, tzinfo=UTC))
    outcomes = {(o.step, o.outcome) for o in services.journeys.outcomes}
    assert ("key_safe", "not_verified") in outcomes
    assert not any(
        r.payload.get("template", {}).get("name") == "gc_key_safe"
        for r in await stack.outbox_rows()
    )

    # a verified conversation with a late ETA receives the code from the KB
    second = await stack.reservation(res_id="J-2", phone="+4915120000002")
    late = await services.brain.respond(
        stack.tenant,
        Inbound(
            channel="whatsapp",
            channel_key="4915120000002",
            text="Nach 22 Uhr",
            kind="button",
            external_id=None,
            phone="+4915120000002",
            button_id="eta_after_22",
        ),
    )
    await services.conversations.update(
        stack.tenant.id, late.conversation_id, verified_reservation_id=second.id
    )
    services.journeys.outcomes.clear()
    await stack.advance_to(datetime(2026, 10, 6, 10, 1, tzinfo=UTC))
    sent = [
        r
        for r in await stack.outbox_rows()
        if r.payload.get("template", {}).get("name") == "gc_key_safe"
    ]
    assert len(sent) == 1
    params = {
        p["parameter_name"]: p["text"]
        for c in sent[0].payload["template"]["components"]
        for p in c.get("parameters", [])
    }
    assert params["code"] == "4711" and params["latest_reception"] == "22:00"
    assert ("key_safe", "sent") in {(o.step, o.outcome) for o in services.journeys.outcomes}


async def test_eta_silence_alerts_staff_unless_the_guest_wrote(stack: Stack) -> None:
    services = stack.services
    await stack.reservation()
    quiet = await stack.reservation(res_id="J-2", phone="+4915120000002")
    for phone, window in ((PHONE, "eta_18_22"), ("+4915120000002", "eta_18_22")):
        await services.brain.respond(
            stack.tenant,
            Inbound(
                channel="whatsapp",
                channel_key=phone.lstrip("+"),
                text="18 bis 22 Uhr",
                kind="button",
                external_id=None,
                phone=phone,
                button_id=window,
            ),
        )
    jobs = await services.jobs.pending(tenant_id=stack.tenant.id, kind="eta_silence")
    # 22:00 local plus two hours = 00:00 local on 2026-10-07 = 22:00 UTC on 2026-10-06
    assert {j.run_at for j in jobs} == {datetime(2026, 10, 6, 22, 0, tzinfo=UTC)}
    # the first guest writes after the window ended, the second stays silent
    stack.clock.set(datetime(2026, 10, 6, 20, 30, tzinfo=UTC))
    await services.brain.respond(
        stack.tenant,
        Inbound(
            channel="whatsapp",
            channel_key=PHONE.lstrip("+"),
            text="Wir sind da, danke!",
            kind="text",
            external_id=None,
            phone=PHONE,
        ),
    )
    await stack.advance_to(datetime(2026, 10, 6, 22, 0, tzinfo=UTC))
    async with services.ctx.db.scope() as session:
        handoffs = list(
            (
                await session.execute(select(Handoff).where(Handoff.trigger == "eta_silence"))
            ).scalars()
        )
    assert len(handoffs) == 1 and handoffs[0].priority == "P1"
    assert quiet.res_id in handoffs[0].summary and "18" in handoffs[0].summary
    outcomes = {(o.step, o.outcome) for o in services.journeys.outcomes}
    assert ("eta_silence", "guest_wrote") in outcomes and ("eta_silence", "alerted") in outcomes
    assert await services.jobs.pending(tenant_id=stack.tenant.id, kind="eta_silence") == []
