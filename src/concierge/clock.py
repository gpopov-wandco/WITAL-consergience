"""Injected clock and time-zone helpers.

Implements: C16

All business logic receives a :class:`Clock` instead of calling ``datetime.now``.
Instants are stored in UTC and shown in the tenant's local time through
:mod:`zoneinfo`. :class:`FakeClock` drives tests deterministically.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Clock(Protocol):
    """Source of the current time; ``now()`` always returns an aware UTC datetime."""

    def now(self) -> datetime: ...


class SystemClock:
    """The wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A controllable clock for tests and the demo time warp."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = ensure_utc(value)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime; naive values are rejected."""
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not allowed; store and pass UTC instants")
    return value.astimezone(UTC)


def zone(name: str) -> ZoneInfo:
    """Resolve an IANA zone name; the message names the field on failure."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown time zone {name!r}; use an IANA name such as Europe/Berlin"
        ) from exc


def to_local(value: datetime, tz: str) -> datetime:
    return ensure_utc(value).astimezone(zone(tz))


def local_date(value: datetime, tz: str) -> date:
    return to_local(value, tz).date()


def local_to_utc(day: date, at: time, tz: str) -> datetime:
    """Combine a local date and wall time into the UTC instant (DST aware).

    A wall time inside a spring-forward gap resolves with the pre-transition offset,
    which is the behaviour of :mod:`zoneinfo` with ``fold=0``; a repeated autumn time
    resolves to its first occurrence.
    """
    local = datetime.combine(day, at.replace(tzinfo=None), tzinfo=zone(tz))
    return local.astimezone(UTC)


def parse_hhmm(value: str) -> time:
    """Parse ``HH:MM`` (24-hour clock)."""
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"expected HH:MM, got {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {value!r}")
    return time(hour=hour, minute=minute)


def parse_range(value: str) -> tuple[time, time]:
    """Parse ``HH:MM-HH:MM``; the range may cross midnight (21:00-08:00)."""
    if "-" not in value:
        raise ValueError(f"expected HH:MM-HH:MM, got {value!r}")
    start, end = value.split("-", 1)
    return parse_hhmm(start), parse_hhmm(end)


def in_range(at: time, window: tuple[time, time]) -> bool:
    """Whether a wall time lies inside a window that may cross midnight (end exclusive)."""
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= at < end
    return at >= start or at < end


def format_local(value: datetime, tz: str, lang: str) -> str:
    """Local date and 24-hour time in the guest's language conventions."""
    local = to_local(value, tz)
    return f"{format_date(local.date(), lang)} {local.strftime('%H:%M')}"


def format_date(day: date, lang: str) -> str:
    """Local date formats: day-first with dots for de, ru, uk, tr; slashes for it; ISO-like for en."""
    if lang in {"de", "ru", "uk", "tr"}:
        return day.strftime("%d.%m.%Y")
    if lang == "it":
        return day.strftime("%d/%m/%Y")
    return day.strftime("%d %b %Y")
