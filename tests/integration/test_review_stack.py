"""Regression tests for the findings of the fresh-context review (docs/REVIEW_FINDINGS.md)
that need the whole demo stack: webhook ingress (R2, R3, R8), the Brain (R4, R15), erasure
and retention (R12, R21), maintenance (R13), the admin (R14, R18) and the widget origins."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from tests.integration.conftest import DemoStack
from tests.integration.test_admin import OWNER, PASSWORDS, csrf, login

from concierge.db.models import (
    Conversation,
    Handoff,
    Message,
    Reservation,
    Suppression,
    TenantSecret,
    WebhookEvent,
)
from concierge.demo.seed import DEMO_SECRETS
from concierge.demo.wa_sim import PayloadFactory, WaSimClient
from concierge.maintenance import DIGEST_KIND
from concierge.reservations.service import ReservationInput
from concierge.tenants.profile import TenantProfile

GUEST = "4915112345678"
OTHER = "4915120000077"


async def send(
    stack: DemoStack,
    text: str,
    *,
    number: str = GUEST,
    phone_number_id: str | None = None,
    secret: str | None = None,
) -> int:
    factory = PayloadFactory(
        phone_number_id or stack.tenant.phone_number_id or "", clock=stack.clock
    )
    sim = WaSimClient(
        "http://testserver",
        secret or DEMO_SECRETS["wa_app_secret"],
        transport=stack.client._transport,
    )
    response = await sim.post(factory.text(number, text))
    await stack.services.worker.drain()
    return response.status_code


async def conversation_of(stack: DemoStack, number: str) -> Conversation:
    guest = await stack.services.conversations.get_guest(stack.tenant.id, "whatsapp", number)
    assert guest is not None
    return await stack.services.conversations.open_conversation(
        stack.tenant.id, guest.id, "whatsapp", lang="de"
    )


async def outbound_count(stack: DemoStack) -> int:
    async with stack.services.ctx.db.scope() as session:
        return len(
            list(
                (await session.execute(select(Message).where(Message.direction == "out"))).scalars()
            )
        )


def second_profile(stack: DemoStack, slug: str, phone_number_id: str) -> TenantProfile:
    data: dict[str, Any] = stack.profile.model_dump(mode="json")
    data["slug"] = slug
    data["name"] = f"Hotel {slug}"
    data["widget"]["public_key"] = f"pk_{slug.replace('-', '')}"
    data["whatsapp"]["phone_number_id"] = phone_number_id
    data["whatsapp"]["display_phone_number"] = "+49 69 " + phone_number_id[-6:].zfill(6)
    return TenantProfile.model_validate(data)


@pytest.mark.feature("F31")
async def test_shared_app_secret_serves_every_tenant_and_a_bad_row_none_of_them(
    demo_stack: DemoStack,
) -> None:
    services = demo_stack.services
    second = await services.tenants.create(
        second_profile(demo_stack, "hotel-zwei", "PN-ZWEI"), actor="t"
    )
    for name in ("wa_app_secret", "wa_verify_token", "wa_access_token"):
        await services.secrets.set(
            second.id, name, DEMO_SECRETS[name], actor="t"
        )  # one Meta app, two WABAs
    third = await services.tenants.create(
        second_profile(demo_stack, "hotel-drei", "PN-DREI"), actor="t"
    )
    async with services.ctx.db.scope() as session:  # a row encrypted under a key that is gone
        session.add(
            TenantSecret(
                tenant_id=third.id, name="wa_app_secret", ciphertext=b"garbage", key_id="k0"
            )
        )
        await session.commit()
    services.secret_cache.invalidate()
    assert (
        await send(demo_stack, "Hallo aus Hotel zwei", number=OTHER, phone_number_id="PN-ZWEI")
        == 200
    )
    async with services.ctx.db.scope() as session:
        stored = list(
            (
                await session.execute(
                    select(Message).where(Message.tenant_id == second.id, Message.direction == "in")
                )
            ).scalars()
        )
    assert len(stored) == 1  # not dropped as a secret mismatch
    assert await send(demo_stack, "Gibt es einen Parkplatz?") == 200  # the first tenant is served
    assert await outbound_count(demo_stack) >= 1


@pytest.mark.feature("F01")
async def test_a_retried_inbound_job_never_answers_twice(demo_stack: DemoStack) -> None:
    services = demo_stack.services
    await send(demo_stack, "Gibt es einen Parkplatz?")
    before = await outbound_count(demo_stack)
    async with services.ctx.db.scope() as session:
        event = (await session.execute(select(WebhookEvent))).scalars().first()
    assert event is not None
    await services.wa_inbound.process_event(event.id)  # the retry of a job that failed late
    await services.worker.drain()
    assert await outbound_count(demo_stack) == before
    conversation = await conversation_of(demo_stack, GUEST)
    assert conversation.status != "needs_staff"
    assert await services.handoffs.inbox(demo_stack.tenant.id) == []  # no spurious P2


@pytest.mark.feature("F03")
async def test_stop_is_honoured_while_staff_handle_the_case(demo_stack: DemoStack) -> None:
    services = demo_stack.services
    await send(demo_stack, "Die Dusche ist kaputt und das Zimmer ist dreckig")
    conversation = await conversation_of(demo_stack, GUEST)
    assert conversation.status == "needs_staff"
    await send(demo_stack, "STOP")
    async with services.ctx.db.scope() as session:
        suppression = (await session.execute(select(Suppression))).scalar_one()
    assert suppression.reason == "stop"
    conversation = await conversation_of(demo_stack, GUEST)
    assert conversation.stopped_at is not None
    replies = [m["payload"] for m in demo_stack.state.messages_to(GUEST)]
    assert any("automatischen Nachrichten" in json.dumps(p, ensure_ascii=False) for p in replies)


@pytest.mark.feature("F01")
async def test_guest_message_rate_limit_keeps_emergencies_open(demo_stack: DemoStack) -> None:
    services = demo_stack.services
    profile = demo_stack.profile.model_copy(deep=True)
    profile.limits.messages_per_guest_per_hour = 2
    await services.tenants.update_profile("hotel-beispiel", profile, actor="t")
    for text in ("Gibt es einen Parkplatz?", "Wann gibt es Frühstück?", "Und WLAN?"):
        await send(demo_stack, text, number=OTHER)
    replies = [
        json.dumps(m["payload"], ensure_ascii=False) for m in demo_stack.state.messages_to(OTHER)
    ]
    assert any("viele Nachrichten" in p for p in replies)
    await send(demo_stack, "Es brennt im Zimmer!", number=OTHER)
    replies = [
        json.dumps(m["payload"], ensure_ascii=False) for m in demo_stack.state.messages_to(OTHER)
    ]
    assert any("112" in p for p in replies)
    async with services.ctx.db.scope() as session:
        p0 = [h for h in (await session.execute(select(Handoff))).scalars() if h.priority == "P0"]
    assert p0


@pytest.mark.feature("F25")
async def test_erasure_removes_webhook_payloads_that_carry_the_number(
    demo_stack: DemoStack,
) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    await send(demo_stack, "Gibt es einen Parkplatz?")
    await send(demo_stack, "Danke")
    subject = await services.gdpr.search(tenant.id, phone="+" + GUEST)
    assert subject.found
    data = await services.gdpr.export(tenant.id, subject)
    assert len(data["webhook_events"]) >= 2
    request = await services.gdpr.create_request(tenant.id, "erasure", subject, actor="usr_test")
    counts = await services.gdpr.erase(tenant.id, subject, actor="usr_test", request_id=request.id)
    assert counts.get("webhook_event", 0) >= 2
    async with services.ctx.db.scope() as session:
        payloads = [
            json.dumps(e.payload) for e in (await session.execute(select(WebhookEvent))).scalars()
        ]
    assert all(GUEST not in p for p in payloads)


@pytest.mark.feature("F25")
async def test_retention_scrubs_reservations_without_a_phone_number(demo_stack: DemoStack) -> None:
    services, tenant, profile = demo_stack.services, demo_stack.tenant, demo_stack.profile
    await services.reservations.upsert(
        tenant.id,
        ReservationInput(
            res_id="NOPHONE-1",
            guest_name="Max Muster",
            email="max@example.invalid",
            arrival=date(2026, 9, 1),
            departure=date(2026, 9, 3),
        ),
        actor="t",
    )
    demo_stack.clock.set(
        datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
        + timedelta(days=profile.retention.booking_days_after_departure + 2)
    )
    counts = await services.retention.run(tenant, profile)
    assert counts.get("reservation.personal", 0) >= 1
    async with services.ctx.db.scope() as session:
        row = await session.scalar(select(Reservation).where(Reservation.res_id == "NOPHONE-1"))
    assert row is not None and row.name_enc is None and row.email_enc is None


@pytest.mark.feature("F32")
async def test_a_failing_maintenance_run_still_schedules_the_next_one(
    demo_stack: DemoStack,
) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant

    async def failing(_tenant: Any) -> None:
        raise RuntimeError("digest exploded")

    services.maintenance.register(DIGEST_KIND, failing)
    run_at = await services.maintenance.schedule(tenant, DIGEST_KIND)
    demo_stack.clock.set(run_at)
    await services.worker.drain()
    pending = await services.jobs.pending(tenant_id=tenant.id, kind=DIGEST_KIND)
    assert any(job.run_at == run_at + timedelta(days=7) for job in pending)


@pytest.mark.feature("F17")
async def test_staff_templates_honour_stop_and_quiet_hours(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await send(demo_stack, "Gibt es einen Parkplatz?")
    stopped = await conversation_of(demo_stack, GUEST)
    await send(demo_stack, "STOP")
    await send(demo_stack, "Wann gibt es Frühstück?", number=OTHER)
    other = await conversation_of(demo_stack, OTHER)
    await login(client, OWNER)
    token = await csrf(client)
    form = {"csrf": token, "mode": "template", "template": "gc_booking_followup", "text": ""}
    suppressed = await client.post(f"/admin/conversations/{stopped.id}/reply", data=form)
    assert suppressed.headers["location"].endswith("notice=suppressed")
    page = await client.get(f"/admin/conversations/{stopped.id}?notice=suppressed")
    assert "STOP" in page.text and "keine Vorlage" in page.text
    demo_stack.clock.set(datetime(2026, 9, 29, 21, 30, tzinfo=UTC))  # 23:30 in Berlin
    await login(client, OWNER)  # the earlier session idled out with the clock jump
    form["csrf"] = await csrf(client)
    quiet = await client.post(f"/admin/conversations/{other.id}/reply", data=form)
    assert quiet.headers["location"].endswith("notice=quiet_hours_block")
    await send(
        demo_stack, "Es brennt im Zimmer!", number=OTHER
    )  # a P0 case may be reached at night
    allowed = await client.post(f"/admin/conversations/{other.id}/reply", data=form)
    assert allowed.headers["location"].endswith("notice=sent_template")


@pytest.mark.feature("F19")
async def test_login_next_parameter_never_leaves_the_site(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    for target in (
        "/\\evil.example",
        "//evil.example",
        "https://evil.example/",
        "/admin/inbox?x=<",
    ):
        response = await client.post(
            "/admin/login", data={"email": OWNER, "password": PASSWORDS[OWNER], "next": target}
        )
        assert response.status_code == 303 and response.headers["location"] == "/admin/"
    response = await client.post(
        "/admin/login", data={"email": OWNER, "password": PASSWORDS[OWNER], "next": "/admin/inbox"}
    )
    assert response.headers["location"] == "/admin/inbox"


@pytest.mark.feature("F22")
async def test_widget_accepts_the_bound_origin_of_a_custom_port(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    body = {"key": demo_stack.profile.widget.public_key, "lang": "de"}
    refused = await client.post(
        "/w/api/session", json=body, headers={"origin": "http://127.0.0.1:9999"}
    )
    assert refused.status_code == 403
    services.widget.own_origins.add(
        "http://127.0.0.1:9999"
    )  # what `concierge demo --port 9999` adds
    accepted = await client.post(
        "/w/api/session", json=body, headers={"origin": "http://127.0.0.1:9999"}
    )
    assert accepted.status_code == 200 and accepted.json()["messages"][0]["kind"] == "disclosure"
    evil = await client.post(
        "/w/api/session", json=body, headers={"origin": "https://evil.invalid"}
    )
    assert evil.status_code == 403


@pytest.mark.feature("F17")
async def test_a_message_stamped_ahead_of_the_clock_does_not_extend_the_window(
    demo_stack: DemoStack,
) -> None:
    """The 24 h window opens at Meta's timestamp, clamped to the service clock (skew)."""
    stack = demo_stack
    factory = PayloadFactory(stack.tenant.phone_number_id or "", clock=stack.clock)
    sim = WaSimClient(
        "http://testserver", DEMO_SECRETS["wa_app_secret"], transport=stack.client._transport
    )
    ahead = int((stack.clock.now() + timedelta(hours=3)).timestamp())
    assert (await sim.post(factory.text(GUEST, "Hallo", ts=ahead))).status_code == 200
    await stack.services.worker.drain()
    conversation = await conversation_of(stack, GUEST)
    assert conversation.window_expires_at is not None
    expected = stack.clock.now() + timedelta(hours=24)
    assert conversation.window_expires_at.replace(tzinfo=None) == expected.replace(tzinfo=None)
