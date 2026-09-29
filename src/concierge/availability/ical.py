"""iCal feeds as an availability source (F09): one feed per physical unit.

Implements: F09, F07, C3, F31

Availability for a night is ``units_total`` minus the units whose feed has an event
overlapping that night. ``DTEND`` is exclusive, cancelled and transparent events are
ignored, an ``RRULE`` is not expanded (doctor warning). Feed URLs are tenant secrets
fetched through the SSRF guard with conditional GET. No prices, no stay rules; the
policy wording says "to be confirmed by the reception".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import icalendar

from concierge.availability.models import (
    AvailabilityAnswer,
    Capabilities,
    DoctorReport,
    Occupancy,
    Quote,
    Restriction,
    RoomTypeInfo,
    SourceUnavailable,
    Stay,
    status_of,
    unknown_answer,
)
from concierge.availability.port import BaseAdapter
from concierge.clock import Clock, local_date, zone
from concierge.log import get_logger
from concierge.security.ssrf import Fetcher, SSRFError

log = get_logger(__name__)


@dataclass
class FeedCache:
    etag: str | None = None
    last_modified: str | None = None
    body: bytes = b""
    fetched_at: datetime | None = None


@dataclass
class ParsedFeed:
    busy_nights: set[date] = field(default_factory=set)
    events: int = 0
    warnings: list[str] = field(default_factory=list)


class ICalAdapter(BaseAdapter):
    name = "ical"

    def __init__(
        self,
        fetcher: Fetcher,
        clock: Clock,
        *,
        feeds: dict[str, str],
        units: dict[str, list[str]],
        room_types: Sequence[RoomTypeInfo],
        tz: str,
        max_age_min: int = 15,
        cache: dict[str, FeedCache] | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.clock = clock
        self.feeds = dict(feeds)
        self.units = {code: list(names) for code, names in units.items()}
        self.room_types = list(room_types)
        self.tz = tz
        self.max_age = timedelta(minutes=max_age_min)
        self._cache: dict[str, FeedCache] = cache if cache is not None else {}

    async def capabilities(self) -> Capabilities:
        return Capabilities(availability=True)

    async def list_room_types(self) -> list[RoomTypeInfo]:
        return list(self.room_types)

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Sequence[str], promo: str = ""
    ) -> AvailabilityAnswer:
        now = self.clock.now()
        wanted = {code.lower() for code in room_types if code}
        quotes: list[Quote] = []
        reasons: list[str] = []
        oldest_fetch: datetime | None = None
        for info in self.room_types:
            if wanted and info.code.lower() not in wanted:
                continue
            unit_names = self.units.get(info.code, [])
            if not unit_names:
                reasons.append(f"{info.code}:no_units")
                continue
            busy_per_night: dict[date, int] = dict.fromkeys(stay.dates(), 0)
            for unit in unit_names:
                url = self.feeds.get(unit)
                if url is None:
                    raise SourceUnavailable(f"feed for unit {unit} is not configured")
                parsed, fetched_at = await self._load(url, stay)
                oldest_fetch = (
                    fetched_at
                    if oldest_fetch is None or fetched_at < oldest_fetch
                    else oldest_fetch
                )
                for night in busy_per_night:
                    if night in parsed.busy_nights:
                        busy_per_night[night] += 1
            units_left = min(len(unit_names) - busy for busy in busy_per_night.values())
            restrictions: list[Restriction] = []
            if occupancy.total > info.max_occupancy:
                rooms_needed = -(-occupancy.total // info.max_occupancy)
                restrictions.append(
                    Restriction("max_occupancy", str(info.max_occupancy), str(rooms_needed))
                )
            quotes.append(
                Quote(
                    room_type=info.code,
                    room_name=info.name("de"),
                    source="ical",
                    fetched_at=now,
                    valid_until=now + self.max_age,
                    units_left=max(0, units_left),
                    restrictions=restrictions,
                    max_occupancy=info.max_occupancy,
                )
            )
        if not quotes:
            return unknown_answer(stay, occupancy, now, reasons, source_name=self.name)
        updated = oldest_fetch or now
        return AvailabilityAnswer(
            status=status_of(quotes),
            source="ical",
            fetched_at=now,
            valid_until=updated + self.max_age,
            stay=stay,
            occupancy=occupancy,
            quotes=quotes,
            reasons=reasons,
            updated_at=updated,
            source_name=self.name,
        )

    async def _load(self, url: str, stay: Stay) -> tuple[ParsedFeed, datetime]:
        now = self.clock.now()
        cached = self._cache.get(url)
        if cached and cached.fetched_at and now - cached.fetched_at < self.max_age:
            return parse_feed(cached.body, stay, self.tz), cached.fetched_at
        headers: dict[str, str] = {"Accept": "text/calendar, */*;q=0.5"}
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        try:
            result = await self.fetcher.fetch(url, headers=headers)
        except SSRFError as exc:
            raise SourceUnavailable(f"feed blocked: {exc}") from exc
        except (OSError, TimeoutError) as exc:
            raise SourceUnavailable(f"feed unreachable: {type(exc).__name__}") from exc
        if result.status == 304 and cached is not None:
            cached.fetched_at = now
            return parse_feed(cached.body, stay, self.tz), now
        if result.status != 200:
            raise SourceUnavailable(f"feed answered {result.status}")
        self._cache[url] = FeedCache(
            etag=result.etag, last_modified=result.last_modified, body=result.body, fetched_at=now
        )
        return parse_feed(result.body, stay, self.tz), now

    async def doctor(self) -> DoctorReport:
        warnings: list[str] = []
        events = 0
        probe = Stay(self.clock.now().date(), self.clock.now().date() + timedelta(days=365))
        for unit, url in self.feeds.items():
            try:
                parsed, _ = await self._load(url, probe)
            except SourceUnavailable as exc:
                warnings.append(f"{unit}: {exc}")
                continue
            events += parsed.events
            warnings.extend(f"{unit}: {w}" for w in parsed.warnings)
        configured = {u for names in self.units.values() for u in names}
        for missing in sorted(configured - set(self.feeds)):
            warnings.append(f"{missing}: no feed URL secret")
        return DoctorReport(
            source=self.name,
            ok=not any("no feed" in w or "unreachable" in w or "blocked" in w for w in warnings),
            detail=f"{len(self.feeds)} feeds, {events} events",
            warnings=warnings,
        )


def parse_feed(body: bytes, stay: Stay, tz: str) -> ParsedFeed:
    """Nights of ``stay`` that an event in the feed makes busy."""
    parsed = ParsedFeed()
    try:
        calendar = icalendar.Calendar.from_ical(body)
    except (ValueError, TypeError) as exc:
        raise SourceUnavailable(f"feed is not valid iCalendar: {type(exc).__name__}") from exc
    stay_nights = set(stay.dates())
    for event in calendar.walk("VEVENT"):
        parsed.events += 1
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if str(event.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        if event.get("RRULE") is not None:
            parsed.warnings.append(
                f"event {event.get('UID', '?')} has an RRULE; only the first occurrence counts"
            )
        start_raw = event.get("DTSTART")
        if start_raw is None:
            parsed.warnings.append(f"event {event.get('UID', '?')} without DTSTART ignored")
            continue
        start = _to_local_date(start_raw.dt, tz)
        end_raw = event.get("DTEND")
        if end_raw is not None:
            end = _to_local_date(end_raw.dt, tz)
            if isinstance(end_raw.dt, datetime) and end == start:
                end = start + timedelta(days=1)
        else:
            duration = event.get("DURATION")
            if duration is not None and isinstance(duration.dt, timedelta):
                end = _to_local_date(start_raw.dt + duration.dt, tz)
                end = max(end, start + timedelta(days=1))
            else:
                end = start + timedelta(days=1)
        night = start
        while night < end:
            if night in stay_nights:
                parsed.busy_nights.add(night)
            night += timedelta(days=1)
    return parsed


def _to_local_date(value: Any, tz: str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=zone(tz)).date()
        return local_date(value, tz)
    if isinstance(value, date):
        return value
    raise SourceUnavailable("unsupported date value in feed")
