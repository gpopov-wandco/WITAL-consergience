"""Property tests (10.3): duplicate and out-of-order webhooks, tenant isolation under
interleaved operations, the scheduler's quiet hours and dedupe keys.

Signed-body mutations and secret round trips are property-tested in
``tests/unit/test_security.py``. Every example here builds a fresh stack on its own
SQLite file, so examples never share state.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select

from concierge.app import create_app
from concierge.db.models import Conversation, Handoff, KbEntry, Message, WebhookEvent
from concierge.demo.seed import DEMO_SECRETS, example_profile
from concierge.eval.golden import GoldenStack
from concierge.journeys.service import JOB_KIND
from concierge.reservations.service import ReservationInput
from concierge.security.signatures import sign_meta
from concierge.tenants.profile import TenantProfile

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
GUEST = "4915120001111"
TEXTS = [
    "Gibt es einen Parkplatz?",
    "Wann gibt es Frühstück?",
    "Ab wann ist Check-in?",
    "Darf ich meinen Hund mitbringen?",
]
PROPERTY = settings(
    max_examples=6,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def run_example(coro_factory: Callable[[Path], Awaitable[None]]) -> None:
    with tempfile.TemporaryDirectory(prefix="gc-prop-") as tmp:
        asyncio.run(coro_factory(Path(tmp)))


def webhook(phone_number_id: str, wamid: str, text: str, ts: int) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "demo-waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "4969000000",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Prop Guest"}, "wa_id": GUEST}],
                            "messages": [
                                {
                                    "from": GUEST,
                                    "id": wamid,
                                    "timestamp": str(ts),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload).encode()


# ------------------------------------------------- duplicates and ordering
@pytest.mark.feature("F01")
@PROPERTY
@given(
    order=st.permutations([*range(len(TEXTS)), 0, 1, 2, 3]),
    skew=st.lists(st.integers(-120, 120), min_size=len(TEXTS), max_size=len(TEXTS)),
)
def test_duplicate_and_out_of_order_webhooks_yield_one_reply_each(
    order: list[int], skew: list[int]
) -> None:
    """Every distinct wamid is processed once, however often and in which order it arrives."""

    async def scenario(work_dir: Path) -> None:
        stack = await GoldenStack.create(work_dir / "dup.db", responder_only=True)
        try:
            services = stack.services
            app = create_app(services, start_worker=False)
            phone_number_id = stack.tenant.phone_number_id or ""
            base = int(NOW.timestamp())
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                for index in order:
                    raw = webhook(
                        phone_number_id, f"wamid.prop.{index}", TEXTS[index], base + skew[index]
                    )
                    response = await client.post(
                        "/wa/webhook",
                        content=raw,
                        headers={
                            "content-type": "application/json",
                            "x-hub-signature-256": sign_meta(raw, DEMO_SECRETS["wa_app_secret"]),
                        },
                    )
                    assert response.status_code == 200
                    await services.worker.drain()
            async with services.ctx.db.scope() as session:
                events = list(
                    (
                        await session.execute(
                            select(WebhookEvent).where(WebhookEvent.tenant_id == stack.tenant.id)
                        )
                    ).scalars()
                )
                inbound = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(Message)
                        .where(Message.direction == "in", Message.tenant_id == stack.tenant.id)
                    )
                    or 0
                )
                replies = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(Message)
                        .where(
                            Message.direction == "out",
                            Message.tenant_id == stack.tenant.id,
                            Message.kind == "text",
                        )
                    )
                    or 0
                )
            keys = [
                e.event_key
                for e in events
                if e.event_key.endswith(tuple(f"wamid.prop.{i}" for i in range(len(TEXTS))))
            ]
            assert len(keys) == len(set(keys)) == len(TEXTS)
            assert inbound == len(TEXTS)
            # one reply per distinct message; a demo banner or an upsell offer may add texts,
            # but never a second answer to the same wamid
            assert len(TEXTS) <= replies <= 2 * len(TEXTS)
            sent = services.wa_port.messages_to(GUEST)  # type: ignore[attr-defined]
            assert len({m.wamid for m in sent}) == len(sent)
        finally:
            await stack.close()

    run_example(scenario)


# ------------------------------------------------- tenant isolation
OPS = st.lists(
    st.tuples(st.sampled_from(["a", "b"]), st.sampled_from(["kb", "message", "handoff"])),
    min_size=4,
    max_size=14,
)


@pytest.mark.feature("F31")
@PROPERTY
@given(ops=OPS)
def test_two_tenants_stay_isolated_under_interleaved_operations(ops: list[tuple[str, str]]) -> None:
    async def scenario(work_dir: Path) -> None:
        stack = await GoldenStack.create(work_dir / "iso.db", responder_only=True)
        try:
            services = stack.services
            second = example_profile("villa-westend")
            other = await services.tenants.create(second, actor="prop")
            tenants = {"a": stack.tenant, "b": other}
            counts: dict[tuple[str, str], int] = {}
            for index, (who, op) in enumerate(ops):
                tenant = tenants[who]
                counts[(who, op)] = counts.get((who, op), 0) + 1
                if op == "kb":
                    await services.kb.upsert(
                        tenant.id, "house_rules", "de", text=f"Regel {index}", actor="prop"
                    )
                elif op == "message":
                    guest = await services.conversations.get_or_create_guest(
                        tenant.id, "web", f"s{index}", lang="de"
                    )
                    conversation = await services.conversations.open_conversation(
                        tenant.id, guest.id, "web", lang="de"
                    )
                    await services.conversations.record_outbound(
                        tenant.id,
                        conversation.id,
                        f"Text {index}",
                        lang="de",
                        kind="text",
                        retention_days=30,
                    )
                else:
                    guest = await services.conversations.get_or_create_guest(
                        tenant.id, "web", f"h{index}", lang="de"
                    )
                    conversation = await services.conversations.open_conversation(
                        tenant.id, guest.id, "web", lang="de"
                    )
                    await services.handoffs.create(
                        tenant.id,
                        conversation.id,
                        priority="P3",
                        trigger="prop",
                        reason="property test",
                        profile=services.tenants.profile_of(tenant),
                        summary=f"Handoff {index}",
                    )
            for who, tenant in tenants.items():
                async with services.ctx.db.tenant_scope(tenant.id) as session:
                    kb_rows = list((await session.execute(select(KbEntry))).scalars())
                    conversations = list((await session.execute(select(Conversation))).scalars())
                    messages = list((await session.execute(select(Message))).scalars())
                    handoffs = list((await session.execute(select(Handoff))).scalars())
                assert all(
                    r.tenant_id == tenant.id for r in kb_rows + conversations + messages + handoffs
                )
                assert len(handoffs) == counts.get((who, "handoff"), 0)
                assert len(messages) == counts.get((who, "message"), 0)
                assert len(conversations) == counts.get((who, "message"), 0) + counts.get(
                    (who, "handoff"), 0
                )
                # tenant a starts with the fixture KB (one house_rules row per language)
                house_rules = [r for r in kb_rows if r.key == "house_rules" and r.lang == "de"]
                assert len(house_rules) == counts.get((who, "kb"), 0) + (1 if who == "a" else 0)
        finally:
            await stack.close()

    run_example(scenario)


# ------------------------------------------------- scheduler
@pytest.mark.feature("F13")
@PROPERTY
@given(
    offset=st.integers(1, 210),
    nights=st.integers(1, 4),
    quiet=st.sampled_from(["21:00-08:00", "22:00-07:00", "20:30-09:15"]),
)
def test_scheduler_never_sends_in_quiet_hours_or_twice_per_dedupe_key(
    offset: int, nights: int, quiet: str
) -> None:
    async def scenario(work_dir: Path) -> None:
        stack = await GoldenStack.create(work_dir / "sched.db", responder_only=True)
        try:
            services = stack.services
            data = services.tenants.profile_of(stack.tenant).model_dump(mode="json")
            data["journeys"]["quiet_hours"] = quiet
            profile = TenantProfile.model_validate(data)
            tenant = await services.tenants.update_profile(stack.tenant.slug, profile, actor="prop")
            arrival = NOW.date() + timedelta(days=offset)
            result = await services.reservations.upsert(
                tenant.id,
                ReservationInput(
                    res_id=f"PROP-{offset}-{nights}",
                    guest_name="Prop Guest",
                    phone=f"+{GUEST}",
                    email="prop@example.invalid",
                    lang="de",
                    arrival=arrival,
                    departure=arrival + timedelta(days=nights),
                    room_type="dz",
                    source="website",
                    adults=2,
                    whatsapp_optin=True,
                    optin_source="website_form",
                    optin_at=NOW - timedelta(hours=1),
                    optin_wording_version="v1",
                    marketing_optin=True,
                ),
                actor="prop",
            )
            reservation = result.reservation
            planned = services.journeys.schedule(profile, reservation, now=NOW)
            zone = ZoneInfo(profile.timezone)
            window = profile.journeys.quiet_window()
            keys = [p.dedupe_key for p in planned]
            assert len(keys) == len(set(keys)), keys
            for step in planned:
                local = step.run_at.astimezone(zone)
                assert not profile.journeys.is_quiet(local.time()), (step.step, local, window)
                assert step.run_at >= NOW
                assert not (step.step == "checkout" and local.time() < time(7, 0))
            before = await services.jobs.pending(tenant_id=tenant.id, kind=JOB_KIND)
            await services.journeys.plan(tenant, reservation)
            again = await services.jobs.pending(tenant_id=tenant.id, kind=JOB_KIND)
            assert len(again) == len(before)
            assert len({j.dedupe_key for j in again}) == len(again)
        finally:
            await stack.close()

    run_example(scenario)
