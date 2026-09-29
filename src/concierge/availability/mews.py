"""Mews adapter (F08): Distributor API for keyless reads, Connector API for reservations.

Implements: F08, F07, F10, F12, C3, C4

Built from the build prompt's description (api.mews.com was unreachable during the
build; endpoints VERIFY, checked by ``doctor --live``). Time units are UTC midnight of
the local day: local dates are converted at this boundary and back (rule 8). The
Connector API needs Mews certification for production; an inquiry is
``reservations/add`` with state ``Optional`` or ``Inquired`` and is sent only when
the tenant allows PMS writes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx2

from concierge.availability.models import (
    AvailabilityAnswer,
    Capabilities,
    DoctorReport,
    InquiryRequest,
    MalformedResponse,
    Money,
    NotConfigured,
    NotSupported,
    Occupancy,
    Quote,
    ReservationRecord,
    Restriction,
    RoomTypeInfo,
    Stay,
    status_of,
    unknown_answer,
)
from concierge.availability.port import BaseAdapter
from concierge.availability.vendor_http import VendorClient, as_int, as_list, need, to_cents
from concierge.clock import Clock, local_date, local_to_utc

PRODUCTION_HOST = "https://api.mews.com"  # VERIFY
DEMO_HOST = "https://api.mews-demo.com"  # VERIFY
LIVE_TTL = timedelta(seconds=120)
CLIENT_NAME = "WITALCO Guest Concierge 0.1"


def utc_midnight(day: date, tz: str) -> str:
    """Mews time unit boundary: local midnight of ``day`` as a UTC ISO string."""
    return local_to_utc(day, time(0, 0), tz).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day(value: str, tz: str) -> date:
    """The local calendar day of a Mews UTC instant (a time unit start or end)."""
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MalformedResponse("mews timestamp") from exc
    # time units start at local midnight; a value a few hours before midnight UTC is the
    # same local day once converted, so no rounding is needed
    return local_date(instant, tz)


class MewsAdapter(BaseAdapter):
    name = "mews"

    def __init__(
        self,
        *,
        hotel_id: str,
        configuration_id: str,
        clock: Clock,
        tz: str,
        currency: str = "EUR",
        client_token: str = "",
        access_token: str = "",
        demo: bool = False,
        allow_write: bool = False,
        base_url: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        self.hotel_id = hotel_id
        self.configuration_id = configuration_id
        self.clock = clock
        self.tz = tz
        self.currency = currency
        self._client_token = client_token
        self._access_token = access_token
        self.allow_write = allow_write
        host = base_url or (DEMO_HOST if demo else PRODUCTION_HOST)
        self.http = VendorClient(host, timeout_s=timeout_s, transport=transport)
        self._categories: dict[str, RoomTypeInfo] = {}

    @property
    def connector_configured(self) -> bool:
        return bool(self._client_token and self._access_token)

    async def capabilities(self) -> Capabilities:
        return Capabilities(
            availability=True,
            prices=True,
            reservations=self.connector_configured,
            arrivals=self.connector_configured,
            inquiry=self.connector_configured and self.allow_write,
        )

    async def _distributor(self, path: str, body: dict[str, Any]) -> Any:
        response = await self.http.request(
            "POST",
            f"/api/distributor/v1/{path}",
            json_body={"Client": CLIENT_NAME, **body},
        )
        return response.data

    async def _connector(self, path: str, body: dict[str, Any]) -> Any:
        if not self.connector_configured:
            raise NotConfigured("mews connector tokens are not configured")
        response = await self.http.request(
            "POST",
            f"/api/connector/v1/{path}",
            json_body={
                "ClientToken": self._client_token,
                "AccessToken": self._access_token,
                "Client": CLIENT_NAME,
                **body,
            },
        )
        return response.data

    async def list_room_types(self) -> list[RoomTypeInfo]:
        payload = await self._distributor("hotels/get", {"HotelId": self.hotel_id})
        categories = as_list(need(payload, "RoomCategories"), "RoomCategories")
        infos: list[RoomTypeInfo] = []
        for category in categories:
            if not isinstance(category, dict):
                raise MalformedResponse("mews room category is not an object")
            names_raw = category.get("Name")
            names: dict[str, str] = {}
            if isinstance(names_raw, dict):
                for key, value in names_raw.items():
                    names[str(key).split("-")[0].lower()] = str(value)
            elif isinstance(names_raw, str):
                names["en"] = names_raw
            beds = as_int(category.get("NormalBedCount"), "NormalBedCount", default=2)
            extra = as_int(category.get("ExtraBedCount"), "ExtraBedCount", default=0)
            infos.append(
                RoomTypeInfo(
                    code=str(category.get("Id", "")),
                    names=names,
                    max_occupancy=beds + extra,
                    units_total=as_int(category.get("SpaceCount"), "SpaceCount", default=1),
                    external_id=str(category.get("Id", "")),
                )
            )
        self._categories = {i.code: i for i in infos}
        return infos

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Sequence[str], promo: str = ""
    ) -> AvailabilityAnswer:
        now = self.clock.now()
        if not self._categories:
            await self.list_room_types()
        body: dict[str, Any] = {
            "HotelId": self.hotel_id,
            "ConfigurationId": self.configuration_id,
            "StartUtc": utc_midnight(stay.arrival, self.tz),
            "EndUtc": utc_midnight(stay.departure, self.tz),
            "AdultCount": occupancy.adults,
            "ChildCount": occupancy.children,
            "CurrencyCode": self.currency,
        }
        if promo:
            body["VoucherCode"] = promo
        payload = await self._distributor("hotels/getAvailability", body)
        rows = as_list(need(payload, "RoomCategoryAvailabilities"), "RoomCategoryAvailabilities")
        wanted = {code.lower() for code in room_types if code}
        quotes: list[Quote] = []
        for row in rows:
            if not isinstance(row, dict):
                raise MalformedResponse("mews availability row is not an object")
            category_id = str(row.get("RoomCategoryId", ""))
            if wanted and category_id.lower() not in wanted:
                continue
            info = self._categories.get(category_id)
            count = as_int(row.get("AvailableRoomCount"), "AvailableRoomCount", default=0)
            per_night = total = None
            occupancies = row.get("RoomOccupancyAvailabilities")
            if isinstance(occupancies, list):
                for occ in occupancies:
                    if not isinstance(occ, dict):
                        continue
                    if as_int(occ.get("AdultCount"), "AdultCount", default=0) != occupancy.adults:
                        continue
                    pricing = occ.get("Pricing")
                    if not isinstance(pricing, list):
                        continue
                    for rate in pricing:
                        if not isinstance(rate, dict) or not isinstance(rate.get("Price"), dict):
                            continue
                        price = rate["Price"]
                        totals = price.get("Total")
                        if isinstance(totals, dict) and self.currency in totals:
                            cents = to_cents(totals[self.currency], "Price.Total")
                            if total is None or cents < total.cents:
                                total = Money(cents, self.currency)
                                average = price.get("AverageAmountPerTimeUnit")
                                if isinstance(average, dict) and self.currency in average:
                                    per_night = Money(
                                        to_cents(average[self.currency], "AveragePerTimeUnit"),
                                        self.currency,
                                    )
                                elif stay.nights:
                                    per_night = Money(cents // stay.nights, self.currency)
            restrictions: list[Restriction] = []
            if info is not None and occupancy.total > info.max_occupancy:
                restrictions.append(
                    Restriction(
                        "max_occupancy",
                        str(info.max_occupancy),
                        str(-(-occupancy.total // info.max_occupancy)),
                    )
                )
            quotes.append(
                Quote(
                    room_type=category_id,
                    room_name=info.name("de") if info else category_id,
                    source="live_pms",
                    fetched_at=now,
                    valid_until=now + LIVE_TTL,
                    per_night=per_night,
                    total=total,
                    units_left=count,
                    tax_inclusive=True,
                    restrictions=restrictions,
                    max_occupancy=info.max_occupancy if info else None,
                )
            )
        if not quotes:
            return unknown_answer(stay, occupancy, now, ["no_categories"], source_name=self.name)
        return AvailabilityAnswer(
            status=status_of(quotes),
            source="live_pms",
            fetched_at=now,
            valid_until=now + LIVE_TTL,
            stay=stay,
            occupancy=occupancy,
            quotes=quotes,
            updated_at=now,
            source_name=self.name,
        )

    # ------------------------------------------------------------ connector
    async def get_reservation(self, ref: str) -> ReservationRecord | None:
        if not self.connector_configured:
            raise NotSupported("mews connector not configured")
        payload = await self._connector(
            "reservations/getAll",
            {"Numbers": [ref.strip()], "Extent": {"Reservations": True, "Customers": True}},
        )
        records = self._records(payload)
        for record in records:
            if record.res_id.lower() == ref.strip().lower():
                return record
        return None

    async def arrivals(self, start: date, end: date) -> list[ReservationRecord]:
        return await self._movements("ScheduledStartUtc", start, end)

    async def departures(self, start: date, end: date) -> list[ReservationRecord]:
        return await self._movements("ScheduledEndUtc", start, end)

    async def _movements(self, field: str, start: date, end: date) -> list[ReservationRecord]:
        if not self.connector_configured:
            raise NotSupported("mews connector not configured")
        payload = await self._connector(
            "reservations/getAll",
            {
                field: {
                    "StartUtc": utc_midnight(start, self.tz),
                    "EndUtc": utc_midnight(end + timedelta(days=1), self.tz),
                },
                "States": ["Confirmed", "Started", "Optional", "Enquired"],
                "Extent": {"Reservations": True, "Customers": True},
            },
        )
        return self._records(payload)

    def _records(self, payload: Any) -> list[ReservationRecord]:
        reservations = as_list(need(payload, "Reservations"), "Reservations")
        customers: dict[str, dict[str, Any]] = {}
        raw_customers = payload.get("Customers") if isinstance(payload, dict) else None
        if isinstance(raw_customers, list):
            for customer in raw_customers:
                if isinstance(customer, dict):
                    customers[str(customer.get("Id", ""))] = customer
        out: list[ReservationRecord] = []
        for item in reservations:
            if not isinstance(item, dict):
                raise MalformedResponse("mews reservation is not an object")
            customer = customers.get(str(item.get("CustomerId", "")), {})
            state = str(item.get("State", "")).lower()
            number = str(item.get("Number", item.get("Id", "")))
            out.append(
                ReservationRecord(
                    res_id=number,
                    confirmation_number=number
                    if state in {"confirmed", "started", "processed"}
                    else "",
                    arrival=local_day(str(need(item, "StartUtc")), self.tz),
                    departure=local_day(str(need(item, "EndUtc")), self.tz),
                    status=state,
                    adults=as_int(item.get("AdultCount"), "AdultCount", default=1),
                    children=as_int(item.get("ChildCount"), "ChildCount", default=0),
                    room_type=str(item.get("RequestedCategoryId", "")),
                    surname=str(customer.get("LastName", "")),
                    first_name=str(customer.get("FirstName", "")),
                    email=str(customer.get("Email", "") or ""),
                    phone=str(customer.get("Phone", "") or ""),
                    lang=str(customer.get("LanguageCode", "") or "").split("-")[0],
                    source="mews",
                )
            )
        return out

    async def create_inquiry(self, inquiry: InquiryRequest) -> str:
        if not self.allow_write:
            raise NotSupported("PMS writes are disabled for this tenant (allow_pms_write)")
        if not self.connector_configured:
            raise NotSupported("mews connector not configured")
        customer = await self._connector(
            "customers/add",
            {
                "OverwriteExisting": False,
                "FirstName": inquiry.first_name,
                "LastName": inquiry.last_name,
                "Email": inquiry.email or None,
                "Phone": inquiry.phone or None,
                "LanguageCode": inquiry.lang,
            },
        )
        customer_id = need(customer, "Id")
        payload = await self._connector(
            "reservations/add",
            {
                "Reservations": [
                    {
                        "Identifier": inquiry.idempotency_key[:32],
                        "State": "Optional",
                        "StartUtc": utc_midnight(inquiry.stay.arrival, self.tz),
                        "EndUtc": utc_midnight(inquiry.stay.departure, self.tz),
                        "AdultCount": inquiry.occupancy.adults,
                        "ChildCount": inquiry.occupancy.children,
                        "RequestedCategoryId": inquiry.room_type or None,
                        "CustomerId": customer_id,
                        "Notes": inquiry.wish[:500],
                    }
                ]
            },
        )
        reservations = as_list(need(payload, "Reservations"), "Reservations")
        for item in reservations:
            if isinstance(item, dict) and isinstance(item.get("Reservation"), dict):
                return str(item["Reservation"].get("Number", item["Reservation"].get("Id", "")))
            if isinstance(item, dict) and item.get("Number"):
                return str(item["Number"])
        raise MalformedResponse("mews reservations/add result lacks a number")

    async def doctor(self) -> DoctorReport:
        payload = await self._distributor("hotels/get", {"HotelId": self.hotel_id})
        warnings: list[str] = []
        tz_name = str(payload.get("TimeZoneIdentifier", "")) if isinstance(payload, dict) else ""
        if tz_name and tz_name != self.tz:
            warnings.append(f"Mews time zone {tz_name} differs from the tenant's {self.tz}")
        if not self.connector_configured:
            warnings.append("connector tokens missing: no reservation lookup, no arrivals")
        return DoctorReport(self.name, True, "distributor readable", warnings)
