"""Reservation intake and verification (F12, F02).

Implements: F12, F02, F11, C3, C9, C15, C16

Reservations are upserted by ``res_id`` per tenant. Names, phones and e-mails are
stored encrypted with blind indexes (the surname index is what verification compares).
Phones are normalised to E.164. Changed dates cancel the pending journey jobs of the
reservation so the journey engine re-plans; a cancellation cancels pending sends. A
WhatsApp opt-in counts only with recorded evidence (source and time), otherwise no
journey message is ever sent.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select

from concierge import audit
from concierge.availability.models import ReservationRecord
from concierge.availability.port import AvailabilityPort
from concierge.clock import Clock, ensure_utc
from concierge.db.models import Reservation
from concierge.db.session import Database
from concierge.jobs.service import JobService
from concierge.log import get_logger
from concierge.security.crypto import Keyring

log = get_logger(__name__)

ChangeHook = Callable[[str, Reservation, str], Awaitable[None]]
CSV_COLUMNS = (
    "res_id",
    "guest_name",
    "phone",
    "email",
    "lang",
    "arrival",
    "departure",
    "room_type",
    "rate_plan",
    "source",
    "whatsapp_optin",
    "optin_source",
    "optin_at",
)
_TRUE = {"1", "true", "ja", "yes", "x", "y", "wahr", "j"}
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y")


def to_e164(raw: str | None, region: str = "DE") -> str | None:
    """E.164 or None: '+49 151 1234567', '0151 1234567', '0049...' and bare wa_ids all parse."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    candidates: list[str] = []
    if text.startswith("+"):
        candidates.append(text)
    elif text.startswith("00"):
        candidates.append("+" + digits[2:])
    elif digits and not text.startswith("0") and len(digits) >= 10:
        candidates.append("+" + digits)
    candidates.append(text)
    for candidate in candidates:
        try:
            number = phonenumbers.parse(candidate, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(number):
            return str(phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164))
    return None


class ReservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    res_id: str = Field(min_length=1, max_length=64)
    guest_name: str = Field(default="", max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    lang: str = Field(default="de", pattern=r"^[a-z]{2}$")
    arrival: date
    departure: date
    room_type: str | None = Field(default=None, max_length=64)
    rate_plan: str | None = Field(default=None, max_length=64)
    source: str = Field(default="manual", max_length=32)
    status: Literal["booked", "cancelled", "checked_in", "checked_out"] = "booked"
    adults: int = Field(default=1, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    whatsapp_optin: bool = False
    optin_source: str | None = Field(default=None, max_length=64)
    optin_at: datetime | None = None
    optin_wording_version: str | None = Field(default=None, max_length=32)
    marketing_optin: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("res_id")
    @classmethod
    def _res_id(cls, value: str) -> str:
        if any(ch.isspace() for ch in value):
            raise ValueError("res_id must not contain whitespace")
        return value

    @model_validator(mode="after")
    def _dates(self) -> ReservationInput:
        if self.departure <= self.arrival:
            raise ValueError("departure must be after arrival")
        if (self.departure - self.arrival).days > 365:
            raise ValueError("stays longer than a year are not accepted")
        return self

    @property
    def optin_recorded(self) -> bool:
        return self.whatsapp_optin and bool(self.optin_source) and self.optin_at is not None


@dataclass
class UpsertResult:
    reservation: Reservation
    created: bool
    changed: list[str] = field(default_factory=list)
    dates_changed: bool = False
    cancelled: bool = False
    cancelled_jobs: int = 0

    @property
    def outcome(self) -> str:
        if self.created:
            return "created"
        if self.cancelled:
            return "cancelled"
        return "updated" if self.changed else "unchanged"


@dataclass
class CsvReservationReport:
    rows: list[ReservationInput] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    applied: int = 0
    created: int = 0
    dry_run: bool = True

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class SyncReport:
    arrivals: int = 0
    departures: int = 0
    created: int = 0
    updated: int = 0


class ReservationService:
    def __init__(
        self,
        db: Database,
        keyring: Keyring,
        clock: Clock,
        jobs: JobService,
        *,
        phone_region: str = "DE",
    ) -> None:
        self.db = db
        self.keyring = keyring
        self.clock = clock
        self.jobs = jobs
        self.phone_region = phone_region
        self.hooks: list[ChangeHook] = []

    def on_change(self, hook: ChangeHook) -> None:
        """Register a listener called with (tenant_id, reservation, outcome) after commits."""
        self.hooks.append(hook)

    @staticmethod
    def job_prefix(tenant_id: str, res_id: str) -> str:
        return f"journey:{tenant_id}:{res_id}:"

    # ---------------------------------------------------------------- upsert
    async def upsert(
        self,
        tenant_id: str,
        data: ReservationInput,
        *,
        actor: str,
        phone_region: str | None = None,
    ) -> UpsertResult:
        now = self.clock.now()
        region = phone_region or self.phone_region
        phone = to_e164(data.phone, region)
        meta = dict(data.meta)
        if data.phone and phone is None:
            meta["phone_invalid"] = True
        optin = data.optin_recorded
        if data.whatsapp_optin and not optin:
            meta["optin_unrecorded"] = True
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(Reservation).where(
                    Reservation.tenant_id == tenant_id, Reservation.res_id == data.res_id
                )
            )
            created = row is None
            if row is None:
                row = Reservation(tenant_id=tenant_id, res_id=data.res_id)
                session.add(row)
            changed: list[str] = []
            dates_changed = not created and (
                row.arrival != data.arrival or row.departure != data.departure
            )
            for column, value in (
                ("arrival", data.arrival),
                ("departure", data.departure),
                ("room_type", data.room_type),
                ("rate_plan", data.rate_plan),
                ("adults", data.adults),
                ("children", data.children),
                ("lang", data.lang),
                ("source", data.source),
                ("whatsapp_optin", optin),
                ("optin_source", data.optin_source if optin else None),
                ("optin_at", ensure_utc(data.optin_at) if optin and data.optin_at else None),
                ("optin_wording_version", data.optin_wording_version if optin else None),
                ("marketing_optin", data.marketing_optin),
            ):
                if getattr(row, column) != value:
                    setattr(row, column, value)
                    changed.append(column)
            if data.marketing_optin and row.marketing_optin_at is None:
                row.marketing_optin_at = now
            if not data.marketing_optin:
                row.marketing_optin_at = None
            self._set_encrypted(
                row, tenant_id, "name", data.guest_name or None, changed, surname=True
            )
            self._set_encrypted(row, tenant_id, "phone", phone, changed)
            self._set_encrypted(
                row, tenant_id, "email", (data.email or "").lower() or None, changed
            )
            cancelled = False
            new_status = data.status
            if new_status == "cancelled" and row.status != "cancelled":
                cancelled = True
                row.cancelled_at = now
            elif new_status != "cancelled":
                row.cancelled_at = None
            if row.status != new_status:
                row.status = new_status
                changed.append("status")
            current_meta = row.meta or {}
            merged = {**current_meta, **meta}
            if created or merged != current_meta:
                row.meta = merged
            if created or changed:
                audit.record(
                    session,
                    actor=actor,
                    action="reservation.create" if created else "reservation.update",
                    target=data.res_id,
                    tenant_id=tenant_id,
                    details={"changed": changed, "source": data.source},
                )
            await session.commit()
            await session.refresh(row)
        cancelled_jobs = 0
        if dates_changed or cancelled:
            cancelled_jobs = await self.jobs.cancel(
                dedupe_prefix=self.job_prefix(tenant_id, row.res_id)
            )
        result = UpsertResult(
            reservation=row,
            created=created,
            changed=changed,
            dates_changed=dates_changed,
            cancelled=cancelled,
            cancelled_jobs=cancelled_jobs,
        )
        for hook in self.hooks:
            await hook(tenant_id, row, result.outcome)
        return result

    def _set_encrypted(
        self,
        row: Reservation,
        tenant_id: str,
        kind: str,
        value: str | None,
        changed: list[str],
        *,
        surname: bool = False,
    ) -> None:
        enc_attr, bidx_attr = f"{kind}_enc", f"{kind}_bidx"
        current = getattr(row, enc_attr)
        current_plain = (
            self.keyring.decrypt_str(current, tenant_id, kind) if current is not None else None
        )
        if current_plain == value:
            return
        if value is None:
            setattr(row, enc_attr, None)
            setattr(row, bidx_attr, None)
        else:
            setattr(row, enc_attr, self.keyring.encrypt_str(value, tenant_id, kind))
            indexed = value.split()[-1] if surname and value.split() else value
            setattr(row, bidx_attr, self.keyring.blind_index(indexed, tenant_id, kind))
        changed.append(kind)

    # ---------------------------------------------------------------- reads
    def decrypt(self, row: Reservation) -> dict[str, str]:
        out: dict[str, str] = {}
        for kind in ("name", "phone", "email"):
            blob = getattr(row, f"{kind}_enc")
            if blob is not None:
                out[kind] = self.keyring.decrypt_str(blob, row.tenant_id, kind)
        return out

    async def get(self, tenant_id: str, reservation_id: str) -> Reservation | None:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(Reservation, reservation_id)
            return row if row is not None and row.tenant_id == tenant_id else None

    async def by_res_id(self, tenant_id: str, res_id: str) -> Reservation | None:
        async with self.db.tenant_scope(tenant_id) as session:
            return await session.scalar(
                select(Reservation).where(
                    Reservation.tenant_id == tenant_id,
                    func.lower(Reservation.res_id) == res_id.strip().lower(),
                )
            )

    async def find_by_phone(self, tenant_id: str, phone: str) -> list[Reservation]:
        normalised = to_e164(phone, self.phone_region) or phone
        bidx = self.keyring.blind_index(normalised, tenant_id, "phone")
        async with self.db.tenant_scope(tenant_id) as session:
            return list(
                (
                    await session.execute(
                        select(Reservation)
                        .where(Reservation.tenant_id == tenant_id, Reservation.phone_bidx == bidx)
                        .order_by(Reservation.arrival)
                    )
                ).scalars()
            )

    async def between(
        self,
        tenant_id: str,
        start: date,
        end: date,
        *,
        field_name: Literal["arrival", "departure"] = "arrival",
    ) -> list[Reservation]:
        column = Reservation.arrival if field_name == "arrival" else Reservation.departure
        async with self.db.tenant_scope(tenant_id) as session:
            return list(
                (
                    await session.execute(
                        select(Reservation)
                        .where(
                            Reservation.tenant_id == tenant_id,
                            column >= start,
                            column <= end,
                            Reservation.status != "cancelled",
                        )
                        .order_by(column, Reservation.res_id)
                    )
                ).scalars()
            )

    async def verify(
        self, tenant_id: str, reference: str, surname: str, arrival: date
    ) -> dict[str, Any] | None:
        """The verification tool: reference, surname (blind index) and arrival must match."""
        row = await self.by_res_id(tenant_id, reference)
        if row is None or row.status == "cancelled":
            return None
        if row.arrival != arrival:
            return None
        expected = self.keyring.blind_index(surname.strip().split()[-1], tenant_id, "name")
        if row.name_bidx != expected:
            return None
        return self.tool_view(row)

    @staticmethod
    def tool_view(row: Reservation) -> dict[str, Any]:
        return {
            "reservation_id": row.id,
            "res_id": row.res_id,
            "confirmation_number": row.res_id if row.status != "cancelled" else "",
            "arrival": row.arrival.isoformat(),
            "departure": row.arrival.isoformat()
            if row.departure is None
            else row.departure.isoformat(),
            "room_type": row.room_type or "",
            "adults": row.adults,
            "children": row.children,
            "status": row.status,
            "eta": row.eta.strftime("%H:%M") if row.eta else "",
        }

    @staticmethod
    def record_view(record: ReservationRecord) -> dict[str, Any]:
        return {
            "res_id": record.res_id,
            "confirmation_number": record.confirmation_number,
            "arrival": record.arrival.isoformat(),
            "departure": record.departure.isoformat(),
            "room_type": record.room_type,
            "adults": record.adults,
            "children": record.children,
            "status": record.status,
            "source": record.source,
        }

    async def set_eta(
        self, tenant_id: str, reservation_id: str, eta: time, *, actor: str
    ) -> Reservation:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(Reservation, reservation_id)
            if row is None or row.tenant_id != tenant_id:
                raise ValueError("unknown reservation")
            row.eta = eta
            row.eta_set_at = self.clock.now()
            audit.record(
                session,
                actor=actor,
                action="reservation.eta",
                target=row.res_id,
                tenant_id=tenant_id,
                details={"eta": eta.strftime("%H:%M")},
            )
            await session.commit()
            await session.refresh(row)
            return row

    async def link_guest(self, tenant_id: str, reservation_id: str, guest_id: str) -> None:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(Reservation, reservation_id)
            if row is not None and row.tenant_id == tenant_id and row.guest_id != guest_id:
                row.guest_id = guest_id
                await session.commit()

    # ------------------------------------------------------------------ csv
    async def import_csv(
        self, tenant_id: str, text: str, *, dry_run: bool = True, actor: str
    ) -> CsvReservationReport:
        report = parse_reservation_csv(text)
        report.dry_run = dry_run
        if report.errors or dry_run:
            return report
        for item in report.rows:
            result = await self.upsert(tenant_id, item, actor=actor)
            report.applied += 1
            report.created += int(result.created)
        return report

    # ------------------------------------------------------------------ pms
    async def sync_from_pms(
        self, tenant_id: str, adapter: AvailabilityPort, *, days_ahead: int = 14
    ) -> SyncReport:
        today = self.clock.now().date()
        end = today + timedelta(days=days_ahead)
        report = SyncReport()
        arrivals = await adapter.arrivals(today, end)
        departures = await adapter.departures(today - timedelta(days=1), end)
        report.arrivals, report.departures = len(arrivals), len(departures)
        seen: set[str] = set()
        for record in [*arrivals, *departures]:
            if record.res_id in seen or not record.res_id:
                continue
            seen.add(record.res_id)
            data = ReservationInput(
                res_id=record.res_id,
                guest_name=f"{record.first_name} {record.surname}".strip(),
                phone=record.phone or None,
                email=record.email or None,
                lang=record.lang[:2].lower() if record.lang else "de",
                arrival=record.arrival,
                departure=record.departure,
                room_type=record.room_type or None,
                rate_plan=record.rate_plan or None,
                source=f"pms:{adapter.name}",
                status="cancelled" if record.status in {"cancelled", "canceled"} else "booked",
                adults=max(1, record.adults),
                children=max(0, record.children),
            )
            result = await self.upsert(tenant_id, data, actor=f"pms:{adapter.name}")
            if result.created:
                report.created += 1
            elif result.changed:
                report.updated += 1
        return report


def parse_reservation_csv(text: str) -> CsvReservationReport:
    report = CsvReservationReport()
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
            if "res_id" not in header or "arrival" not in header or "departure" not in header:
                report.errors.append((number, "header must contain res_id, arrival and departure"))
                return report
            if unknown:
                report.errors.append((number, f"unknown columns: {', '.join(unknown)}"))
                return report
            continue
        record = dict(zip(header, cells, strict=False))
        try:
            report.rows.append(_csv_reservation(record))
        except ValidationError as exc:
            report.errors.append((number, _validation_message(exc)))
        except ValueError as exc:
            report.errors.append((number, str(exc)[:200]))
    if header is None:
        report.errors.append((0, "empty file"))
    return report


def _csv_reservation(record: dict[str, str]) -> ReservationInput:
    optin_raw = record.get("whatsapp_optin", "").strip().lower()
    optin_at = _parse_datetime(record.get("optin_at", ""))
    return ReservationInput(
        res_id=record.get("res_id", ""),
        guest_name=record.get("guest_name", ""),
        phone=record.get("phone") or None,
        email=record.get("email") or None,
        lang=(record.get("lang") or "de").lower()[:2],
        arrival=_parse_day(record.get("arrival", "")),
        departure=_parse_day(record.get("departure", "")),
        room_type=record.get("room_type") or None,
        rate_plan=record.get("rate_plan") or None,
        source=record.get("source") or "csv",
        whatsapp_optin=optin_raw in _TRUE,
        optin_source=record.get("optin_source") or None,
        optin_at=optin_at,
    )


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error.get("loc", ())) or "row"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)[:200]


def _parse_day(value: str) -> date:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise ValueError(f"date {value!r} is not YYYY-MM-DD or DD.MM.YYYY")


def _parse_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(value, fmt)  # noqa: DTZ007
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"optin_at {value!r} is not an ISO date-time") from None
    return ensure_utc(parsed)
