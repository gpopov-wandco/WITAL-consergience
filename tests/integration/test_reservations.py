"""Reservation intake (F12): upsert, E.164, blind indexes, opt-in evidence, verification,
reschedule, cancellation, CSV, PMS sync and the signed push API."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select
from tests.unit.test_profile import SAMPLE

from concierge.app import create_app
from concierge.availability.models import ReservationRecord
from concierge.availability.port import BaseAdapter
from concierge.channels.whatsapp.fake import FakeGraph
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.db.models import AuditLog, Job, Reservation, Tenant
from concierge.db.session import Database
from concierge.reservations.service import ReservationInput, to_e164
from concierge.security.crypto import Keyring
from concierge.security.signatures import sign_timestamped
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F12")

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
SECRET = "reservation-secret-test"


@pytest.fixture
async def stack(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> tuple[Services, Tenant]:
    clock.set(NOW)
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    services = build_services(ctx, llm=None, wa_port=FakeGraph())
    tenant = await services.tenants.create(TenantProfile.model_validate(SAMPLE), actor="test")
    await services.secrets.set(tenant.id, "reservation_api_secret", SECRET, actor="test")
    return services, tenant


def _input(**kw: Any) -> ReservationInput:
    base: dict[str, Any] = {
        "res_id": "R-100",
        "guest_name": "Anna Maria Muster",
        "phone": "0151 12345678",
        "email": "Anna@Example.invalid",
        "lang": "de",
        "arrival": date(2026, 10, 3),
        "departure": date(2026, 10, 5),
        "room_type": "dz",
        "adults": 2,
    }
    base.update(kw)
    return ReservationInput(**base)


def test_e164_normalisation() -> None:
    assert to_e164("0151 12345678") == "+4915112345678"
    assert to_e164("+49 (151) 123-45678") == "+4915112345678"
    assert to_e164("0049 151 12345678") == "+4915112345678"
    assert to_e164("4915112345678") == "+4915112345678"
    assert to_e164("069 000000") == "+4969000000"
    assert to_e164("+1 415 555 2671") == "+14155552671"
    assert to_e164("12") is None and to_e164("") is None and to_e164(None) is None


async def test_upsert_encrypts_indexes_and_records_optin_evidence(
    stack: tuple[Services, Tenant],
) -> None:
    services, tenant = stack
    keyring = services.ctx.keyring
    result = await services.reservations.upsert(
        tenant.id, _input(whatsapp_optin=True), actor="test"
    )
    row = result.reservation
    assert result.created and result.outcome == "created"
    assert services.reservations.decrypt(row) == {
        "name": "Anna Maria Muster",
        "phone": "+4915112345678",
        "email": "anna@example.invalid",
    }
    assert row.phone_bidx == keyring.blind_index("+4915112345678", tenant.id, "phone")
    assert row.name_bidx == keyring.blind_index("Muster", tenant.id, "name")
    assert row.whatsapp_optin is False and row.meta["optin_unrecorded"] is True
    evidence = await services.reservations.upsert(
        tenant.id,
        _input(
            whatsapp_optin=True,
            optin_source="website_form",
            optin_at=NOW,
            optin_wording_version="v1",
        ),
        actor="test",
    )
    assert (
        evidence.reservation.whatsapp_optin is True
        and evidence.reservation.optin_source == "website_form"
    )
    assert "whatsapp_optin" in evidence.changed and not evidence.dates_changed
    unchanged = await services.reservations.upsert(
        tenant.id,
        _input(
            whatsapp_optin=True,
            optin_source="website_form",
            optin_at=NOW,
            optin_wording_version="v1",
        ),
        actor="test",
    )
    assert unchanged.outcome == "unchanged" and unchanged.changed == []
    invalid = await services.reservations.upsert(
        tenant.id, _input(res_id="R-101", phone="12"), actor="test"
    )
    assert (
        invalid.reservation.phone_enc is None and invalid.reservation.meta["phone_invalid"] is True
    )
    found = await services.reservations.find_by_phone(tenant.id, "+49 151 12345678")
    assert [r.res_id for r in found] == ["R-100"]
    async with services.ctx.db.scope() as session:
        audits = list(
            (
                await session.execute(select(AuditLog).where(AuditLog.action.like("reservation.%")))
            ).scalars()
        )
    assert [a.action for a in audits] == [
        "reservation.create",
        "reservation.update",
        "reservation.create",
    ]
    assert all("Muster" not in json.dumps(a.details) for a in audits)


async def test_verify_matches_reference_surname_and_arrival(stack: tuple[Services, Tenant]) -> None:
    services, tenant = stack
    await services.reservations.upsert(tenant.id, _input(), actor="test")
    ok = await services.reservations.verify(tenant.id, " r-100 ", "muster", date(2026, 10, 3))
    assert ok is not None and ok["confirmation_number"] == "R-100" and ok["room_type"] == "dz"
    assert (
        await services.reservations.verify(tenant.id, "R-100", "Beispiel", date(2026, 10, 3))
        is None
    )
    assert (
        await services.reservations.verify(tenant.id, "R-100", "Muster", date(2026, 10, 4)) is None
    )
    assert (
        await services.reservations.verify(tenant.id, "R-999", "Muster", date(2026, 10, 3)) is None
    )
    await services.reservations.upsert(tenant.id, _input(status="cancelled"), actor="test")
    assert (
        await services.reservations.verify(tenant.id, "R-100", "Muster", date(2026, 10, 3)) is None
    )


async def test_reschedule_and_cancel_cancel_pending_journey_jobs(
    stack: tuple[Services, Tenant],
) -> None:
    services, tenant = stack
    await services.reservations.upsert(tenant.id, _input(), actor="test")
    prefix = services.reservations.job_prefix(tenant.id, "R-100")
    await services.jobs.enqueue(
        "journey_step",
        {"step": "t_minus_7"},
        run_at=NOW + timedelta(days=1),
        tenant_id=tenant.id,
        dedupe_key=f"{prefix}t_minus_7",
    )
    await services.jobs.enqueue(
        "journey_step",
        {"step": "t_minus_2"},
        run_at=NOW + timedelta(days=2),
        tenant_id=tenant.id,
        dedupe_key=f"{prefix}t_minus_2",
    )
    await services.jobs.enqueue(
        "journey_step",
        {"step": "other"},
        run_at=NOW + timedelta(days=2),
        tenant_id=tenant.id,
        dedupe_key="journey:other:x",
    )
    outcomes: list[str] = []

    async def hook(tenant_id: str, reservation: Reservation, outcome: str) -> None:
        outcomes.append(outcome)

    services.reservations.on_change(hook)
    moved = await services.reservations.upsert(
        tenant.id, _input(arrival=date(2026, 10, 10), departure=date(2026, 10, 12)), actor="test"
    )
    assert moved.dates_changed and moved.cancelled_jobs == 2 and moved.outcome == "updated"
    async with services.ctx.db.scope() as session:
        jobs = {j.dedupe_key: j.status for j in (await session.execute(select(Job))).scalars()}
    assert jobs[f"{prefix}t_minus_7"] == "cancelled" and jobs["journey:other:x"] == "pending"
    await services.jobs.enqueue(
        "journey_step",
        {"step": "t_minus_2"},
        run_at=NOW + timedelta(days=8),
        tenant_id=tenant.id,
        dedupe_key=f"{prefix}t_minus_2:2",
    )
    cancelled = await services.reservations.upsert(
        tenant.id,
        _input(arrival=date(2026, 10, 10), departure=date(2026, 10, 12), status="cancelled"),
        actor="test",
    )
    assert (
        cancelled.cancelled
        and cancelled.cancelled_jobs == 1
        and cancelled.reservation.cancelled_at == NOW
    )
    assert outcomes == ["updated", "cancelled"]
    again = await services.reservations.upsert(
        tenant.id,
        _input(arrival=date(2026, 10, 10), departure=date(2026, 10, 12), status="cancelled"),
        actor="test",
    )
    assert again.outcome == "unchanged" and not again.cancelled


async def test_csv_import_with_german_formats(stack: tuple[Services, Tenant]) -> None:
    services, tenant = stack
    text = (
        "res_id;guest_name;phone;email;lang;arrival;departure;room_type;rate_plan;source;whatsapp_optin;optin_source;optin_at\r\n"
        "B-1;Erika Beispiel;0151 5555555;erika@example.invalid;DE;03.10.2026;05.10.2026;dz;flex;pms_export;ja;buchungsformular;2026-09-20T10:00:00+02:00\r\n"
        "B-2;Max Muster;;;en;2026-10-06;2026-10-07;ez;;;nein;;\r\n"
        "B-3;Bad Row;;;de;2026-10-06;2026-10-05;ez;;;;;\r\n"
    )
    dry = await services.reservations.import_csv(tenant.id, text, dry_run=True, actor="staff:1")
    assert (
        len(dry.rows) == 2
        and dry.errors
        and dry.errors[0][0] == 4
        and "departure" in dry.errors[0][1]
    )
    good = text.rsplit("B-3", 1)[0]
    applied = await services.reservations.import_csv(
        tenant.id, good, dry_run=False, actor="staff:1"
    )
    assert applied.ok and applied.applied == 2 and applied.created == 2
    first = await services.reservations.by_res_id(tenant.id, "B-1")
    assert (
        first is not None
        and first.whatsapp_optin is True
        and first.optin_at == datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
    )
    assert (
        first.lang == "de" and first.source == "pms_export" and first.arrival == date(2026, 10, 3)
    )
    second = await services.reservations.by_res_id(tenant.id, "B-2")
    assert second is not None and second.whatsapp_optin is False and second.source == "csv"
    bad_header = await services.reservations.import_csv(
        tenant.id, "res_id,name\nx,y\n", dry_run=True, actor="x"
    )
    assert bad_header.errors[0][1].startswith("header must contain")


class FakePms(BaseAdapter):
    name = "fakepms"

    async def arrivals(self, start: date, end: date) -> list[ReservationRecord]:
        return [
            ReservationRecord(
                "P-1",
                "P-1",
                date(2026, 10, 2),
                date(2026, 10, 4),
                "confirmed",
                2,
                0,
                "dz",
                "Muster",
                "Anna",
                "a@example.invalid",
                "+49 151 12345678",
                "de",
                "fakepms",
            ),
            ReservationRecord(
                "P-2",
                "",
                date(2026, 10, 3),
                date(2026, 10, 5),
                "cancelled",
                1,
                0,
                "ez",
                "Beispiel",
                "Erika",
                "",
                "",
                "",
                "fakepms",
            ),
        ]

    async def departures(self, start: date, end: date) -> list[ReservationRecord]:
        return [
            ReservationRecord(
                "P-1",
                "P-1",
                date(2026, 10, 2),
                date(2026, 10, 4),
                "confirmed",
                2,
                0,
                "dz",
                "Muster",
                "Anna",
                "",
                "",
                "de",
                "fakepms",
            )
        ]


async def test_sync_from_pms_upserts_without_optin(stack: tuple[Services, Tenant]) -> None:
    services, tenant = stack
    report = await services.reservations.sync_from_pms(tenant.id, FakePms(), days_ahead=7)
    assert (
        report.arrivals == 2
        and report.departures == 1
        and report.created == 2
        and report.updated == 0
    )
    first = await services.reservations.by_res_id(tenant.id, "P-1")
    assert first is not None and first.source == "pms:fakepms" and first.whatsapp_optin is False
    assert services.reservations.decrypt(first)["phone"] == "+4915112345678"
    second = await services.reservations.by_res_id(tenant.id, "P-2")
    assert second is not None and second.status == "cancelled"
    again = await services.reservations.sync_from_pms(tenant.id, FakePms(), days_ahead=7)
    assert again.created == 0 and again.updated == 0


# ------------------------------------------------------------------- API
def _signed(
    body: dict[str, Any], *, secret: str = SECRET, at: datetime = NOW
) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode("utf-8")
    header = sign_timestamped(raw, secret.encode("utf-8"), int(at.timestamp()))
    return raw, {"content-type": "application/json", "x-signature": header}


async def test_push_api_signature_replay_idempotency_reschedule_and_cancel(
    stack: tuple[Services, Tenant], clock: FakeClock
) -> None:
    services, tenant = stack
    app = create_app(services, start_worker=False)
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    )
    url = f"/api/v1/tenants/{tenant.public_key}/reservations"
    body = {
        "res_id": "WEB-1",
        "guest_name": "Anna Muster",
        "phone": "+49 151 12345678",
        "email": "anna@example.invalid",
        "lang": "de",
        "arrival": "2026-10-03",
        "departure": "2026-10-05",
        "room_type": "dz",
        "source": "website",
        "whatsapp_optin": True,
        "optin_source": "website_form",
        "optin_at": "2026-09-29T09:00:00Z",
    }
    raw, headers = _signed(body)
    created = await client.post(url, content=raw, headers=headers)
    assert (
        created.status_code == 201
        and created.json()["status"] == "created"
        and created.json()["whatsapp_optin"] is True
    )
    replay = await client.post(url, content=raw, headers=headers)
    assert replay.status_code == 200 and replay.json()["status"] == "unchanged"
    clock.advance(timedelta(minutes=6))
    stale = await client.post(url, content=raw, headers=headers)
    assert stale.status_code == 401
    raw, headers = _signed(body, at=clock.now())
    assert (await client.post(url, content=raw, headers=headers)).status_code == 200
    raw, bad = _signed(body, secret="wrong", at=clock.now())
    assert (await client.post(url, content=raw, headers=bad)).status_code == 401
    assert (
        await client.post(url, content=raw, headers={"content-type": "application/json"})
    ).status_code == 401
    prefix = services.reservations.job_prefix(tenant.id, "WEB-1")
    await services.jobs.enqueue(
        "journey_step",
        {"step": "t_minus_7"},
        run_at=clock.now() + timedelta(days=1),
        tenant_id=tenant.id,
        dedupe_key=f"{prefix}t_minus_7",
    )
    raw, headers = _signed(
        {**body, "arrival": "2026-10-10", "departure": "2026-10-12"}, at=clock.now()
    )
    moved = await client.post(url, content=raw, headers=headers)
    assert (
        moved.status_code == 200
        and moved.json()["status"] == "updated"
        and moved.json()["cancelled_jobs"] >= 1
    )
    raw, headers = _signed(
        {**body, "arrival": "2026-10-10", "departure": "2026-10-12", "status": "cancelled"},
        at=clock.now(),
    )
    cancelled = await client.post(url, content=raw, headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    raw, headers = _signed(
        {"res_id": "WEB-2", "arrival": "2026-10-10", "departure": "2026-10-01"}, at=clock.now()
    )
    invalid = await client.post(url, content=raw, headers=headers)
    assert invalid.status_code == 422 and any("departure" in f for f in invalid.json()["fields"])
    raw, headers = _signed(
        {"res_id": "x y", "arrival": "2026-10-10", "departure": "2026-10-11"}, at=clock.now()
    )
    spaced = await client.post(url, content=raw, headers=headers)
    assert spaced.status_code == 422 and spaced.json()["fields"][0].startswith("res_id")
    assert (
        await client.post("/api/v1/tenants/pk_nope/reservations", content=raw, headers=headers)
    ).status_code == 404
    other = await services.tenants.create(
        TenantProfile.model_validate(
            dict(SAMPLE, slug="other", name="Other", widget={"public_key": "pk_other"})
        ),
        actor="test",
    )
    assert (
        await client.post(
            f"/api/v1/tenants/{other.public_key}/reservations", content=raw, headers=headers
        )
    ).status_code == 503
    big = b"{" + b" " * (64 * 1024) + b"}"
    assert (await client.post(url, content=big, headers=headers)).status_code == 413
    await client.aclose()
