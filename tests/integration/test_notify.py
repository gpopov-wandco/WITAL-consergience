"""Staff alerts, SLA timers and the e-mail outbox (F17, F18)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.conftest import DemoStack

from concierge.demo.seed import DEMO_SECRETS
from concierge.demo.wa_sim import PayloadFactory, WaSimClient
from concierge.notify.outbox import RetryableEmail, footer
from concierge.tenants.profile import HandoffContact, TenantProfile

pytestmark = pytest.mark.feature("F17")

GUEST = "4915112345678"
DESK_WA = "4969000001"
DESK_EMAIL = "desk@example.invalid"


async def conversation_for(
    stack: DemoStack, text: str = "Gibt es einen Parkplatz?", *, guest_number: str = GUEST
) -> str:
    factory = PayloadFactory(stack.tenant.phone_number_id or "")
    sim = WaSimClient(
        "http://testserver", DEMO_SECRETS["wa_app_secret"], transport=stack.client._transport
    )
    assert (await sim.post(factory.text(guest_number, text))).status_code == 200
    await stack.services.worker.drain()
    guest = await stack.services.conversations.get_guest(stack.tenant.id, "whatsapp", guest_number)
    assert guest is not None
    conversation = await stack.services.conversations.open_conversation(
        stack.tenant.id, guest.id, "whatsapp", lang="de"
    )
    return conversation.id


def profile_with(profile: TenantProfile, **changes: object) -> TenantProfile:
    data = profile.model_dump(mode="json")
    data.update(changes)
    return TenantProfile.model_validate(data)


# ----------------------------------------------------------------- alerts
async def test_new_handoff_alerts_staff_by_whatsapp_and_email(demo_stack: DemoStack) -> None:
    services = demo_stack.services
    conversation_id = await conversation_for(demo_stack)
    created = await services.handoffs.create(
        demo_stack.tenant.id,
        conversation_id,
        priority="P1",
        trigger="guest_request",
        reason="Gast möchte mit einer Person sprechen",
        profile=demo_stack.profile,
    )
    records = [r for r in services.alerts_out.sent if r.handoff_id == created.id]
    assert {(r.channel, r.outcome) for r in records} == {
        ("whatsapp", "queued"),
        ("email", "queued"),
    }
    await services.worker.drain()
    wa = demo_stack.state.messages_to(DESK_WA)
    assert len(wa) == 1 and wa[0]["payload"]["template"]["name"] == "gc_staff_alert"
    body = str(wa[0]["payload"])
    assert "P1" in body and "/admin/conversations/" in body
    assert GUEST not in body  # the guest's number never travels in an alert
    mails = demo_stack.email.to(DESK_EMAIL)
    assert len(mails) == 1 and mails[0].subject.startswith("[P1] Hotel Beispiel")
    assert "/admin/conversations/" in mails[0].text and "Hotel Beispiel" in mails[0].text

    # the same handoff never alerts twice (idempotent outbox rows)
    await services.alerts_out.on_handoff(
        demo_stack.tenant,
        demo_stack.profile,
        (await services.handoffs.get(demo_stack.tenant.id, created.id)),  # type: ignore[arg-type]
    )
    await services.worker.drain()
    assert len(demo_stack.state.messages_to(DESK_WA)) == 1
    assert len(demo_stack.email.to(DESK_EMAIL)) == 1


def test_contacts_outside_staffed_hours(demo_stack: DemoStack) -> None:
    alerts = demo_stack.services.alerts_out
    profile = profile_with(
        demo_stack.profile,
        handoff={
            "sla": {"p1_min": 10, "p2_min": 120},
            "contacts": [
                {"name": "Desk", "whatsapp": "+49 69 000001", "email": DESK_EMAIL},
                {"name": "Night", "whatsapp": "+49 69 000002", "on_call": True},
            ],
        },
    )
    day = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)  # 12:00 Berlin, staffed
    night = datetime(2026, 9, 29, 23, 30, tzinfo=UTC)  # 01:30 Berlin, unstaffed
    assert [c.name for c in alerts.contacts_for(profile, "P1", day)] == ["Desk", "Night"]
    assert alerts.contacts_for(profile, "P1", night) == []
    assert alerts.contacts_for(profile, "P2", night) == []
    assert [c.name for c in alerts.contacts_for(profile, "P0", night)] == ["Night"]
    assert [c.name for c in alerts.contacts_for(profile, "P0", day)] == ["Desk", "Night"]
    # repeats rotate through the pool
    assert [c.name for c in alerts.contacts_for(profile, "P0", day, level=1)] == ["Night"]
    assert [c.name for c in alerts.contacts_for(profile, "P0", day, level=2)] == ["Desk"]
    none = profile_with(demo_stack.profile, handoff={"contacts": []})
    assert alerts.contacts_for(none, "P0", day) == []
    assert isinstance(profile.handoff.contacts[0], HandoffContact)


async def test_p2_outside_staffed_hours_goes_by_email_only(demo_stack: DemoStack) -> None:
    services = demo_stack.services
    demo_stack.clock.set(datetime(2026, 9, 29, 23, 30, tzinfo=UTC))
    conversation_id = await conversation_for(demo_stack)
    created = await services.handoffs.create(
        demo_stack.tenant.id,
        conversation_id,
        priority="P2",
        trigger="unclear",
        reason="unklare Frage",
        profile=demo_stack.profile,
    )
    records = [r for r in services.alerts_out.sent if r.handoff_id == created.id]
    assert [(r.channel, r.outcome) for r in records] == [("email", "queued")]
    handoff = await services.handoffs.get(demo_stack.tenant.id, created.id)
    assert handoff is not None and handoff.sla_due_at is not None
    # the SLA clock starts at the next staffed morning (07:00 Berlin = 05:00 UTC)
    assert handoff.sla_due_at == datetime(2026, 9, 30, 5, 0, tzinfo=UTC) + timedelta(minutes=120)


# ----------------------------------------------------------------- timers
async def test_timer_job_escalates_breached_sla_and_repeats_p0(demo_stack: DemoStack) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    conversation_id = await conversation_for(demo_stack)
    p1 = await services.handoffs.create(
        tenant.id,
        conversation_id,
        priority="P1",
        trigger="guest_request",
        reason="Rückruf",
        profile=demo_stack.profile,
    )
    await services.timers.schedule(tenant.id)
    await services.worker.drain()
    handoff = await services.handoffs.get(tenant.id, p1.id)
    assert handoff is not None and not handoff.sla_breached and handoff.escalation_level == 0

    demo_stack.clock.advance(timedelta(minutes=11))
    await services.worker.drain()  # the rescheduled minute job is due now
    handoff = await services.handoffs.get(tenant.id, p1.id)
    assert handoff is not None and handoff.sla_breached and handoff.status == "escalated"
    assert handoff.escalation_level == 1
    escalations = [
        r for r in services.alerts_out.sent if r.handoff_id == p1.id and r.channel == "whatsapp"
    ]
    assert len(escalations) == 2  # the first alert and the escalation
    await services.worker.drain()
    assert len(demo_stack.state.messages_to(DESK_WA)) == 2

    # acknowledged handoffs stop escalating
    await services.handoffs.acknowledge(tenant.id, p1.id, user_id="usr_test")
    demo_stack.clock.advance(timedelta(minutes=2))
    await services.worker.drain()
    handoff = await services.handoffs.get(tenant.id, p1.id)
    assert handoff is not None and handoff.escalation_level == 1

    # an unacknowledged P0 repeats every p0_repeat_min minutes
    emergency = await conversation_for(
        demo_stack, "Hilfe, es brennt in meinem Zimmer", guest_number="4915120000002"
    )
    p0 = await services.handoffs.open_for_conversation(tenant.id, emergency)
    assert p0 is not None and p0.priority == "P0" and p0.next_alert_at is not None
    before = len([r for r in services.alerts_out.sent if r.handoff_id == p0.id])
    demo_stack.clock.advance(timedelta(minutes=demo_stack.profile.handoff.sla.p0_repeat_min + 1))
    await services.worker.drain()
    after = len([r for r in services.alerts_out.sent if r.handoff_id == p0.id])
    assert after > before
    p0 = await services.handoffs.get(tenant.id, p0.id)
    assert p0 is not None and p0.escalation_level >= 1 and p0.status in {"open", "escalated"}

    # an emergency in the acknowledged conversation re-alerts and reopens the case
    sent_before = len(services.alerts_out.sent)
    upgraded = await services.handoffs.create(
        tenant.id,
        conversation_id,
        priority="P0",
        trigger="emergency",
        reason="Notfall",
        profile=demo_stack.profile,
    )
    assert not upgraded.created and upgraded.id == p1.id and upgraded.priority == "P0"
    assert len(services.alerts_out.sent) > sent_before
    reopened = await services.handoffs.get(tenant.id, p1.id)
    assert reopened is not None and reopened.status == "escalated"
    assert reopened.next_alert_at is not None


async def test_timer_jobs_are_scheduled_once_per_tenant(demo_stack: DemoStack) -> None:
    services = demo_stack.services
    assert await services.timers.schedule_all() == 1
    await services.timers.schedule_all()
    pending = await services.jobs.pending(kind="handoff_timers")
    assert len(pending) == 1


# ------------------------------------------------------------ e-mail outbox
async def test_email_outbox_sends_retries_and_dies(demo_stack: DemoStack) -> None:
    services, tenant, profile = demo_stack.services, demo_stack.tenant, demo_stack.profile
    outbox = services.email_outbox
    assert outbox.configured
    row = await outbox.enqueue(
        tenant.id,
        profile,
        to="guest@example.invalid",
        subject="Ihre Anreise",
        text="Guten Tag,\nwir freuen uns auf Sie.",
        idempotency_key="test:1",
        business_initiated=True,
    )
    again = await outbox.enqueue(
        tenant.id,
        profile,
        to="x@example.invalid",
        subject="dup",
        text="dup",
        idempotency_key="test:1",
    )
    assert again.id == row.id
    await services.worker.drain()
    rows = {r.id: r for r in await outbox.rows(tenant.id)}
    assert rows[row.id].state == "sent" and rows[row.id].provider_message_id == "fake-1"
    message = demo_stack.email.to("guest@example.invalid")[0]
    assert message.reply_to == "reception@example.invalid"
    assert "Keine weiteren Nachrichten / no more messages" in message.text
    assert message.text.rstrip().endswith("reception@example.invalid")

    demo_stack.email.fail_next = 1
    retry = await outbox.enqueue(
        tenant.id, profile, to="b@example.invalid", subject="s", text="t", idempotency_key="test:2"
    )
    with pytest.raises(RetryableEmail):
        await outbox.send(tenant.id, retry.id)
    assert (await outbox.send(tenant.id, retry.id)).state == "sent"

    demo_stack.email.fail_next = 10
    dead = await outbox.enqueue(
        tenant.id, profile, to="c@example.invalid", subject="s", text="t", idempotency_key="test:3"
    )
    await services.worker.drain()
    states = {r.id: r.state for r in await outbox.rows(tenant.id)}
    assert states[dead.id] in {"queued", "dead"}
    for _ in range(6):
        demo_stack.clock.advance(timedelta(minutes=30))
        await services.worker.drain()
    states = {r.id: r.state for r in await outbox.rows(tenant.id)}
    assert states[dead.id] == "dead"
    assert (await outbox.send(tenant.id, "out_missing")).state == "missing"


def test_footer_carries_identity_and_stop_address(demo_stack: DemoStack) -> None:
    text = footer(demo_stack.profile, business_initiated=True)
    assert "Hotel Beispiel" in text and "reception@example.invalid" in text
    assert "https://example.invalid/impressum" in text
    assert "no more messages" in text
    assert "no more messages" not in footer(demo_stack.profile, business_initiated=False)


async def test_booking_inquiry_emails_the_hotel_with_reply_to_guest(demo_stack: DemoStack) -> None:
    from datetime import date

    from concierge.availability.booking import InquiryForm
    from concierge.availability.models import Occupancy, Stay

    services = demo_stack.services
    form = InquiryForm(
        name="Anna Muster",
        email="anna@example.invalid",
        phone="",
        stay=Stay(date(2026, 10, 20), date(2026, 10, 22)),
        occupancy=Occupancy(adults=2, children=0),
        wish="Ruhiges Zimmer, bitte",
        consent=True,
        lang="de",
        room_type="dz",
    )
    result = await services.inquiries.create(
        demo_stack.tenant, demo_stack.profile, form, conversation_id=None
    )
    assert not result.duplicate
    await services.worker.drain()
    mails = demo_stack.email.to("reception@example.invalid")
    assert len(mails) == 1
    assert mails[0].reply_to == "anna@example.invalid"
    assert "2026-10-20" in mails[0].subject and "Ruhiges Zimmer" in mails[0].text
    assert "no more messages" not in mails[0].text  # not business-initiated marketing
    async with services.ctx.db.tenant_scope(demo_stack.tenant.id) as session:
        from concierge.db.models import GuestRequest

        row = await session.get(GuestRequest, result.request_id)
    assert row is not None and row.details["email_pending"] is False
    again = await services.inquiries.create(
        demo_stack.tenant, demo_stack.profile, form, conversation_id=None
    )
    assert again.duplicate
    await services.worker.drain()
    assert len(demo_stack.email.to("reception@example.invalid")) == 1
