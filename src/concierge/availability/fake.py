"""A scripted PMS port (F07, F35): live-source behaviour without a vendor.

Implements: F07, F35

``ScriptedPms`` answers like a live PMS (``source`` ``live_pms``) from values a test or
the golden runner gives it, including third-party texts in ``extras`` so the policy's
untrusted-text wrapping can be asserted. It never talks to the network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from concierge.availability.models import (
    AvailabilityAnswer,
    Capabilities,
    DoctorReport,
    Money,
    Occupancy,
    Quote,
    ReservationRecord,
    Restriction,
    RoomTypeInfo,
    Status,
    Stay,
)
from concierge.availability.port import BaseAdapter
from concierge.clock import Clock
from concierge.security.ssrf import FetchResult

LIVE_VALIDITY_S = 120


@dataclass
class ScriptedRoom:
    code: str
    names: dict[str, str]
    per_night_cents: int
    units_left: int = 3
    max_occupancy: int = 2
    restrictions: list[Restriction] = field(default_factory=list)
    extras: dict[str, str] = field(default_factory=dict)


class ScriptedPms(BaseAdapter):
    """Quotes and reservations scripted in memory; ``fail_with`` makes every call raise."""

    name = "scripted_pms"

    def __init__(
        self,
        clock: Clock,
        rooms: list[ScriptedRoom],
        *,
        reservations: list[ReservationRecord] | None = None,
        tax_inclusive: bool | None = True,
        fail_with: Exception | None = None,
    ) -> None:
        self.clock = clock
        self.rooms = rooms
        self.reservations = list(reservations or [])
        self.tax_inclusive = tax_inclusive
        self.fail_with = fail_with
        self.calls: list[tuple[str, object]] = []

    def _check(self, call: str, arg: object) -> None:
        self.calls.append((call, arg))
        if self.fail_with is not None:
            raise self.fail_with

    async def capabilities(self) -> Capabilities:
        return Capabilities(
            availability=True, prices=True, restrictions=True, reservations=True, arrivals=True
        )

    async def list_room_types(self) -> list[RoomTypeInfo]:
        self._check("list_room_types", None)
        return [
            RoomTypeInfo(r.code, dict(r.names), r.max_occupancy, r.units_left) for r in self.rooms
        ]

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Sequence[str], promo: str = ""
    ) -> AvailabilityAnswer:
        room_type_ids = list(room_types)
        self._check("quote", (stay, occupancy, tuple(room_type_ids)))
        now = self.clock.now()
        quotes: list[Quote] = []
        for room in self.rooms:
            if room_type_ids and room.code not in room_type_ids:
                continue
            fits = occupancy.total <= room.max_occupancy
            restrictions = list(room.restrictions)
            if not fits:
                restrictions.append(Restriction("max_occupancy", str(room.max_occupancy)))
            quotes.append(
                Quote(
                    room_type=room.code,
                    room_name=room.names.get("en", room.code),
                    source="live_pms",
                    fetched_at=now,
                    valid_until=now + timedelta(seconds=LIVE_VALIDITY_S),
                    per_night=Money(room.per_night_cents),
                    total=Money(room.per_night_cents * stay.nights),
                    units_left=room.units_left if room.units_left > 0 else 0,
                    tax_inclusive=self.tax_inclusive,
                    restrictions=restrictions,
                    max_occupancy=room.max_occupancy,
                    extras=dict(room.extras),
                )
            )
        available = [q for q in quotes if (q.units_left or 0) > 0 and not q.restrictions]
        restricted = [q for q in quotes if (q.units_left or 0) > 0 and q.restrictions]
        status: Status = "available" if available else "restricted" if restricted else "unavailable"
        return AvailabilityAnswer(
            status=status,
            source="live_pms",
            fetched_at=now,
            valid_until=now + timedelta(seconds=LIVE_VALIDITY_S),
            stay=stay,
            occupancy=occupancy,
            quotes=quotes,
            source_name=self.name,
        )

    async def get_reservation(self, ref: str) -> ReservationRecord | None:
        self._check("get_reservation", ref)
        return next((r for r in self.reservations if r.res_id == ref), None)

    async def arrivals(self, start: date, end: date) -> list[ReservationRecord]:
        self._check("arrivals", (start, end))
        return [r for r in self.reservations if start <= r.arrival <= end]

    async def departures(self, start: date, end: date) -> list[ReservationRecord]:
        self._check("departures", (start, end))
        return [r for r in self.reservations if start <= r.departure <= end]

    async def doctor(self) -> DoctorReport:
        return DoctorReport(source=self.name, ok=self.fail_with is None, detail="scripted")


def default_rooms(per_night_cents: int = 13500) -> list[ScriptedRoom]:
    """One double room type; the price is a fixture value, not a hotel fact."""
    return [
        ScriptedRoom(
            "dz",
            {"de": "Doppelzimmer", "en": "Double room"},
            per_night_cents,
            units_left=3,
            max_occupancy=2,
        )
    ]


class StaticFetcher:
    """Serves iCal feeds (or any page) from memory with the ``SafeFetcher`` result shape."""

    def __init__(self, pages: dict[str, str], *, content_type: str = "text/calendar") -> None:
        self.pages = dict(pages)
        self.content_type = content_type
        self.requests: list[str] = []

    async def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResult:
        self.requests.append(url)
        body = self.pages.get(url)
        if body is None:
            return FetchResult(url=url, status=404, content_type="text/plain", body=b"")
        return FetchResult(
            url=url, status=200, content_type=self.content_type, body=body.encode("utf-8")
        )
