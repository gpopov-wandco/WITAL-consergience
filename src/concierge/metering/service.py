"""Metering (F24): dated prices, the monthly free allowance, EUR conversion, reports.

Implements: F24, C16

Usage rows are append-only and priced when written: WhatsApp from the status
webhook's own pricing fields against the dated ``price_table`` (the free allowance
counts billable messages per business number and calendar month in the tenant's time
zone), the model from the dated ``model_price`` table in USD converted with the dated
USD-EUR row, voice per call in the post-call pipeline. Nightly reconciliation appends
a correction row when the tables changed after the fact. Nothing here charges or
enforces anything.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select

from concierge.clock import Clock, local_to_utc, to_local
from concierge.db.models import FxRate, ModelPrice, PriceTable, UsageEvent
from concierge.db.session import Database
from concierge.ids import new_id
from concierge.llm.port import Usage
from concierge.metering.prices import (
    DEFAULT_MESSAGE_PRICES,
    DEFAULT_USD_EUR,
    MessagePriceRow,
    ModelPriceRow,
    default_model_price,
    model_cost_usd,
    to_micro,
)
from concierge.reports.pdf import simple_pdf
from concierge.tenants.profile import TenantProfile

MICRO = 1_000_000
CORRECTION = "correction"


@dataclass
class ReportRow:
    kind: str
    category: str
    events: int
    quantity: int
    unit: str
    cost_micro_eur: int

    @property
    def cost_eur(self) -> float:
        return self.cost_micro_eur / MICRO


@dataclass
class UsageReport:
    tenant_id: str
    tenant_name: str
    year: int
    month: int
    generated_at: datetime
    rows: list[ReportRow] = field(default_factory=list)

    @property
    def total_micro_eur(self) -> int:
        return sum(r.cost_micro_eur for r in self.rows)

    @property
    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def to_json(self) -> str:
        return json.dumps(
            {
                "tenant_id": self.tenant_id,
                "tenant": self.tenant_name,
                "month": self.label,
                "generated_at": self.generated_at.isoformat(),
                "rows": [dict(asdict(r), cost_eur=r.cost_eur) for r in self.rows],
                "total_eur": self.total_micro_eur / MICRO,
            },
            ensure_ascii=False,
            indent=2,
        )

    def to_csv(self) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
        writer.writerow(["month", "kind", "category", "events", "quantity", "unit", "cost_eur"])
        for row in self.rows:
            writer.writerow(
                [
                    self.label,
                    row.kind,
                    row.category,
                    row.events,
                    row.quantity,
                    row.unit,
                    f"{row.cost_eur:.4f}",
                ]
            )
        writer.writerow(
            [self.label, "total", "", "", "", "", f"{self.total_micro_eur / MICRO:.4f}"]
        )
        return buffer.getvalue()

    def to_pdf(self) -> bytes:
        table = [["Kind", "Category", "Events", "Quantity", "EUR"]]
        for row in self.rows:
            table.append(
                [
                    row.kind,
                    row.category,
                    str(row.events),
                    f"{row.quantity} {row.unit}",
                    f"{row.cost_eur:.4f}",
                ]
            )
        table.append(["total", "", "", "", f"{self.total_micro_eur / MICRO:.4f}"])
        return simple_pdf(
            f"Usage {self.label}: {self.tenant_name}",
            [("Metering", ["Costs from dated price tables; no charge is made by this report."])],
            subtitle=f"Generated {self.generated_at.isoformat(timespec='minutes')}",
            table=table,
            footer="WITALCO Guest Concierge usage report (F24). Re-check Meta's rate card before every price change.",
        )


class MeteringService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    # --------------------------------------------------------------- prices
    async def message_price(
        self, tenant_id: str, region: str, category: str, on: date
    ) -> MessagePriceRow | None:
        region = (region or "DE").upper()
        async with self.db.scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(PriceTable)
                        .where(
                            PriceTable.region == region,
                            PriceTable.category == category,
                            PriceTable.effective_from <= on,
                            (PriceTable.tenant_id == tenant_id) | (PriceTable.tenant_id.is_(None)),
                        )
                        .order_by(PriceTable.effective_from.desc())
                    )
                ).scalars()
            )
        if rows:
            tenant_rows = [r for r in rows if r.tenant_id == tenant_id]
            chosen = tenant_rows[0] if tenant_rows else rows[0]
            return MessagePriceRow(
                chosen.region,
                chosen.category,
                chosen.effective_from,
                chosen.price_micro_eur / MICRO,
                chosen.free_allowance_per_month,
                chosen.source_url,
                source="tenant" if tenant_rows else "global",
            )
        defaults = [
            r
            for r in DEFAULT_MESSAGE_PRICES
            if r.region == region and r.category == category and r.effective_from <= on
        ]
        if not defaults:
            return None
        return max(defaults, key=lambda r: r.effective_from)

    async def usd_eur(self, on: date) -> float:
        async with self.db.scope() as session:
            row = await session.scalar(
                select(FxRate)
                .where(FxRate.base == "USD", FxRate.quote == "EUR", FxRate.effective_from <= on)
                .order_by(FxRate.effective_from.desc())
                .limit(1)
            )
        if row is not None:
            return row.rate_micro / MICRO
        return DEFAULT_USD_EUR[1]

    async def model_price(self, model: str, on: date) -> ModelPriceRow | None:
        async with self.db.scope() as session:
            row = await session.scalar(
                select(ModelPrice)
                .where(ModelPrice.model == model, ModelPrice.effective_from <= on)
                .order_by(ModelPrice.effective_from.desc())
                .limit(1)
            )
        if row is not None:
            return ModelPriceRow(
                row.model,
                row.effective_from,
                row.input_micro_usd_per_mtok / MICRO,
                row.output_micro_usd_per_mtok / MICRO,
                row.cache_write_micro_usd_per_mtok / MICRO,
                row.cache_read_micro_usd_per_mtok / MICRO,
                row.source_url,
            )
        return default_model_price(model)

    async def newest_price_dates(self) -> tuple[date | None, date | None]:
        """Newest effective dates of the message and model tables (stale-table alert)."""
        async with self.db.scope() as session:
            message = await session.scalar(select(func.max(PriceTable.effective_from)))
            model = await session.scalar(select(func.max(ModelPrice.effective_from)))
        message_default = max(r.effective_from for r in DEFAULT_MESSAGE_PRICES)
        model_default = max(r.effective_from for r in _default_model_rows())
        return (
            max(message or message_default, message_default),
            max(model or model_default, model_default),
        )

    # ---------------------------------------------------------------- write
    @staticmethod
    def month_start(tz: str, instant: datetime) -> datetime:
        local = to_local(instant, tz)
        return local_to_utc(local.date().replace(day=1), time(0, 0), tz)

    @staticmethod
    def month_end(tz: str, instant: datetime) -> datetime:
        local = to_local(instant, tz)
        first = local.date().replace(day=1)
        following = (first + timedelta(days=32)).replace(day=1)
        return local_to_utc(following, time(0, 0), tz)

    async def record_whatsapp(
        self, tenant_id: str, profile: TenantProfile, details: dict[str, Any]
    ) -> UsageEvent | None:
        """One priced usage event per billable status webhook (idempotent by ref)."""
        ref = str(details.get("ref", ""))
        occurred: datetime = details.get("occurred_at") or self.clock.now()
        category = str(details.get("category", ""))
        billable = bool(details.get("billable"))
        region = str(details.get("region") or profile.contacts.phone_region)
        cost = 0
        price: MessagePriceRow | None = None
        used = 0
        async with self.db.tenant_scope(tenant_id) as session:
            existing = await session.scalar(
                select(UsageEvent).where(UsageEvent.tenant_id == tenant_id, UsageEvent.ref == ref)
            )
            if existing is not None:
                return None
            if billable and category:
                price = await self.message_price(tenant_id, region, category, occurred.date())
                if price is not None:
                    start = self.month_start(profile.timezone, occurred)
                    used = int(
                        await session.scalar(
                            select(func.count())
                            .select_from(UsageEvent)
                            .where(
                                UsageEvent.tenant_id == tenant_id,
                                UsageEvent.kind == "whatsapp",
                                UsageEvent.category == category,
                                UsageEvent.occurred_at >= start,
                                UsageEvent.occurred_at < occurred,
                                UsageEvent.quantity == 1,
                            )
                        )
                        or 0
                    )
                    cost = 0 if used < price.free_allowance_per_month else to_micro(price.price_eur)
            event = UsageEvent(
                tenant_id=tenant_id,
                kind="whatsapp",
                category=category,
                quantity=1,
                unit="message",
                cost_micro_eur=cost,
                occurred_at=occurred,
                ref=ref,
                meta={
                    "billable": billable,
                    "pricing_model": details.get("pricing_model", ""),
                    "pricing_type": details.get("pricing_type", ""),
                    "conversation_id": details.get("conversation_id", ""),
                    "region": region,
                    "priced": price is not None,
                    "price_source": price.source if price else "none",
                    "price_eur": price.price_eur if price else None,
                    "allowance": price.free_allowance_per_month if price else None,
                    "allowance_used_before": used,
                },
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return event

    async def record_llm(self, tenant_id: str, usage: Usage, meta: dict[str, Any]) -> UsageEvent:
        now = self.clock.now()
        model = str(meta.get("model", ""))
        usd = float(meta.get("usd", 0.0))
        if not usd:
            price = await self.model_price(model, now.date())
            if price is not None:
                usd = model_cost_usd(
                    price,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                )
        rate = await self.usd_eur(now.date())
        async with self.db.tenant_scope(tenant_id) as session:
            event = UsageEvent(
                tenant_id=tenant_id,
                kind="llm",
                category=model,
                quantity=usage.input_tokens
                + usage.output_tokens
                + usage.cache_write_tokens
                + usage.cache_read_tokens,
                unit="token",
                cost_micro_usd=to_micro(usd),
                cost_micro_eur=round(usd * rate * MICRO),
                occurred_at=now,
                ref=f"llm:{meta.get('request_id') or new_id('req')}",
                meta={
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "cache_write": usage.cache_write_tokens,
                    "cache_read": usage.cache_read_tokens,
                    "inference_geo": usage.inference_geo or "",
                    "request_id": meta.get("request_id", ""),
                    "usd_eur": rate,
                },
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return event

    # --------------------------------------------------------------- report
    async def month_report(
        self, tenant_id: str, tenant_name: str, profile: TenantProfile, year: int, month: int
    ) -> UsageReport:
        start = local_to_utc(date(year, month, 1), time(0, 0), profile.timezone)
        end = self.month_end(profile.timezone, start + timedelta(days=1))
        async with self.db.tenant_scope(tenant_id) as session:
            grouped = (
                await session.execute(
                    select(
                        UsageEvent.kind,
                        UsageEvent.category,
                        UsageEvent.unit,
                        func.count(),
                        func.sum(UsageEvent.quantity),
                        func.sum(UsageEvent.cost_micro_eur),
                    )
                    .where(
                        UsageEvent.tenant_id == tenant_id,
                        UsageEvent.occurred_at >= start,
                        UsageEvent.occurred_at < end,
                    )
                    .group_by(UsageEvent.kind, UsageEvent.category, UsageEvent.unit)
                    .order_by(UsageEvent.kind, UsageEvent.category)
                )
            ).all()
        rows = [
            ReportRow(kind, category or "-", int(count), int(quantity or 0), unit, int(cost or 0))
            for kind, category, unit, count, quantity, cost in grouped
        ]
        return UsageReport(tenant_id, tenant_name, year, month, self.clock.now(), rows)

    async def reconcile_whatsapp(
        self, tenant_id: str, profile: TenantProfile, year: int, month: int
    ) -> int:
        """Append one correction row when current tables price the month differently."""
        start = local_to_utc(date(year, month, 1), time(0, 0), profile.timezone)
        end = self.month_end(profile.timezone, start + timedelta(days=1))
        async with self.db.tenant_scope(tenant_id) as session:
            events = list(
                (
                    await session.execute(
                        select(UsageEvent)
                        .where(
                            UsageEvent.tenant_id == tenant_id,
                            UsageEvent.kind == "whatsapp",
                            UsageEvent.occurred_at >= start,
                            UsageEvent.occurred_at < end,
                        )
                        .order_by(UsageEvent.occurred_at, UsageEvent.id)
                    )
                ).scalars()
            )
        recorded = sum(e.cost_micro_eur for e in events)
        expected = 0
        used: dict[str, int] = {}
        for event in events:
            if event.category == CORRECTION or not event.meta.get("billable"):
                continue
            price = await self.message_price(
                tenant_id,
                str(event.meta.get("region") or "DE"),
                event.category,
                event.occurred_at.date(),
            )
            if price is None:
                continue
            before = used.get(event.category, 0)
            used[event.category] = before + 1
            if before >= price.free_allowance_per_month:
                expected += to_micro(price.price_eur)
        delta = expected - recorded
        if delta == 0:
            return 0
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            session.add(
                UsageEvent(
                    tenant_id=tenant_id,
                    kind="whatsapp",
                    category=CORRECTION,
                    quantity=0,
                    unit="message",
                    cost_micro_eur=delta,
                    occurred_at=min(now, end - timedelta(seconds=1)),
                    ref=f"correction:wa:{year:04d}-{month:02d}:{new_id('cor')}",
                    meta={
                        "reconciled_at": now.isoformat(),
                        "expected": expected,
                        "recorded": recorded,
                    },
                )
            )
            await session.commit()
        return delta


def _default_model_rows() -> tuple[ModelPriceRow, ...]:
    from concierge.metering.prices import DEFAULT_MODEL_PRICES

    return DEFAULT_MODEL_PRICES


def month_of(instant: datetime, tz: str) -> tuple[int, int]:
    local = to_local(instant.astimezone(UTC), tz)
    return local.year, local.month
