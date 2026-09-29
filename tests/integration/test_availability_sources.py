"""Fallback sources (F09) and the booking inquiry (F10) against the database.

Manual inventory through the chain (restrictions, staleness, missing rows), the staff
inventory service (bulk set with audit, month grid, CSV with German formats, dry run),
the iCal adapter behind the SSRF guard (edge cases, conditional GET, blocked feed) and
the inquiry service (consent, card data, dedupe, handoff, PMS write gate).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from concierge.availability.booking import InquiryError, InquiryForm, InquiryService
from concierge.availability.chain import AvailabilityService
from concierge.availability.factory import AdapterFactory
from concierge.availability.models import InquiryRequest, NotSupported, Occupancy, Stay
from concierge.availability.policy import wording
from concierge.availability.port import BaseAdapter
from concierge.channels.whatsapp.fake import FakeGraph
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.db.models import AuditLog, DayInventory, GuestRequest, Handoff, Tenant
from concierge.db.session import Database
from concierge.demo.seed import seed_demo
from concierge.security.crypto import Keyring
from concierge.security.ssrf import SafeFetcher
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F09")

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
OCC = Occupancy(2, 0)


@dataclass
class Hotel:
    services: Services
    tenant: Tenant
    profile: TenantProfile
    clock: FakeClock

    async def refresh(self) -> None:
        tenant = await self.services.tenants.get(self.tenant.slug)
        assert tenant is not None
        self.tenant = tenant
        self.profile = TenantProfile.model_validate(tenant.profile)

    async def check(self, stay: Stay, occupancy: Occupancy = OCC, room_type: str = "") -> Any:
        return await self.services.availability.check(
            self.tenant, self.profile, stay, occupancy, room_type
        )


@pytest.fixture
async def hotel(db: Database, settings: Settings, keyring: Keyring, clock: FakeClock) -> Hotel:
    clock.set(NOW)
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    services = build_services(ctx, llm=None, wa_port=FakeGraph(), demo=True)
    await seed_demo(services)
    tenant = await services.tenants.require("hotel-beispiel")
    return Hotel(services, tenant, TenantProfile.model_validate(tenant.profile), clock)


# --------------------------------------------------------------- manual
async def test_manual_inventory_through_the_chain(hotel: Hotel) -> None:
    answer = await hotel.check(Stay(date(2026, 10, 6), date(2026, 10, 8)))
    assert answer.source == "manual" and answer.status == "available" and not answer.stale
    dz = next(q for q in answer.quotes if q.room_type == "dz")
    assert dz.units_left == 8 and dz.per_night is not None and dz.per_night.cents == 12000
    assert dz.total is not None and dz.total.cents == 24000 and dz.tax_inclusive is True
    text = wording(answer, "de", hotel.services.texts, tz="Europe/Berlin")
    assert (
        text.startswith("Laut unserem Kalender (Stand 29.09.2026)") and "120 EUR pro Nacht" in text
    )
    only_ez = await hotel.check(Stay(date(2026, 10, 6), date(2026, 10, 8)), Occupancy(1), "ez")
    assert [q.room_type for q in only_ez.quotes] == ["ez"] and only_ez.quotes[
        0
    ].per_night is not None
    assert only_ez.quotes[0].per_night.cents == 9500


async def test_manual_restrictions_min_stay_closed_arrival_and_occupancy(hotel: Hotel) -> None:
    friday = await hotel.check(Stay(date(2026, 10, 2), date(2026, 10, 3)))
    assert friday.status == "restricted"
    rule = next(r for r in friday.quotes[0].restrictions if r.kind == "min_stay")
    assert rule.value == "2" and rule.alternative == "2026-10-04"
    closed = await hotel.check(Stay(date(2026, 10, 19), date(2026, 10, 21)))
    rule = next(r for r in closed.quotes[0].restrictions if r.kind == "closed_arrival")
    assert rule.value == "2026-10-19" and rule.alternative == "2026-10-20"
    crowd = await hotel.check(Stay(date(2026, 10, 6), date(2026, 10, 8)), Occupancy(3, 0), "dz")
    rule = crowd.quotes[0].restrictions[0]
    assert rule.kind == "max_occupancy" and rule.value == "2" and rule.alternative == "2"
    text = wording(crowd, "de", hotel.services.texts, tz="Europe/Berlin")
    assert "höchstens 2 Personen pro Zimmer" in text and "Alternative: 2 Zimmer" in text


async def test_manual_missing_rows_and_staleness(hotel: Hotel) -> None:
    far = await hotel.check(Stay(date(2027, 6, 1), date(2027, 6, 3)))
    assert far.status == "unknown" and far.source == "none"
    assert "manual:unknown" in far.reasons and "no_live_data" in far.reasons
    hotel.clock.advance(timedelta(hours=40))
    hotel.services.availability.reset()
    stale = await hotel.check(Stay(date(2026, 10, 6), date(2026, 10, 8)))
    assert (
        stale.source == "manual"
        and stale.stale
        and not stale.prices_shown
        and "stale" in stale.reasons
    )
    text = wording(stale, "de", hotel.services.texts, tz="Europe/Berlin")
    assert "Stand 29.09.2026" in text and "EUR" not in text and "verfügbar" in text
    report = await (await hotel.services.adapters("manual", hotel.tenant, hotel.profile)).doctor()  # type: ignore[union-attr]
    assert not report.ok and any("staleness" in w for w in report.warnings)


async def test_inventory_bulk_set_grid_and_csv(hotel: Hotel) -> None:
    inventory = hotel.services.inventory
    tenant_id = hotel.tenant.id
    count = await inventory.set_days(
        tenant_id,
        "dz",
        date(2026, 10, 6),
        date(2026, 10, 8),
        units_available=1,
        price_cents=15000,
        actor="staff:1",
    )
    assert count == 3
    hotel.services.availability.reset()
    answer = await hotel.check(Stay(date(2026, 10, 6), date(2026, 10, 8)), OCC, "dz")
    assert answer.quotes[0].units_left == 1 and answer.quotes[0].per_night is not None
    assert answer.quotes[0].per_night.cents == 15000
    async with hotel.services.ctx.db.scope() as session:
        audits = list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "inventory.set_days")
                )
            ).scalars()
        )
    assert len(audits) == 1 and audits[0].actor == "staff:1" and audits[0].details["days"] == 3
    grid = await inventory.grid(tenant_id, 2026, 10)
    assert (
        set(grid) == {"ez", "dz"} and len(grid["dz"]) == 31 and grid["dz"][5].price_cents == 15000
    )
    csv_text = (
        "﻿date;room_type;units_available;min_stay;closed_arrival;closed_departure;price_eur\r\n"
        '06.10.2026;dz;4;2;nein;ja;"1.250,00"\r\n'
        "07.10.2026;DZ;5;;0;;99,90\r\n"
        "08.10.2026;ez;2;1;x;;€ 80\r\n"
    )
    dry = await inventory.import_csv(tenant_id, csv_text, dry_run=True, actor="staff:1")
    assert dry.ok and len(dry.rows) == 3 and dry.applied == 0 and dry.rows[0].price_cents == 125000
    assert (
        dry.rows[1].room_type == "dz"
        and dry.rows[1].min_stay is None
        and dry.rows[1].closed_arrival is False
    )
    assert dry.rows[2].closed_arrival is True and dry.rows[2].price_cents == 8000
    async with hotel.services.ctx.db.scope() as session:
        row = await session.scalar(
            select(DayInventory).join(
                DayInventory.room_type_id == DayInventory.room_type_id and None
            )  # type: ignore[arg-type]
            if False
            else select(DayInventory).where(
                DayInventory.date == date(2026, 10, 6), DayInventory.units_available == 4
            )
        )
    assert row is None
    bad = await inventory.import_csv(
        tenant_id,
        "date,room_type,units_available\n2026-10-06,dz,4\n31.02.2026,dz,4\n2026-10-07,suite,1\n2026-10-08,dz,vielleicht\n",
        dry_run=True,
        actor="staff:1",
    )
    assert [line for line, _ in bad.errors] == [3, 5] and "date" in bad.errors[0][1]
    unknown = await inventory.import_csv(
        tenant_id,
        "date,room_type,units_available\n2026-10-07,suite,1\n",
        dry_run=False,
        actor="staff:1",
    )
    assert unknown.errors and "unknown room types: suite" in unknown.errors[0][1]
    applied = await inventory.import_csv(tenant_id, csv_text, dry_run=False, actor="staff:2")
    assert applied.ok and applied.applied == 3
    hotel.services.availability.reset()
    answer = await hotel.check(Stay(date(2026, 10, 6), date(2026, 10, 8)), OCC, "dz")
    assert answer.quotes[0].units_left == 4 and answer.quotes[0].total is not None
    assert answer.quotes[0].total.cents == 125000 + 9990
    assert (await inventory.import_csv(tenant_id, "", dry_run=True, actor="x")).errors[0][
        1
    ] == "empty file"
    with pytest.raises(ValueError, match="unknown room type"):
        await inventory.set_days(
            tenant_id, "suite", date(2026, 10, 6), date(2026, 10, 6), units_available=1, actor="x"
        )


# ----------------------------------------------------------------- iCal
FEED_BUSY = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:1\r\nDTSTART;VALUE=DATE:20261003\r\nDTEND;VALUE=DATE:20261005\r\nSUMMARY:Booked\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:2\r\nDTSTART;TZID=Europe/Berlin:20261010T150000\r\nDTEND;TZID=Europe/Berlin:20261012T110000\r\nSUMMARY:Booked\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)
FEED_IGNORED = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:3\r\nDTSTART;VALUE=DATE:20261003\r\nDTEND;VALUE=DATE:20261005\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:4\r\nDTSTART;VALUE=DATE:20261003\r\nDTEND;VALUE=DATE:20261005\r\nTRANSP:TRANSPARENT\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:5\r\nDTSTART;VALUE=DATE:20261020\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


@pytest.fixture
async def ical_hotel(hotel: Hotel) -> tuple[Hotel, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        host = request.headers.get("host", "")
        path = request.url.path
        if host == "calendar.example.invalid" and path == "/dz1.ics":
            if request.headers.get("if-none-match") == '"v1"':
                return httpx2.Response(304)
            return httpx2.Response(
                200,
                content=FEED_BUSY.encode(),
                headers={"content-type": "text/calendar", "etag": '"v1"'},
            )
        if host == "calendar.example.invalid" and path == "/dz2.ics":
            return httpx2.Response(
                200, content=FEED_IGNORED.encode(), headers={"content-type": "text/calendar"}
            )
        return httpx2.Response(404)

    async def resolver(host: str) -> list[str]:
        return ["10.0.0.5"] if host == "internal.invalid" else ["93.184.216.34"]

    factory = AdapterFactory(
        hotel.services.ctx.db,
        hotel.clock,
        hotel.services.secrets,
        fetcher=SafeFetcher(transport=httpx2.MockTransport(handler), resolver=resolver),
    )
    hotel.services.availability.factory = factory
    hotel.services.adapters = factory
    profile = hotel.profile.model_copy(
        update={
            "availability": hotel.profile.availability.model_copy(
                update={"chain": ["ical", "none"], "ical_units": {"dz": 2}, "ical_max_age_min": 15}
            )
        }
    )
    await hotel.services.tenants.update_profile(hotel.tenant.slug, profile, actor="test")
    await hotel.refresh()
    for name, url in (
        ("ical_url_dz_1", "https://calendar.example.invalid/dz1.ics"),
        ("ical_url_dz_2", "https://calendar.example.invalid/dz2.ics"),
    ):
        await hotel.services.secrets.set(hotel.tenant.id, name, url, actor="test")
    return hotel, seen


async def test_ical_edge_cases_conditional_get_and_wording(
    ical_hotel: tuple[Hotel, list[httpx2.Request]],
) -> None:
    hotel, seen = ical_hotel
    busy = await hotel.check(Stay(date(2026, 10, 3), date(2026, 10, 5)))
    assert busy.source == "ical" and busy.status == "available"
    assert busy.quotes[0].units_left == 1 and busy.quotes[0].per_night is None
    assert busy.quotes[0].room_name == "Doppelzimmer" and busy.quotes[0].max_occupancy == 2
    text = wording(busy, "en", hotel.services.texts, tz="Europe/Berlin")
    assert (
        "According to our calendar" in text
        and "confirmed by the reception" in text
        and "EUR" not in text
    )
    exclusive = await hotel.check(Stay(date(2026, 10, 5), date(2026, 10, 6)))
    assert exclusive.quotes[0].units_left == 2
    timed = await hotel.check(Stay(date(2026, 10, 10), date(2026, 10, 12)))
    assert timed.quotes[0].units_left == 1
    after = await hotel.check(Stay(date(2026, 10, 12), date(2026, 10, 13)))
    assert after.quotes[0].units_left == 2
    assert len([r for r in seen if r.url.path == "/dz1.ics"]) == 1  # cached within max age
    hotel.clock.advance(timedelta(minutes=16))
    hotel.services.availability.reset()
    again = await hotel.check(Stay(date(2026, 10, 3), date(2026, 10, 5)))
    fetches = [r for r in seen if r.url.path == "/dz1.ics"]
    assert len(fetches) == 2 and fetches[1].headers["if-none-match"] == '"v1"'
    assert again.quotes[0].units_left == 1
    adapter = await hotel.services.adapters("ical", hotel.tenant, hotel.profile)
    assert adapter is not None
    report = await adapter.doctor()
    assert report.ok and any("RRULE" in w for w in report.warnings)
    crowd = await hotel.check(Stay(date(2026, 10, 5), date(2026, 10, 6)), Occupancy(3, 0))
    assert crowd.quotes[0].restrictions[0].kind == "max_occupancy"


async def test_ical_blocked_feed_falls_through_to_none(
    ical_hotel: tuple[Hotel, list[httpx2.Request]],
) -> None:
    hotel, seen = ical_hotel
    await hotel.services.secrets.set(
        hotel.tenant.id, "ical_url_dz_2", "https://internal.invalid/x.ics", actor="test"
    )
    answer = await hotel.check(Stay(date(2026, 10, 3), date(2026, 10, 5)))
    assert answer.status == "unknown" and "ical:SourceUnavailable" in answer.reasons
    assert not any(r.headers.get("host") == "internal.invalid" for r in seen)


# -------------------------------------------------------------- inquiry
class RecordingAdapter(BaseAdapter):
    def __init__(self, name: str, *, allow_write: bool) -> None:
        self.name = name
        self.allow_write = allow_write
        self.inquiries: list[InquiryRequest] = []

    async def create_inquiry(self, inquiry: InquiryRequest) -> str:
        if self.name == "apaleo":
            raise AssertionError("apaleo must never be asked to write")
        if not self.allow_write:
            raise NotSupported("writes disabled")
        self.inquiries.append(inquiry)
        return f"{self.name}-inq-1"


async def test_inquiry_consent_card_data_dedupe_handoff_and_write_gate(hotel: Hotel) -> None:
    services = hotel.services
    guest = await services.conversations.get_or_create_guest(
        hotel.tenant.id, "web", "s1", lang="de"
    )
    conversation = await services.conversations.open_conversation(
        hotel.tenant.id, guest.id, "web", lang="de"
    )
    stay = Stay(date(2026, 10, 6), date(2026, 10, 8))
    form = InquiryForm(
        "Anna Muster", "anna@example.invalid", "", stay, OCC, "Zimmer mit Balkon", True, "de"
    )
    with pytest.raises(InquiryError) as no_consent:
        await services.inquiries.create(
            hotel.tenant,
            hotel.profile,
            InquiryForm("A", "a@x.invalid", "", stay, OCC, "w", False, "de"),
            conversation_id=conversation.id,
        )
    assert no_consent.value.code == "consent_required"
    with pytest.raises(InquiryError) as card:
        await services.inquiries.create(
            hotel.tenant,
            hotel.profile,
            InquiryForm("A", "a@x.invalid", "", stay, OCC, "Karte 4111 1111 1111 1111", True, "de"),
            conversation_id=conversation.id,
        )
    assert card.value.code == "card_data"
    with pytest.raises(InquiryError) as contact:
        await services.inquiries.create(
            hotel.tenant,
            hotel.profile,
            InquiryForm("A", "", "", stay, OCC, "w", True, "de"),
            conversation_id=conversation.id,
        )
    assert contact.value.code == "contact_required"
    result = await services.inquiries.create(
        hotel.tenant, hotel.profile, form, conversation_id=conversation.id
    )
    assert not result.duplicate and result.handoff_id and result.pms_ref is None
    again = await services.inquiries.create(
        hotel.tenant, hotel.profile, form, conversation_id=conversation.id
    )
    assert (
        again.duplicate
        and again.request_id == result.request_id
        and again.handoff_id == result.handoff_id
    )
    async with services.ctx.db.scope() as session:
        request = await session.get(GuestRequest, result.request_id)
        handoff = await session.get(Handoff, result.handoff_id)
    assert (
        request is not None
        and request.kind == "booking_inquiry"
        and "anna@example.invalid" not in str(request.details)
    )
    # the inquiry e-mail to the hotel is queued through the outbox (reply-to the guest)
    assert request.details["arrival"] == "2026-10-06" and request.details["email_pending"] is False
    queued = await services.email_outbox.rows(hotel.tenant.id)
    assert {r.category for r in queued} == {"inquiry", "alert"}  # plus the P2 staff alert
    inquiry_mail = next(r for r in queued if r.category == "inquiry")
    assert inquiry_mail.payload["reply_to"] == "anna@example.invalid"
    assert services.inquiries.contact_of(hotel.tenant.id, request) == {
        "name": "Anna Muster",
        "email": "anna@example.invalid",
        "phone": "",
    }
    assert (
        handoff is not None
        and handoff.priority == "P2"
        and handoff.trigger == "booking_inquiry"
        and "2026-10-06" in handoff.summary
    )
    # write gate: apaleo is never asked, beds24 only with allow_pms_write
    adapters = {
        "apaleo": RecordingAdapter("apaleo", allow_write=True),
        "beds24": RecordingAdapter("beds24", allow_write=True),
    }

    async def factory(source: str, tenant: Tenant, profile: TenantProfile) -> Any:
        return adapters.get(source)

    gated = InquiryService(
        services.ctx.db,
        hotel.clock,
        services.ctx.keyring,
        services.handoffs,
        AvailabilityService(hotel.clock, factory),
    )
    apaleo_profile = hotel.profile.model_copy(
        update={
            "availability": hotel.profile.availability.model_copy(
                update={"chain": ["apaleo", "none"], "allow_pms_write": True}
            )
        }
    )
    other = InquiryForm(
        "Ben Beispiel", "ben@example.invalid", "", stay, OCC, "Ruhiges Zimmer", True, "de"
    )
    result = await gated.create(hotel.tenant, apaleo_profile, other, conversation_id=None)
    assert result.pms_ref is None and result.handoff_id is None
    beds_profile = hotel.profile.model_copy(
        update={
            "availability": hotel.profile.availability.model_copy(
                update={"chain": ["beds24", "none"], "allow_pms_write": False}
            )
        }
    )
    third = InquiryForm("Cem Örnek", "", "+49 151 2223344", stay, OCC, "Spät anreisen", True, "tr")
    assert (
        await gated.create(hotel.tenant, beds_profile, third, conversation_id=None)
    ).pms_ref is None
    assert adapters["beds24"].inquiries == []
    writing = beds_profile.model_copy(
        update={
            "availability": beds_profile.availability.model_copy(update={"allow_pms_write": True})
        }
    )
    fourth = InquiryForm(
        "Dana Esempio", "dana@example.invalid", "", stay, OCC, "Vista", True, "it", room_type="101"
    )
    written = await gated.create(hotel.tenant, writing, fourth, conversation_id=None)
    assert (
        written.pms_ref == "beds24-inq-1" and adapters["beds24"].inquiries[0].last_name == "Esempio"
    )
