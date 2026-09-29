"""GDPR tooling (F25): retention with frozen time, subject search, export, erasure,
restriction, consent log, registry coverage."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.integration.conftest import DemoStack

from concierge.db.models import (
    GUEST_DATA_TABLES,
    Consent,
    DeletionLog,
    Guest,
    Message,
    MessageText,
    Suppression,
    VoiceCall,
)
from concierge.demo.seed import DEMO_SECRETS
from concierge.demo.wa_sim import PayloadFactory, WaSimClient
from concierge.gdpr.service import ERASED, EXPORTED, KEPT_BY_DESIGN, GdprError
from concierge.reservations.service import ReservationInput

pytestmark = pytest.mark.feature("F25")

GUEST = "4915112345678"
PHONE = "+4915112345678"


async def chat(stack: DemoStack, text: str) -> None:
    factory = PayloadFactory(stack.tenant.phone_number_id or "", clock=stack.clock)
    sim = WaSimClient(
        "http://testserver", DEMO_SECRETS["wa_app_secret"], transport=stack.client._transport
    )
    assert (await sim.post(factory.text(GUEST, text))).status_code == 200
    await stack.services.worker.drain()


def test_registry_covers_every_guest_data_table() -> None:
    covered = EXPORTED | set(KEPT_BY_DESIGN)
    missing_export = [t.__name__ for t in GUEST_DATA_TABLES if t not in covered]
    missing_erase = [t.__name__ for t in GUEST_DATA_TABLES if t not in ERASED | set(KEPT_BY_DESIGN)]
    assert missing_export == [] and missing_erase == []
    assert Suppression in KEPT_BY_DESIGN and Consent in KEPT_BY_DESIGN


async def test_search_export_restrict_and_erase_keep_suppression(demo_stack: DemoStack) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    await chat(demo_stack, "Gibt es einen Parkplatz?")
    await chat(demo_stack, "STOP")  # suppression row for this number
    subject = await services.gdpr.search(tenant.id, phone=PHONE)
    assert subject.found and len(subject.guest_ids) == 1 and len(subject.conversation_ids) == 1
    assert subject.reservation_ids  # RES-1001 belongs to the same number
    by_res = await services.gdpr.search(tenant.id, res_id="RES-1001")
    assert by_res.reservation_ids == subject.reservation_ids
    none = await services.gdpr.search(tenant.id, email="nobody@example.invalid")
    assert not none.found
    with pytest.raises(GdprError):
        await services.gdpr.search(tenant.id)

    data = await services.gdpr.export(tenant.id, subject)
    assert data["guests"][0]["phone"] == PHONE
    assert any(
        "Parkplatz" in (m["text"] or "") for c in data["conversations"] for m in c["messages"]
    )
    assert data["reservations"][0]["res_id"] == "RES-1001" and data["reservations"][0]["name"]
    assert data["suppressions"] and data["consents"]  # consent proof from the opt-in evidence
    pdf = services.gdpr.export_pdf(data, hotel="Hotel Beispiel")
    assert pdf.startswith(b"%PDF")

    request = await services.gdpr.create_request(tenant.id, "erasure", subject, actor="usr_test")
    assert (
        request.status == "open"
        and len(request.tasks) == 5
        and request.due_at == demo_stack.clock.now() + timedelta(days=30)
    )
    assert await services.gdpr.restrict(tenant.id, subject, True, actor="usr_test") == 1
    async with services.ctx.db.scope() as session:
        guest = await session.get(Guest, subject.guest_ids[0])
        assert guest is not None and guest.restricted is True

    counts = await services.gdpr.erase(tenant.id, subject, actor="usr_test", request_id=request.id)
    assert counts["guest"] == 1 and counts["conversation"] == 1 and counts["reservation"] == 1
    assert counts["message"] >= 2 and counts["message_text"] >= 2
    gone = await services.gdpr.search(tenant.id, phone=PHONE)
    assert not gone.found
    async with services.ctx.db.scope() as session:
        suppressions = list((await session.execute(select(Suppression))).scalars())
        consents = list((await session.execute(select(Consent))).scalars())
        deletions = list(
            (
                await session.execute(select(DeletionLog).where(DeletionLog.reason == "erasure"))
            ).scalars()
        )
    assert suppressions and consents  # proof survives content deletion
    assert {d.table_name for d in deletions} >= {"guest", "conversation", "message", "reservation"}
    assert all(d.row_count > 0 for d in deletions)
    requests = await services.gdpr.requests(tenant.id)
    assert requests[0].status == "erased"
    for task in requests[0].tasks:
        await services.gdpr.complete_task(tenant.id, requests[0].id, task["id"], actor="usr_test")
    done = (await services.gdpr.requests(tenant.id))[0]
    assert done.status == "done" and done.completed_at is not None


async def test_retention_with_frozen_time(demo_stack: DemoStack) -> None:
    services, tenant, profile = demo_stack.services, demo_stack.tenant, demo_stack.profile
    await chat(demo_stack, "Ich bin schwanger und brauche ein ruhiges Zimmer.")
    await chat(demo_stack, "Gibt es einen Parkplatz?")
    async with services.ctx.db.scope() as session:
        flagged = [
            m
            for m in (
                await session.execute(select(Message).where(Message.direction == "in"))
            ).scalars()
            if m.meta.get("special_category")
        ]
    assert len(flagged) == 1
    state, _reply = await services.voice.begin(
        tenant, "retell", "call_r", from_number="+4915120000009"
    )
    await services.postcall.finish(
        tenant,
        profile,
        call_row_id=state.call_row_id,
        duration_s=30,
        transcript=[("user", "Hallo")],
        provider_cost_micro_eur=None,
        outcome="completed",
        retain_transcript=True,
        callback_needed=False,
    )
    services.voice.forget(state)

    assert await services.retention.run(tenant, profile) == {}
    demo_stack.clock.advance(timedelta(days=8))
    first = await services.retention.run(tenant, profile)
    assert first == {"message_text": 1}  # only the special-category text after the grace period
    async with services.ctx.db.scope() as session:
        texts = list((await session.execute(select(MessageText))).scalars())
        log = list((await session.execute(select(DeletionLog))).scalars())
    assert all("schwanger" not in t.text for t in texts)
    assert log[-1].reason == "special_category" and log[-1].row_count == 1

    demo_stack.clock.advance(timedelta(days=25))  # day 33: chat and transcript retention over
    second = await services.retention.run(tenant, profile)
    assert second["message_text"] >= 3 and second["voice_call.transcript"] == 1
    assert second.get("guest.personal", 0) >= 1
    async with services.ctx.db.scope() as session:
        assert list((await session.execute(select(MessageText))).scalars()) == []
        call = await session.get(VoiceCall, state.call_row_id)
        assert call is not None and call.transcript is None
        guest = await session.scalar(select(Guest).where(Guest.channel_key == GUEST))
        assert guest is not None and guest.phone_enc is None and guest.phone_bidx is None

    # booking-linked data: 30 days after departure
    reservation = await services.reservations.by_res_id(tenant.id, "RES-1001")
    assert reservation is not None and reservation.phone_bidx is not None
    demo_stack.clock.set(
        datetime.combine(
            reservation.departure + timedelta(days=31), datetime.min.time(), tzinfo=UTC
        )
    )
    third = await services.retention.run(tenant, profile)
    assert third.get("reservation.personal", 0) >= 1
    refreshed = await services.reservations.by_res_id(tenant.id, "RES-1001")
    assert refreshed is not None and refreshed.phone_bidx is None and refreshed.name_enc is None
    assert refreshed.res_id == "RES-1001"  # the booking skeleton stays for statistics


async def test_consent_log_from_opt_in_and_withdrawal(demo_stack: DemoStack) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    result = await services.reservations.upsert(
        tenant.id,
        ReservationInput(
            res_id="C-1",
            guest_name="Kim Beispiel",
            phone="+4915120000077",
            lang="de",
            arrival=date(2026, 10, 20),
            departure=date(2026, 10, 22),
            room_type="dz",
            source="website",
            adults=2,
            whatsapp_optin=True,
            optin_source="website_form",
            optin_at=demo_stack.clock.now(),
            optin_wording_version="v2",
            marketing_optin=True,
        ),
        actor="test",
    )
    async with services.ctx.db.scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Consent).where(Consent.reservation_id == result.reservation.id)
                )
            ).scalars()
        )
    assert sorted(c.kind for c in rows) == ["marketing", "whatsapp_journey"]
    assert rows[0].text_version == "v2" and rows[0].subject_bidx == result.reservation.phone_bidx
    again = await services.gdpr.record_consent(
        tenant.id,
        subject_bidx=rows[0].subject_bidx,
        kind="marketing",
        text_version="v2",
        given_at=demo_stack.clock.now(),
        channel="website_form",
    )
    assert again is None  # one row per subject, kind and wording version
    assert await services.gdpr.withdraw_consent(tenant.id, rows[0].subject_bidx, "marketing") == 1
    async with services.ctx.db.scope() as session:
        withdrawn = list(
            (
                await session.execute(select(Consent).where(Consent.withdrawn_at.is_not(None)))
            ).scalars()
        )
    assert len(withdrawn) == 1 and withdrawn[0].kind == "marketing"
