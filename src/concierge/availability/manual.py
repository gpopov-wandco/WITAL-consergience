"""Manual inventory (F09): staff-maintained day grid, bulk set with audit, CSV import.

Implements: F09, F07, C16

The adapter reads ``room_type`` and ``day_inventory``. Data older than the tenant's
``stale_after_hours`` is served without prices and flagged stale, so the wording drops
to "as of {date}". ``ManualInventoryService`` is what the admin grid and the CSV
import call; every write is audited.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from concierge import audit
from concierge.availability.models import (
    AvailabilityAnswer,
    Capabilities,
    DoctorReport,
    Money,
    Occupancy,
    Quote,
    Restriction,
    RoomTypeInfo,
    Stay,
    status_of,
    unknown_answer,
)
from concierge.availability.port import BaseAdapter
from concierge.clock import Clock, ensure_utc
from concierge.db.models import DayInventory, RoomType
from concierge.db.session import Database

CSV_COLUMNS = (
    "date",
    "room_type",
    "units_available",
    "min_stay",
    "closed_arrival",
    "closed_departure",
    "price_eur",
)
_TRUE = {"1", "true", "ja", "yes", "x", "y", "wahr", "j"}
_FALSE = {"", "0", "false", "nein", "no", "falsch", "n", "-"}
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y")


class ManualAdapter(BaseAdapter):
    name = "manual"

    def __init__(
        self,
        db: Database,
        clock: Clock,
        tenant_id: str,
        *,
        stale_after_hours: int,
        currency: str,
        tax_inclusive: bool | None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.tenant_id = tenant_id
        self.stale_after = timedelta(hours=stale_after_hours)
        self.currency = currency
        self.tax_inclusive = tax_inclusive

    async def capabilities(self) -> Capabilities:
        return Capabilities(availability=True, prices=True, restrictions=True)

    async def list_room_types(self) -> list[RoomTypeInfo]:
        async with self.db.tenant_scope(self.tenant_id) as session:
            rows = (
                await session.execute(
                    select(RoomType)
                    .where(RoomType.tenant_id == self.tenant_id)
                    .order_by(RoomType.sort_order, RoomType.code)
                )
            ).scalars()
            return [_info(row) for row in rows]

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Sequence[str], promo: str = ""
    ) -> AvailabilityAnswer:
        now = self.clock.now()
        nights = list(stay.dates())
        wanted = {code.lower() for code in room_types if code}
        async with self.db.tenant_scope(self.tenant_id) as session:
            types = list(
                (
                    await session.execute(
                        select(RoomType)
                        .where(RoomType.tenant_id == self.tenant_id)
                        .order_by(RoomType.sort_order, RoomType.code)
                    )
                ).scalars()
            )
            if wanted:
                types = [t for t in types if t.code.lower() in wanted]
            if not types:
                return unknown_answer(
                    stay, occupancy, now, ["no_room_types"], source_name=self.name
                )
            rows = (
                await session.execute(
                    select(DayInventory).where(
                        DayInventory.tenant_id == self.tenant_id,
                        DayInventory.room_type_id.in_([t.id for t in types]),
                        DayInventory.date.in_([*nights, stay.departure]),
                    )
                )
            ).scalars()
            by_type: dict[str, dict[date, DayInventory]] = {}
            for row in rows:
                by_type.setdefault(row.room_type_id, {})[row.date] = row
        quotes: list[Quote] = []
        reasons: list[str] = []
        oldest: datetime | None = None
        for room in types:
            days = by_type.get(room.id, {})
            night_rows = [days.get(d) for d in nights]
            if any(r is None for r in night_rows):
                reasons.append(f"{room.code}:no_inventory")
                continue
            present = [r for r in night_rows if r is not None]
            updated = min(ensure_utc(r.updated_at) for r in present)
            oldest = updated if oldest is None or updated < oldest else oldest
            restrictions = self._restrictions(
                room, stay, occupancy, present, days.get(stay.departure)
            )
            units = min(r.units_available for r in present)
            prices = [r.price_cents for r in present]
            per_night = total = None
            if all(p is not None for p in prices):
                cents = [p for p in prices if p is not None]
                per_night = Money(min(cents), self.currency)
                total = Money(sum(cents), self.currency)
            quotes.append(
                Quote(
                    room_type=room.code,
                    room_name=_info(room).name("de"),
                    source="manual",
                    fetched_at=now,
                    valid_until=updated + self.stale_after,
                    per_night=per_night,
                    total=total,
                    units_left=units,
                    tax_inclusive=self.tax_inclusive,
                    restrictions=restrictions,
                    max_occupancy=room.max_occupancy,
                )
            )
        if not quotes:
            return unknown_answer(stay, occupancy, now, reasons, source_name=self.name)
        assert oldest is not None
        valid_until = oldest + self.stale_after
        answer = AvailabilityAnswer(
            status=status_of(quotes),
            source="manual",
            fetched_at=now,
            valid_until=valid_until,
            stay=stay,
            occupancy=occupancy,
            quotes=quotes,
            reasons=reasons,
            updated_at=oldest,
            stale=valid_until <= now,
            source_name=self.name,
        )
        if answer.stale:
            answer.without_prices("stale")
        return answer

    @staticmethod
    def _restrictions(
        room: RoomType,
        stay: Stay,
        occupancy: Occupancy,
        night_rows: list[DayInventory],
        departure_row: DayInventory | None,
    ) -> list[Restriction]:
        found: list[Restriction] = []
        first = night_rows[0]
        if occupancy.total > room.max_occupancy:
            rooms_needed = -(-occupancy.total // room.max_occupancy)
            found.append(Restriction("max_occupancy", str(room.max_occupancy), str(rooms_needed)))
        if first.min_stay > stay.nights:
            found.append(
                Restriction(
                    "min_stay",
                    str(first.min_stay),
                    (stay.arrival + timedelta(days=first.min_stay)).isoformat(),
                )
            )
        if first.closed_arrival:
            alternative = next(
                (r.date.isoformat() for r in night_rows[1:] if not r.closed_arrival), ""
            )
            found.append(Restriction("closed_arrival", stay.arrival.isoformat(), alternative))
        if departure_row is not None and departure_row.closed_departure:
            alternative = next(
                (r.date.isoformat() for r in reversed(night_rows[1:]) if not r.closed_departure),
                "",
            )
            found.append(Restriction("closed_departure", stay.departure.isoformat(), alternative))
        return found

    async def doctor(self) -> DoctorReport:
        async with self.db.tenant_scope(self.tenant_id) as session:
            types = list(
                (
                    await session.execute(
                        select(RoomType).where(RoomType.tenant_id == self.tenant_id)
                    )
                ).scalars()
            )
            newest = await session.scalar(
                select(DayInventory.updated_at)
                .where(DayInventory.tenant_id == self.tenant_id)
                .order_by(DayInventory.updated_at.desc())
            )
        warnings: list[str] = []
        if not types:
            warnings.append("no room types")
        if newest is None:
            warnings.append("no inventory rows")
        elif ensure_utc(newest) + self.stale_after <= self.clock.now():
            warnings.append("inventory older than the staleness threshold")
        return DoctorReport(
            source=self.name,
            ok=not warnings,
            detail=f"{len(types)} room types",
            warnings=warnings,
        )


def _info(row: RoomType) -> RoomTypeInfo:
    names = {str(k): str(v) for k, v in row.names.items()} if isinstance(row.names, dict) else {}
    return RoomTypeInfo(
        code=row.code,
        names=names,
        max_occupancy=row.max_occupancy,
        units_total=row.units_total,
        external_id=str(row.external_ids.get("manual", "")),
    )


# ------------------------------------------------------------- staff service


@dataclass
class DayCell:
    date: date
    units_available: int
    min_stay: int
    closed_arrival: bool
    closed_departure: bool
    price_cents: int | None
    updated_at: datetime


@dataclass
class CellUpdate:
    room_type: str
    date: date
    units_available: int | None = None
    min_stay: int | None = None
    closed_arrival: bool | None = None
    closed_departure: bool | None = None
    price_cents: int | None = None
    clear_price: bool = False


@dataclass
class CsvRow:
    line: int
    date: date
    room_type: str
    units_available: int | None
    min_stay: int | None
    closed_arrival: bool | None
    closed_departure: bool | None
    price_cents: int | None


@dataclass
class CsvReport:
    rows: list[CsvRow] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    applied: int = 0
    dry_run: bool = True

    @property
    def ok(self) -> bool:
        return not self.errors


class ManualInventoryService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def upsert_room_type(
        self,
        tenant_id: str,
        code: str,
        *,
        names: dict[str, str],
        units_total: int,
        max_occupancy: int,
        base_price_cents: int | None = None,
        currency: str = "EUR",
        sort_order: int = 0,
        actor: str,
    ) -> RoomType:
        code = code.strip().lower()
        if not re.fullmatch(r"[a-z0-9_\-]{1,32}", code):
            raise ValueError("room type codes use lower-case letters, digits, '-' and '_'")
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(RoomType).where(RoomType.tenant_id == tenant_id, RoomType.code == code)
            )
            if row is None:
                row = RoomType(tenant_id=tenant_id, code=code)
                session.add(row)
            row.names = dict(names)
            row.units_total = units_total
            row.max_occupancy = max_occupancy
            row.base_price_cents = base_price_cents
            row.currency = currency
            row.sort_order = sort_order
            audit.record(
                session,
                actor=actor,
                action="inventory.room_type",
                target=code,
                tenant_id=tenant_id,
                details={"units_total": units_total, "max_occupancy": max_occupancy},
            )
            await session.commit()
            await session.refresh(row)
            return row

    async def set_days(
        self,
        tenant_id: str,
        room_type_code: str,
        start: date,
        end: date,
        *,
        units_available: int | None = None,
        min_stay: int | None = None,
        closed_arrival: bool | None = None,
        closed_departure: bool | None = None,
        price_cents: int | None = None,
        clear_price: bool = False,
        actor: str,
    ) -> int:
        """Bulk set of the given fields for every day in [start, end] (inclusive)."""
        if end < start:
            raise ValueError("end before start")
        if (end - start).days > 400:
            raise ValueError("at most 400 days per bulk set")
        cells: list[CellUpdate] = []
        day = start
        while day <= end:
            cells.append(
                CellUpdate(
                    room_type=room_type_code.lower(),
                    date=day,
                    units_available=units_available,
                    min_stay=min_stay,
                    closed_arrival=closed_arrival,
                    closed_departure=closed_departure,
                    price_cents=price_cents,
                    clear_price=clear_price,
                )
            )
            day += timedelta(days=1)
        return await self.apply_cells(
            tenant_id,
            cells,
            actor=actor,
            action="inventory.set_days",
            details={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "units_available": units_available,
                "min_stay": min_stay,
                "closed_arrival": closed_arrival,
                "closed_departure": closed_departure,
                "price_cents": price_cents,
            },
        )

    async def apply_cells(
        self,
        tenant_id: str,
        cells: Sequence[CellUpdate],
        *,
        actor: str,
        action: str = "inventory.apply",
        details: dict[str, Any] | None = None,
    ) -> int:
        """Apply many cell updates in one transaction with one audit row."""
        if not cells:
            return 0
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            rooms = {
                row.code: row
                for row in (
                    await session.execute(select(RoomType).where(RoomType.tenant_id == tenant_id))
                ).scalars()
            }
            missing = sorted({c.room_type for c in cells if c.room_type not in rooms})
            if missing:
                raise ValueError(f"unknown room types: {', '.join(missing)}")
            dates = [c.date for c in cells]
            existing: dict[tuple[str, date], DayInventory] = {
                (row.room_type_id, row.date): row
                for row in (
                    await session.execute(
                        select(DayInventory).where(
                            DayInventory.tenant_id == tenant_id,
                            DayInventory.date >= min(dates),
                            DayInventory.date <= max(dates),
                        )
                    )
                ).scalars()
            }
            count = 0
            for cell in cells:
                room = rooms[cell.room_type]
                row = existing.get((room.id, cell.date))
                if row is None:
                    row = DayInventory(
                        tenant_id=tenant_id,
                        room_type_id=room.id,
                        date=cell.date,
                        units_available=room.units_total,
                        price_cents=room.base_price_cents,
                    )
                    session.add(row)
                    existing[(room.id, cell.date)] = row
                if cell.units_available is not None:
                    row.units_available = max(0, min(cell.units_available, 999))
                if cell.min_stay is not None:
                    row.min_stay = max(1, min(cell.min_stay, 60))
                if cell.closed_arrival is not None:
                    row.closed_arrival = cell.closed_arrival
                if cell.closed_departure is not None:
                    row.closed_departure = cell.closed_departure
                if cell.price_cents is not None:
                    row.price_cents = max(0, cell.price_cents)
                elif cell.clear_price:
                    row.price_cents = None
                row.updated_at = now
                row.updated_by = actor[:40]
                count += 1
            audit.record(
                session,
                actor=actor,
                action=action,
                target=",".join(sorted({c.room_type for c in cells}))[:120],
                tenant_id=tenant_id,
                details={"days": count, **(details or {})},
            )
            await session.commit()
            return count

    async def grid(self, tenant_id: str, year: int, month: int) -> dict[str, list[DayCell]]:
        first = date(year, month, 1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        async with self.db.tenant_scope(tenant_id) as session:
            types = list(
                (
                    await session.execute(
                        select(RoomType)
                        .where(RoomType.tenant_id == tenant_id)
                        .order_by(RoomType.sort_order, RoomType.code)
                    )
                ).scalars()
            )
            rows = (
                await session.execute(
                    select(DayInventory).where(
                        DayInventory.tenant_id == tenant_id,
                        DayInventory.date >= first,
                        DayInventory.date <= last,
                    )
                )
            ).scalars()
            by_type: dict[str, dict[date, DayInventory]] = {}
            for row in rows:
                by_type.setdefault(row.room_type_id, {})[row.date] = row
        out: dict[str, list[DayCell]] = {}
        for room in types:
            cells: list[DayCell] = []
            day = first
            while day <= last:
                cell_row = by_type.get(room.id, {}).get(day)
                if cell_row is not None:
                    cells.append(
                        DayCell(
                            day,
                            cell_row.units_available,
                            cell_row.min_stay,
                            cell_row.closed_arrival,
                            cell_row.closed_departure,
                            cell_row.price_cents,
                            ensure_utc(cell_row.updated_at),
                        )
                    )
                day += timedelta(days=1)
            out[room.code] = cells
        return out

    async def import_csv(
        self, tenant_id: str, text: str, *, dry_run: bool = True, actor: str
    ) -> CsvReport:
        """Parse and (unless dry run) apply a CSV; German Excel formats are tolerated."""
        report = CsvReport(dry_run=dry_run)
        parsed = parse_inventory_csv(text)
        report.errors.extend(parsed.errors)
        report.rows.extend(parsed.rows)
        if report.errors or dry_run:
            return report
        async with self.db.tenant_scope(tenant_id) as session:
            types = {
                row.code: row
                for row in (
                    await session.execute(select(RoomType).where(RoomType.tenant_id == tenant_id))
                ).scalars()
            }
        missing = sorted({r.room_type for r in report.rows if r.room_type not in types})
        if missing:
            report.errors.append((0, f"unknown room types: {', '.join(missing)}"))
            return report
        report.applied = await self.apply_cells(
            tenant_id,
            [
                CellUpdate(
                    room_type=row.room_type,
                    date=row.date,
                    units_available=row.units_available,
                    min_stay=row.min_stay,
                    closed_arrival=row.closed_arrival,
                    closed_departure=row.closed_departure,
                    price_cents=row.price_cents,
                )
                for row in report.rows
            ],
            actor=actor,
            action="inventory.csv_import",
            details={"rows": len(report.rows)},
        )
        return report


def parse_inventory_csv(text: str) -> CsvReport:
    report = CsvReport()
    text = text.lstrip("﻿")
    sample = text[:2000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header: list[str] | None = None
    for number, raw in enumerate(reader, start=1):
        if not raw or all(not cell.strip() for cell in raw):
            continue
        cells = [cell.strip() for cell in raw]
        if header is None:
            header = [cell.lower().replace(" ", "_") for cell in cells]
            unknown = [h for h in header if h not in CSV_COLUMNS]
            if "date" not in header or "room_type" not in header:
                report.errors.append((number, "header must contain date and room_type"))
                return report
            if unknown:
                report.errors.append((number, f"unknown columns: {', '.join(unknown)}"))
                return report
            continue
        record = dict(zip(header, cells, strict=False))
        try:
            report.rows.append(_csv_row(number, record))
        except ValueError as exc:
            report.errors.append((number, str(exc)))
    if header is None:
        report.errors.append((0, "empty file"))
    return report


def _csv_row(line: int, record: dict[str, str]) -> CsvRow:
    return CsvRow(
        line=line,
        date=parse_day(record.get("date", "")),
        room_type=_room_code(record.get("room_type", "")),
        units_available=_int_or_none(record.get("units_available"), "units_available", 0, 999),
        min_stay=_int_or_none(record.get("min_stay"), "min_stay", 1, 60),
        closed_arrival=_bool_or_none(record.get("closed_arrival"), "closed_arrival"),
        closed_departure=_bool_or_none(record.get("closed_departure"), "closed_departure"),
        price_cents=parse_price_cents(record.get("price_eur")),
    )


def _room_code(value: str) -> str:
    code = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_\-]{1,32}", code):
        raise ValueError(f"room_type {value!r} is not a valid code")
    return code


def parse_day(value: str) -> date:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise ValueError(f"date {value!r} is not YYYY-MM-DD or DD.MM.YYYY")


def _int_or_none(value: str | None, name: str, lo: int, hi: int) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        number = int(value.strip().replace(".", "").replace(",", "."))
    except ValueError:
        try:
            number = int(float(value.strip().replace(",", ".")))
        except ValueError as exc:
            raise ValueError(f"{name} {value!r} is not a whole number") from exc
    if not lo <= number <= hi:
        raise ValueError(f"{name} {number} outside {lo}..{hi}")
    return number


def _bool_or_none(value: str | None, name: str) -> bool | None:
    if value is None:
        return None
    key = value.strip().lower()
    if key in _TRUE:
        return True
    if key in _FALSE:
        return False
    raise ValueError(f"{name} {value!r} is not ja/nein, 1/0 or true/false")


def parse_price_cents(value: str | None) -> int | None:
    """'120', '120,50', '1.250,00', '1,250.00', '€ 99' and '99 EUR' all parse."""
    if value is None:
        return None
    text = value.strip().replace("€", "").replace("EUR", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else text.replace(",", "")
    elif text.count(".") > 1 or (text.count(".") == 1 and len(text.rsplit(".", 1)[1]) == 3):
        text = text.replace(".", "")
    try:
        amount = float(text)
    except ValueError as exc:
        raise ValueError(f"price_eur {value!r} is not a number") from exc
    if amount < 0 or amount > 100_000:
        raise ValueError(f"price_eur {value!r} outside 0..100000")
    return round(amount * 100)


def cells_to_rows(cells: dict[str, list[DayCell]]) -> list[dict[str, Any]]:
    """Flat rows for templates and exports."""
    out: list[dict[str, Any]] = []
    for code, days in cells.items():
        for cell in days:
            out.append(
                {
                    "room_type": code,
                    "date": cell.date.isoformat(),
                    "units_available": cell.units_available,
                    "min_stay": cell.min_stay,
                    "closed_arrival": cell.closed_arrival,
                    "closed_departure": cell.closed_departure,
                    "price_cents": cell.price_cents,
                }
            )
    return out
