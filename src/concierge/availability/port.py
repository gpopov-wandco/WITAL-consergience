"""The availability port every source implements (F07).

Implements: F07, F08, F09, 6.2

Adapters raise ``AvailabilityError`` subclasses; the chain decides what to do next.
``BaseAdapter`` provides the ``NotSupported`` defaults so a source implements only what
it has (an iCal feed has no prices, no reservations, no inquiries).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from concierge.availability.models import (
    AvailabilityAnswer,
    Capabilities,
    DoctorReport,
    InquiryRequest,
    NotSupported,
    Occupancy,
    ReservationRecord,
    RoomTypeInfo,
    Stay,
)


class AvailabilityPort(Protocol):
    name: str

    async def capabilities(self) -> Capabilities: ...

    async def list_room_types(self) -> list[RoomTypeInfo]: ...

    async def availability(self, stay: Stay, occupancy: Occupancy) -> AvailabilityAnswer: ...

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Sequence[str], promo: str = ""
    ) -> AvailabilityAnswer: ...

    async def get_reservation(self, ref: str) -> ReservationRecord | None: ...

    async def arrivals(self, start: date, end: date) -> list[ReservationRecord]: ...

    async def departures(self, start: date, end: date) -> list[ReservationRecord]: ...

    async def create_inquiry(self, inquiry: InquiryRequest) -> str: ...

    async def doctor(self) -> DoctorReport: ...


class BaseAdapter:
    name = "base"

    async def capabilities(self) -> Capabilities:
        return Capabilities()

    async def list_room_types(self) -> list[RoomTypeInfo]:
        return []

    async def availability(self, stay: Stay, occupancy: Occupancy) -> AvailabilityAnswer:
        return await self.quote(stay, occupancy, [])

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Sequence[str], promo: str = ""
    ) -> AvailabilityAnswer:
        raise NotSupported(f"{self.name} has no availability")

    async def get_reservation(self, ref: str) -> ReservationRecord | None:
        raise NotSupported(f"{self.name} has no reservations")

    async def arrivals(self, start: date, end: date) -> list[ReservationRecord]:
        raise NotSupported(f"{self.name} has no arrivals")

    async def departures(self, start: date, end: date) -> list[ReservationRecord]:
        raise NotSupported(f"{self.name} has no departures")

    async def create_inquiry(self, inquiry: InquiryRequest) -> str:
        raise NotSupported(f"{self.name} cannot create inquiries")

    async def doctor(self) -> DoctorReport:
        return DoctorReport(source=self.name, ok=True)
