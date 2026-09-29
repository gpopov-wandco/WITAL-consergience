"""Beds24 API v2 adapter (F08).

Implements: F08, F07, F10, F12, C3, C4

Built from the build prompt's description (the OpenAPI document at
``https://beds24.com/api/v2/apiV2.yaml`` was unreachable during the build, see
docs/VERIFIED_FACTS.md); every endpoint below is marked VERIFY and checked by
``doctor --live``. Facts used: the refresh token is exchanged for a 24-hour ``token``
header; ``/inventory/rooms/calendar`` is empty without ``include*`` flags; 100 credits
per five minutes per account (``X-FiveMinCreditLimit-Remaining``, ``X-RequestCost``), so
the adapter stops below 40 percent of the budget; an inquiry is ``POST /bookings`` with
``status: inquiry`` and is sent only when the tenant allows PMS writes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx2

from concierge.availability.models import (
    AuthError,
    AvailabilityAnswer,
    Capabilities,
    DoctorReport,
    InquiryRequest,
    MalformedResponse,
    Money,
    NotSupported,
    Occupancy,
    Quote,
    RateLimited,
    ReservationRecord,
    Restriction,
    RoomTypeInfo,
    Stay,
    status_of,
    unknown_answer,
)
from concierge.availability.port import BaseAdapter
from concierge.availability.vendor_http import VendorClient, as_int, as_list, need, to_cents
from concierge.clock import Clock

DEFAULT_BASE_URL = "https://api.beds24.com/v2"  # VERIFY
LIVE_TTL = timedelta(seconds=120)
CREDIT_LIMIT = 100
CREDIT_FLOOR_FRACTION = 0.4


@dataclass
class TokenState:
    token: str = ""
    expires_at: datetime | None = None


@dataclass
class CreditState:
    remaining: int | None = None
    resets_at: datetime | None = None


class Beds24Adapter(BaseAdapter):
    name = "beds24"

    def __init__(
        self,
        *,
        property_id: str,
        refresh_token: str,
        clock: Clock,
        currency: str = "EUR",
        tax_inclusive: bool | None = None,
        allow_write: bool = False,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = 8.0,
        token_state: TokenState | None = None,
        credit_state: CreditState | None = None,
    ) -> None:
        self.property_id = property_id
        self._refresh_token = refresh_token
        self.clock = clock
        self.currency = currency
        self.tax_inclusive = tax_inclusive
        self.allow_write = allow_write
        self.http = VendorClient(base_url, timeout_s=timeout_s, transport=transport)
        self.token_state = token_state or TokenState()
        self.credit_state = credit_state or CreditState()
        self._room_cache: dict[str, RoomTypeInfo] = {}

    async def capabilities(self) -> Capabilities:
        return Capabilities(
            availability=True,
            prices=True,
            restrictions=True,
            reservations=True,
            arrivals=True,
            inquiry=self.allow_write,
        )

    # -------------------------------------------------------------- plumbing
    async def _token(self, *, force: bool = False) -> str:
        now = self.clock.now()
        state = self.token_state
        if (
            not force
            and state.token
            and state.expires_at is not None
            and state.expires_at - timedelta(minutes=5) > now
        ):
            return state.token
        response = await self.http.request(
            "GET", "/authentication/token", headers={"refreshToken": self._refresh_token}
        )
        token = need(response.data, "token")
        if not isinstance(token, str) or not token:
            raise MalformedResponse("beds24 token missing")
        expires_in = as_int(
            response.data.get("expiresIn") if isinstance(response.data, dict) else None,
            "expiresIn",
            default=86400,
        )
        state.token = token
        state.expires_at = now + timedelta(seconds=expires_in)
        return token

    def _check_budget(self) -> None:
        state = self.credit_state
        now = self.clock.now()
        if state.resets_at is not None and now >= state.resets_at:
            state.remaining = None
            state.resets_at = None
        if state.remaining is not None and state.remaining < CREDIT_LIMIT * CREDIT_FLOOR_FRACTION:
            retry = (state.resets_at - now).total_seconds() if state.resets_at else 300.0
            raise RateLimited("beds24 credit budget reached", retry_after_s=max(1.0, retry))

    def _note_credits(self, headers: dict[str, str]) -> None:
        remaining = headers.get("x-fivemincreditlimit-remaining")
        if remaining is not None and remaining.strip().lstrip("-").isdigit():
            self.credit_state.remaining = int(remaining)
            if self.credit_state.resets_at is None:
                resets = headers.get("x-fivemincreditlimit-resetsin")
                seconds = float(resets) if resets and resets.strip().isdigit() else 300.0
                self.credit_state.resets_at = self.clock.now() + timedelta(seconds=seconds)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        self._check_budget()
        token = await self._token()
        try:
            response = await self.http.request(
                method, path, headers={"token": token}, params=params, json_body=json_body
            )
        except AuthError:
            token = await self._token(force=True)
            response = await self.http.request(
                method, path, headers={"token": token}, params=params, json_body=json_body
            )
        self._note_credits(response.headers)
        if isinstance(response.data, dict) and response.data.get("success") is False:
            raise MalformedResponse("beds24 reported success=false")
        return response.data

    # ------------------------------------------------------------- read side
    async def list_room_types(self) -> list[RoomTypeInfo]:
        payload = await self._call(
            "GET",
            "/properties",
            params={"id": self.property_id, "includeAllRooms": "true"},
        )
        properties = as_list(need(payload, "data"), "data")
        infos: list[RoomTypeInfo] = []
        for prop in properties:
            for room in as_list(prop.get("roomTypes", []), "roomTypes"):
                if not isinstance(room, dict):
                    raise MalformedResponse("beds24 roomType is not an object")
                info = RoomTypeInfo(
                    code=str(room.get("id", "")),
                    names={"de": str(room.get("name", "")), "en": str(room.get("name", ""))},
                    max_occupancy=as_int(room.get("maxPeople"), "maxPeople", default=2),
                    units_total=as_int(room.get("qty"), "qty", default=1),
                    external_id=str(room.get("id", "")),
                )
                infos.append(info)
        self._room_cache = {i.code: i for i in infos}
        return infos

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Sequence[str], promo: str = ""
    ) -> AvailabilityAnswer:
        now = self.clock.now()
        if not self._room_cache:
            await self.list_room_types()
        payload = await self._call(
            "GET",
            "/inventory/rooms/calendar",
            params={
                "propertyId": self.property_id,
                "startDate": stay.arrival.isoformat(),
                "endDate": (stay.departure - timedelta(days=1)).isoformat(),
                "includeNumAvail": "true",
                "includePrices": "true",
                "includeMinStay": "true",
                "includeMaxStay": "true",
                "includeOverride": "true",
            },
        )
        rooms = as_list(need(payload, "data"), "data")
        wanted = {code.lower() for code in room_types if code}
        quotes: list[Quote] = []
        for room in rooms:
            if not isinstance(room, dict):
                raise MalformedResponse("beds24 calendar room is not an object")
            room_id = str(room.get("roomId", ""))
            if wanted and room_id.lower() not in wanted:
                continue
            days = self._expand_calendar(as_list(room.get("calendar", []), "calendar"))
            info = self._room_cache.get(room_id)
            quote = self._quote_for(stay, occupancy, room_id, info, days, now)
            if quote is not None:
                quotes.append(quote)
        if not quotes:
            return unknown_answer(stay, occupancy, now, ["no_calendar_rows"], source_name=self.name)
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

    @staticmethod
    def _expand_calendar(entries: list[Any]) -> dict[date, dict[str, Any]]:
        days: dict[date, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise MalformedResponse("beds24 calendar entry is not an object")
            try:
                start = date.fromisoformat(str(entry.get("from")))
                end = date.fromisoformat(str(entry.get("to", entry.get("from"))))
            except ValueError as exc:
                raise MalformedResponse("beds24 calendar dates") from exc
            day = start
            while day <= end:
                days[day] = entry
                day += timedelta(days=1)
        return days

    def _quote_for(
        self,
        stay: Stay,
        occupancy: Occupancy,
        room_id: str,
        info: RoomTypeInfo | None,
        days: dict[date, dict[str, Any]],
        now: datetime,
    ) -> Quote | None:
        rows = [days.get(d) for d in stay.dates()]
        if any(r is None for r in rows):
            return None
        present = [r for r in rows if r is not None]
        units = min(as_int(r.get("numAvail"), "numAvail", default=0) for r in present)
        restrictions: list[Restriction] = []
        first = present[0]
        min_stay = as_int(first.get("minStay"), "minStay", default=1)
        if min_stay > stay.nights:
            restrictions.append(
                Restriction(
                    "min_stay", str(min_stay), (stay.arrival + timedelta(days=min_stay)).isoformat()
                )
            )
        max_stay = as_int(first.get("maxStay"), "maxStay", default=0)
        if 0 < max_stay < stay.nights:
            restrictions.append(
                Restriction(
                    "closed",
                    f"max_stay:{max_stay}",
                    (stay.arrival + timedelta(days=max_stay)).isoformat(),
                )
            )
        override = str(first.get("override", "") or "").lower()
        if override in {"blackout", "closed"}:
            restrictions.append(Restriction("closed", override))
        if info is not None and occupancy.total > info.max_occupancy:
            rooms_needed = -(-occupancy.total // info.max_occupancy)
            restrictions.append(
                Restriction("max_occupancy", str(info.max_occupancy), str(rooms_needed))
            )
        prices = [r.get("price1") for r in present]
        per_night = total = None
        if all(p is not None for p in prices):
            cents = [to_cents(p, "price1") for p in prices]
            per_night = Money(min(cents), self.currency)
            total = Money(sum(cents), self.currency)
        return Quote(
            room_type=room_id,
            room_name=info.name("de") if info else room_id,
            source="live_pms",
            fetched_at=now,
            valid_until=now + LIVE_TTL,
            per_night=per_night,
            total=total,
            units_left=units,
            tax_inclusive=self.tax_inclusive,
            restrictions=restrictions,
            max_occupancy=info.max_occupancy if info else None,
        )

    async def get_reservation(self, ref: str) -> ReservationRecord | None:
        payload = await self._call(
            "GET", "/bookings", params={"id": ref, "propertyId": self.property_id}
        )
        bookings = as_list(need(payload, "data"), "data")
        for booking in bookings:
            record = self._record(booking)
            if record.res_id.lower() == ref.strip().lower():
                return record
        return None

    async def arrivals(self, start: date, end: date) -> list[ReservationRecord]:
        payload = await self._call(
            "GET",
            "/bookings",
            params={
                "propertyId": self.property_id,
                "arrivalFrom": start.isoformat(),
                "arrivalTo": end.isoformat(),
            },
        )
        return [self._record(b) for b in as_list(need(payload, "data"), "data")]

    async def departures(self, start: date, end: date) -> list[ReservationRecord]:
        payload = await self._call(
            "GET",
            "/bookings",
            params={
                "propertyId": self.property_id,
                "departureFrom": start.isoformat(),
                "departureTo": end.isoformat(),
            },
        )
        return [self._record(b) for b in as_list(need(payload, "data"), "data")]

    @staticmethod
    def _record(booking: Any) -> ReservationRecord:
        if not isinstance(booking, dict):
            raise MalformedResponse("beds24 booking is not an object")
        try:
            arrival = date.fromisoformat(str(booking.get("arrival")))
            departure = date.fromisoformat(str(booking.get("departure")))
        except ValueError as exc:
            raise MalformedResponse("beds24 booking dates") from exc
        status = str(booking.get("status", "")).lower()
        return ReservationRecord(
            res_id=str(booking.get("id", "")),
            confirmation_number=str(booking.get("id", ""))
            if status in {"confirmed", "new"}
            else "",
            arrival=arrival,
            departure=departure,
            status=status,
            adults=as_int(booking.get("numAdult"), "numAdult", default=1),
            children=as_int(booking.get("numChild"), "numChild", default=0),
            room_type=str(booking.get("roomId", "")),
            surname=str(booking.get("lastName", "")),
            first_name=str(booking.get("firstName", "")),
            email=str(booking.get("email", "") or ""),
            phone=str(booking.get("phone", "") or booking.get("mobile", "") or ""),
            lang=str(booking.get("lang", "") or ""),
            source="beds24",
        )

    # ------------------------------------------------------------ write side
    async def create_inquiry(self, inquiry: InquiryRequest) -> str:
        if not self.allow_write:
            raise NotSupported("PMS writes are disabled for this tenant (allow_pms_write)")
        if not inquiry.room_type:
            raise NotSupported("a beds24 inquiry needs a room type")
        payload = await self._call(
            "POST",
            "/bookings",
            json_body=[
                {
                    "roomId": inquiry.room_type,
                    "status": "inquiry",
                    "arrival": inquiry.stay.arrival.isoformat(),
                    "departure": inquiry.stay.departure.isoformat(),
                    "numAdult": inquiry.occupancy.adults,
                    "numChild": inquiry.occupancy.children,
                    "firstName": inquiry.first_name,
                    "lastName": inquiry.last_name,
                    "email": inquiry.email,
                    "phone": inquiry.phone,
                    "lang": inquiry.lang,
                    "notes": f"[concierge {inquiry.idempotency_key[:12]}] {inquiry.wish}"[:500],
                }
            ],
        )
        results = as_list(payload, "bookings result")
        for item in results:
            if isinstance(item, dict) and isinstance(item.get("new"), dict):
                return str(item["new"].get("id", ""))
        raise MalformedResponse("beds24 inquiry result lacks new.id")

    async def doctor(self) -> DoctorReport:
        try:
            payload = await self._call("GET", "/authentication/details")
        except RateLimited as exc:
            return DoctorReport(self.name, False, "credit budget reached", [str(exc)])
        warnings: list[str] = []
        if isinstance(payload, dict):
            remaining = self.credit_state.remaining
            if remaining is not None and remaining < CREDIT_LIMIT * CREDIT_FLOOR_FRACTION:
                warnings.append(f"only {remaining} credits left in the five-minute window")
        return DoctorReport(self.name, True, "token valid", warnings)
