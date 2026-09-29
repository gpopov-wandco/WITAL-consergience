"""Availability domain types (F07): money in integer cents, quotes that name their source.

Implements: F07, C6, C17

A ``Quote`` is never binding: the assistant quotes numbers from it and names the
source; a booking exists only when a PMS reservation with a confirmation number is
returned by ``get_reservation``. ``valid_until`` drives re-queries (rule 5).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

Source = Literal["live_pms", "live_pms_cached", "ical", "manual", "none"]
Status = Literal["available", "unavailable", "restricted", "unknown"]
PMS_SOURCES: frozenset[str] = frozenset({"beds24", "apaleo", "mews", "smoobu", "ibelsa"})


class AvailabilityError(Exception):
    """Base class for adapter failures the chain treats as 'try the next source'."""


class NotSupported(AvailabilityError):
    """The adapter does not implement this operation."""


class NotConfigured(AvailabilityError):
    """Required profile fields or secrets are missing."""


class SourceUnavailable(AvailabilityError):
    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class AuthError(SourceUnavailable):
    """Credentials rejected (401/403); alerts the operator, never retried in a loop."""


class RateLimited(SourceUnavailable):
    """The vendor (or our own budget) says slow down."""


class MalformedResponse(SourceUnavailable):
    """The vendor answered with a body the adapter cannot interpret."""


@dataclass(frozen=True)
class Money:
    cents: int
    currency: str = "EUR"

    def format(self, lang: str) -> str:
        whole, frac = divmod(abs(self.cents), 100)
        sign = "-" if self.cents < 0 else ""
        separator = "." if lang == "en" else ","
        amount = f"{sign}{whole}" if frac == 0 else f"{sign}{whole}{separator}{frac:02d}"
        return f"{amount} {self.currency}"


@dataclass(frozen=True)
class Occupancy:
    adults: int
    children: int = 0

    @property
    def total(self) -> int:
        return self.adults + self.children


@dataclass(frozen=True)
class Stay:
    arrival: date
    departure: date

    def __post_init__(self) -> None:
        if self.departure <= self.arrival:
            raise ValueError("departure must be after arrival")

    @property
    def nights(self) -> int:
        return (self.departure - self.arrival).days

    def dates(self) -> Iterator[date]:
        """The nights of the stay: arrival up to, not including, departure (DTEND exclusive)."""
        for offset in range(self.nights):
            yield self.arrival + timedelta(days=offset)


@dataclass(frozen=True)
class RoomTypeInfo:
    code: str
    names: dict[str, str]
    max_occupancy: int
    units_total: int
    external_id: str = ""

    def name(self, lang: str) -> str:
        return self.names.get(lang) or self.names.get("de") or self.names.get("en") or self.code


@dataclass(frozen=True)
class Restriction:
    kind: Literal["min_stay", "closed_arrival", "closed_departure", "max_occupancy", "closed"]
    value: str
    alternative: str = ""
    """A concrete alternative in machine form (an ISO date or a count), rendered by the policy."""


@dataclass
class Quote:
    room_type: str
    room_name: str
    source: Source
    fetched_at: datetime
    valid_until: datetime
    per_night: Money | None = None
    total: Money | None = None
    units_left: int | None = None
    tax_inclusive: bool | None = None
    restrictions: list[Restriction] = field(default_factory=list)
    booking_url: str | None = None
    max_occupancy: int | None = None
    extras: dict[str, str] = field(default_factory=dict)
    """Third-party texts (city tax, cancellation, fees); shown as untrusted text only."""
    binding: bool = False

    @property
    def available(self) -> bool:
        return not self.restrictions and (self.units_left is None or self.units_left > 0)


@dataclass
class AvailabilityAnswer:
    status: Status
    source: Source
    fetched_at: datetime
    valid_until: datetime
    stay: Stay
    occupancy: Occupancy
    quotes: list[Quote] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    updated_at: datetime | None = None
    stale: bool = False
    source_name: str = ""

    @property
    def prices_shown(self) -> bool:
        return any(q.per_night is not None or q.total is not None for q in self.quotes)

    def without_prices(self, reason: str) -> AvailabilityAnswer:
        for quote in self.quotes:
            quote.per_night = None
            quote.total = None
        if reason not in self.reasons:
            self.reasons.append(reason)
        return self


def unknown_answer(
    stay: Stay, occupancy: Occupancy, now: datetime, reasons: list[str], *, source_name: str = ""
) -> AvailabilityAnswer:
    return AvailabilityAnswer(
        status="unknown",
        source="none",
        fetched_at=now,
        valid_until=now,
        stay=stay,
        occupancy=occupancy,
        reasons=list(reasons),
        source_name=source_name,
    )


def status_of(quotes: list[Quote]) -> Status:
    if not quotes:
        return "unknown"
    if any(q.available for q in quotes):
        return "available"
    if any(q.restrictions and (q.units_left is None or q.units_left > 0) for q in quotes):
        return "restricted"
    return "unavailable"


@dataclass(frozen=True)
class ReservationRecord:
    """A reservation as a PMS reports it (surname is compared, never shown to a stranger)."""

    res_id: str
    confirmation_number: str
    arrival: date
    departure: date
    status: str
    adults: int
    children: int
    room_type: str
    surname: str
    first_name: str = ""
    email: str = ""
    phone: str = ""
    lang: str = ""
    source: str = ""
    rate_plan: str = ""


@dataclass(frozen=True)
class Capabilities:
    availability: bool = False
    prices: bool = False
    restrictions: bool = False
    reservations: bool = False
    arrivals: bool = False
    inquiry: bool = False


@dataclass
class DoctorReport:
    source: str
    ok: bool
    detail: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InquiryRequest:
    stay: Stay
    occupancy: Occupancy
    room_type: str
    first_name: str
    last_name: str
    email: str
    phone: str
    lang: str
    wish: str
    idempotency_key: str
