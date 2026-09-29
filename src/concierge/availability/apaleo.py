"""apaleo adapter (F08): read-only, OAuth2 client credentials.

Implements: F08, F07, F12, C3, C4

Built from the build prompt's description (apaleo.dev was unreachable during the
build; endpoints VERIFY, checked by ``doctor --live``). ``/booking/v1/offers`` returns
price, fees, city tax and cancellation terms in one call. ``POST /booking/v1/bookings``
creates real reservations, so this adapter never writes, whatever the tenant's
``allow_pms_write`` says. Prices are gross (tax inclusive).
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

DEFAULT_API_URL = "https://api.apaleo.com"  # VERIFY
DEFAULT_IDENTITY_URL = "https://identity.apaleo.com/connect/token"  # VERIFY
LIVE_TTL = timedelta(seconds=120)


@dataclass
class TokenState:
    token: str = ""
    expires_at: datetime | None = None


class ApaleoAdapter(BaseAdapter):
    name = "apaleo"

    def __init__(
        self,
        *,
        property_id: str,
        client_id: str,
        client_secret: str,
        clock: Clock,
        api_url: str = DEFAULT_API_URL,
        identity_url: str = DEFAULT_IDENTITY_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = 8.0,
        token_state: TokenState | None = None,
    ) -> None:
        self.property_id = property_id
        self._client_id = client_id
        self._client_secret = client_secret
        self.clock = clock
        self.identity_url = identity_url
        self.http = VendorClient(api_url, timeout_s=timeout_s, transport=transport)
        self.token_state = token_state or TokenState()

    async def capabilities(self) -> Capabilities:
        return Capabilities(
            availability=True, prices=True, restrictions=True, reservations=True, arrivals=True
        )

    async def _token(self, *, force: bool = False) -> str:
        now = self.clock.now()
        state = self.token_state
        if not force and state.token and state.expires_at and state.expires_at > now:
            return state.token
        response = await self.http.request(
            "POST",
            self.identity_url,
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
        )
        token = need(response.data, "access_token")
        if not isinstance(token, str) or not token:
            raise MalformedResponse("apaleo token missing")
        expires_in = as_int(response.data.get("expires_in"), "expires_in", default=3600)
        state.token = token
        state.expires_at = now + timedelta(seconds=max(60, expires_in - 60))
        return token

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        token = await self._token()
        try:
            response = await self.http.request(
                "GET", path, headers={"Authorization": f"Bearer {token}"}, params=params
            )
        except AuthError:
            token = await self._token(force=True)
            response = await self.http.request(
                "GET", path, headers={"Authorization": f"Bearer {token}"}, params=params
            )
        return response.data

    async def list_room_types(self) -> list[RoomTypeInfo]:
        payload = await self._get(
            "/inventory/v1/unit-groups", {"propertyId": self.property_id, "pageSize": 100}
        )
        groups = as_list(need(payload, "unitGroups"), "unitGroups")
        infos: list[RoomTypeInfo] = []
        for group in groups:
            if not isinstance(group, dict):
                raise MalformedResponse("apaleo unit group is not an object")
            infos.append(
                RoomTypeInfo(
                    code=str(group.get("code", group.get("id", ""))),
                    names={"de": str(group.get("name", "")), "en": str(group.get("name", ""))},
                    max_occupancy=as_int(group.get("maxPersons"), "maxPersons", default=2),
                    units_total=as_int(group.get("memberCount"), "memberCount", default=1),
                    external_id=str(group.get("id", "")),
                )
            )
        return infos

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Sequence[str], promo: str = ""
    ) -> AvailabilityAnswer:
        now = self.clock.now()
        params: dict[str, Any] = {
            "propertyId": self.property_id,
            "arrival": stay.arrival.isoformat(),
            "departure": stay.departure.isoformat(),
            "adults": occupancy.adults,
            "timeSliceTemplate": "OverNight",
        }
        if promo:
            params["promoCode"] = promo
        payload = await self._get("/booking/v1/offers", params)
        offers = as_list(payload.get("offers", []) if isinstance(payload, dict) else None, "offers")
        wanted = {code.lower() for code in room_types if code}
        quotes: dict[str, Quote] = {}
        for offer in offers:
            if not isinstance(offer, dict):
                raise MalformedResponse("apaleo offer is not an object")
            group = need(offer, "unitGroup")
            code = str(group.get("code", group.get("id", "")))
            if wanted and code.lower() not in wanted:
                continue
            quote = self._quote_from_offer(offer, code, group, stay, occupancy, now)
            existing = quotes.get(code)
            # keep the cheapest rate plan per unit group
            if existing is None or (
                quote.total is not None
                and existing.total is not None
                and quote.total.cents < existing.total.cents
            ):
                quotes[code] = quote
        if not quotes:
            unavailable = await self._unavailable(stay, occupancy, wanted, now)
            if not unavailable:
                return unknown_answer(stay, occupancy, now, ["no_offers"], source_name=self.name)
            return AvailabilityAnswer(
                status=status_of(unavailable),
                source="live_pms",
                fetched_at=now,
                valid_until=now + LIVE_TTL,
                stay=stay,
                occupancy=occupancy,
                quotes=unavailable,
                updated_at=now,
                source_name=self.name,
            )
        return AvailabilityAnswer(
            status=status_of(list(quotes.values())),
            source="live_pms",
            fetched_at=now,
            valid_until=now + LIVE_TTL,
            stay=stay,
            occupancy=occupancy,
            quotes=list(quotes.values()),
            updated_at=now,
            source_name=self.name,
        )

    def _quote_from_offer(
        self,
        offer: dict[str, Any],
        code: str,
        group: dict[str, Any],
        stay: Stay,
        occupancy: Occupancy,
        now: datetime,
    ) -> Quote:
        gross = need(offer, "totalGrossAmount")
        total_cents = to_cents(gross.get("amount"), "totalGrossAmount.amount")
        currency = str(gross.get("currency", "EUR"))
        slices = offer.get("timeSlices") or []
        per_night_cents: int | None = None
        if isinstance(slices, list) and slices:
            amounts = []
            for item in slices:
                if isinstance(item, dict) and isinstance(item.get("totalGrossAmount"), dict):
                    amounts.append(to_cents(item["totalGrossAmount"].get("amount"), "slice amount"))
            if amounts:
                per_night_cents = min(amounts)
        if per_night_cents is None and stay.nights:
            per_night_cents = total_cents // stay.nights
        extras: dict[str, str] = {}
        city_tax = offer.get("cityTax")
        if isinstance(city_tax, dict) and isinstance(city_tax.get("totalGrossAmount"), dict):
            extras["city_tax"] = (
                f"{Money(to_cents(city_tax['totalGrossAmount'].get('amount'), 'cityTax'), currency).format('en')}"
            )
        cancellation = offer.get("cancellationFee")
        if isinstance(cancellation, dict) and cancellation.get("description"):
            extras["cancellation"] = str(cancellation["description"])[:300]
        fees = offer.get("fees")
        if isinstance(fees, list) and fees:
            names = [str(f.get("name", "")) for f in fees if isinstance(f, dict)]
            extras["fees"] = ", ".join(n for n in names if n)[:300]
        if occupancy.children:
            extras["children"] = "child prices to be confirmed by the reception"
        restrictions: list[Restriction] = []
        max_persons = as_int(group.get("maxPersons"), "maxPersons", default=99)
        if occupancy.total > max_persons:
            restrictions.append(
                Restriction(
                    "max_occupancy", str(max_persons), str(-(-occupancy.total // max_persons))
                )
            )
        units = offer.get("availableUnits")
        return Quote(
            room_type=code,
            room_name=str(group.get("name", code)),
            source="live_pms",
            fetched_at=now,
            valid_until=now + LIVE_TTL,
            per_night=Money(per_night_cents, currency) if per_night_cents is not None else None,
            total=Money(total_cents, currency),
            units_left=as_int(units, "availableUnits") if units is not None else None,
            tax_inclusive=True,
            restrictions=restrictions,
            max_occupancy=max_persons if max_persons != 99 else None,
            extras=extras,
        )

    async def _unavailable(
        self, stay: Stay, occupancy: Occupancy, wanted: set[str], now: datetime
    ) -> list[Quote]:
        payload = await self._get(
            "/availability/v1/unit-groups",
            {
                "propertyId": self.property_id,
                "from": stay.arrival.isoformat(),
                "to": stay.departure.isoformat(),
                "adults": occupancy.adults,
                "timeSliceTemplate": "OverNight",
            },
        )
        slices = as_list(
            payload.get("timeSlices", []) if isinstance(payload, dict) else None, "timeSlices"
        )
        sellable: dict[str, int] = {}
        names: dict[str, str] = {}
        for item in slices:
            if not isinstance(item, dict):
                raise MalformedResponse("apaleo time slice is not an object")
            for group_item in as_list(item.get("unitGroups", []), "unitGroups"):
                group = need(group_item, "unitGroup")
                code = str(group.get("code", group.get("id", "")))
                names[code] = str(group.get("name", code))
                count = as_int(group_item.get("sellableCount"), "sellableCount", default=0)
                sellable[code] = min(sellable.get(code, count), count)
        quotes: list[Quote] = []
        for code, count in sellable.items():
            if wanted and code.lower() not in wanted:
                continue
            restrictions = [] if count <= 0 else [Restriction("closed", "rate_plan_restriction")]
            quotes.append(
                Quote(
                    room_type=code,
                    room_name=names.get(code, code),
                    source="live_pms",
                    fetched_at=now,
                    valid_until=now + LIVE_TTL,
                    units_left=count,
                    restrictions=restrictions,
                    tax_inclusive=True,
                )
            )
        return quotes

    async def get_reservation(self, ref: str) -> ReservationRecord | None:
        token = await self._token()
        response = await self.http.request(
            "GET",
            f"/booking/v1/reservations/{ref.strip()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status == 404 or response.data is None:
            return None
        return self._record(response.data)

    async def arrivals(self, start: date, end: date) -> list[ReservationRecord]:
        return await self._reservations("Arrival", start, end)

    async def departures(self, start: date, end: date) -> list[ReservationRecord]:
        return await self._reservations("Departure", start, end)

    async def _reservations(self, kind: str, start: date, end: date) -> list[ReservationRecord]:
        payload = await self._get(
            "/booking/v1/reservations",
            {
                "propertyIds": self.property_id,
                "dateFilter": kind,
                "from": start.isoformat(),
                "to": (end + timedelta(days=1)).isoformat(),
                "pageSize": 100,
            },
        )
        rows = payload.get("reservations", []) if isinstance(payload, dict) else None
        return [self._record(r) for r in as_list(rows, "reservations")]

    @staticmethod
    def _record(data: Any) -> ReservationRecord:
        if not isinstance(data, dict):
            raise MalformedResponse("apaleo reservation is not an object")
        try:
            arrival = datetime.fromisoformat(str(data.get("arrival"))).date()
            departure = datetime.fromisoformat(str(data.get("departure"))).date()
        except ValueError as exc:
            raise MalformedResponse("apaleo reservation dates") from exc
        raw_guest = data.get("primaryGuest")
        guest: dict[str, Any] = raw_guest if isinstance(raw_guest, dict) else {}
        status = str(data.get("status", "")).lower()
        children = data.get("childrenAges")
        return ReservationRecord(
            res_id=str(data.get("id", "")),
            confirmation_number=str(data.get("id", ""))
            if status in {"confirmed", "inhouse", "checkedout"}
            else "",
            arrival=arrival,
            departure=departure,
            status=status,
            adults=as_int(data.get("adults"), "adults", default=1),
            children=len(children) if isinstance(children, list) else 0,
            room_type=str((data.get("unitGroup") or {}).get("code", "")),
            surname=str(guest.get("lastName", "")),
            first_name=str(guest.get("firstName", "")),
            email=str(guest.get("email", "") or ""),
            phone=str(guest.get("phone", "") or ""),
            lang=str(guest.get("preferredLanguage", "") or ""),
            source="apaleo",
            rate_plan=str((data.get("ratePlan") or {}).get("code", "")),
        )

    async def create_inquiry(self, inquiry: InquiryRequest) -> str:
        raise NotSupported("apaleo bookings are confirmed reservations; inquiries go to staff")

    async def doctor(self) -> DoctorReport:
        payload = await self._get(f"/inventory/v1/properties/{self.property_id}", {})
        name = str(payload.get("name", "")) if isinstance(payload, dict) else ""
        return DoctorReport(self.name, True, f"property {name or self.property_id} readable")
