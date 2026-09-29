"""Metering (F24): dated rows, the monthly allowance at boundaries, EUR, reconciliation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.unit.test_profile import SAMPLE

from concierge.clock import FakeClock
from concierge.db.models import FxRate, ModelPrice, PriceTable, Tenant, UsageEvent
from concierge.db.session import Database
from concierge.llm.port import Usage
from concierge.metering.service import MeteringService
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

pytestmark = pytest.mark.feature("F24")


@pytest.fixture
async def tenant(db: Database, clock: FakeClock) -> Tenant:
    return await TenantService(db, clock).create(TenantProfile.model_validate(SAMPLE), actor="test")


def details(ref: str, category: str, at: datetime, *, billable: bool = True) -> dict[str, object]:
    return {
        "ref": ref,
        "category": category,
        "billable": billable,
        "occurred_at": at,
        "conversation_id": "conv",
    }


async def test_dated_price_rows_tenant_rows_win_and_defaults_apply(
    db: Database, clock: FakeClock, tenant: Tenant
) -> None:
    metering = MeteringService(db, clock)
    default = await metering.message_price(tenant.id, "DE", "utility", date(2026, 9, 29))
    assert (
        default is not None
        and default.price_eur == 0.0456
        and default.free_allowance_per_month == 0
    )
    assert await metering.message_price(tenant.id, "DE", "service", date(2026, 9, 30)) is not None
    later = await metering.message_price(tenant.id, "DE", "service", date(2026, 10, 1))
    assert later is not None and later.free_allowance_per_month == 1000
    assert await metering.message_price(tenant.id, "XX", "utility", date(2026, 9, 29)) is None
    async with db.scope() as session:
        session.add(
            PriceTable(
                tenant_id=None,
                region="DE",
                category="utility",
                effective_from=date(2026, 10, 1),
                price_micro_eur=50_000,
                free_allowance_per_month=2,
                source_url="https://example.invalid/rates",
                updated_by="test",
            )
        )
        session.add(
            PriceTable(
                tenant_id=tenant.id,
                region="DE",
                category="utility",
                effective_from=date(2026, 10, 1),
                price_micro_eur=40_000,
                free_allowance_per_month=3,
                source_url="https://example.invalid/contract",
                updated_by="test",
            )
        )
        await session.commit()
    before = await metering.message_price(tenant.id, "DE", "utility", date(2026, 9, 30))
    assert before is not None and before.price_eur == 0.0456  # dated row not yet effective
    after = await metering.message_price(tenant.id, "DE", "utility", date(2026, 10, 2))
    assert after is not None and after.price_eur == 0.04 and after.free_allowance_per_month == 3
    assert after.source == "tenant" and before.source == "default"
    other = await metering.message_price("ten_other", "DE", "utility", date(2026, 10, 2))
    assert other is not None and other.price_eur == 0.05  # global row for everyone else
    assert other.source == "global"


async def test_allowance_counts_per_local_month(
    db: Database, clock: FakeClock, tenant: Tenant
) -> None:
    metering = MeteringService(db, clock)
    profile = TenantProfile.model_validate(SAMPLE)  # Europe/Berlin
    async with db.scope() as session:
        session.add(
            PriceTable(
                tenant_id=None,
                region="DE",
                category="utility",
                effective_from=date(2026, 1, 1),
                price_micro_eur=45_600,
                free_allowance_per_month=2,
                source_url="x",
                updated_by="test",
            )
        )
        await session.commit()
    # 2026-09-30 23:30 Berlin = 21:30 UTC still belongs to September
    september = datetime(2026, 9, 30, 21, 30, tzinfo=UTC)
    october = datetime(2026, 9, 30, 22, 30, tzinfo=UTC)  # 00:30 Berlin on 1 October
    costs = []
    for index, at in enumerate(
        [
            september,
            september + timedelta(minutes=1),
            september + timedelta(minutes=2),
            october,
            october + timedelta(minutes=1),
            october + timedelta(minutes=2),
        ]
    ):
        event = await metering.record_whatsapp(
            tenant.id, profile, details(f"wa:{index}", "utility", at)
        )
        assert event is not None and event.meta["price_source"] == "global"
        costs.append(event.cost_micro_eur)
    assert costs == [0, 0, 45_600, 0, 0, 45_600]
    assert (
        await metering.record_whatsapp(tenant.id, profile, details("wa:0", "utility", september))
        is None
    )
    free = await metering.record_whatsapp(
        tenant.id, profile, details("wa:free", "service", september, billable=False)
    )
    assert free is not None and free.cost_micro_eur == 0 and free.meta["billable"] is False
    assert free.meta["price_source"] == "none"


async def test_llm_usage_priced_in_usd_and_eur_from_dated_rows(
    db: Database, clock: FakeClock, tenant: Tenant
) -> None:
    metering = MeteringService(db, clock)
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000)
    event = await metering.record_llm(
        tenant.id, usage, {"model": "claude-sonnet-5-5", "request_id": "r1"}
    )
    assert event.cost_micro_usd == 3_000_000  # 2 USD input + 1 USD output
    assert event.cost_micro_eur == 2_760_000  # default 0.92
    async with db.scope() as session:
        session.add(
            FxRate(
                base="USD",
                quote="EUR",
                effective_from=date(2026, 9, 1),
                rate_micro=850_000,
                source="test",
            )
        )
        session.add(
            ModelPrice(
                model="claude-sonnet-5-5",
                effective_from=date(2026, 9, 1),
                input_micro_usd_per_mtok=1_000_000,
                output_micro_usd_per_mtok=5_000_000,
                cache_write_micro_usd_per_mtok=1_250_000,
                cache_read_micro_usd_per_mtok=100_000,
                source_url="x",
            )
        )
        await session.commit()
    again = await metering.record_llm(
        tenant.id, usage, {"model": "claude-sonnet-5-5", "request_id": "r2"}
    )
    assert again.cost_micro_usd == 1_500_000 and again.cost_micro_eur == 1_275_000
    priced = await metering.record_llm(
        tenant.id, usage, {"model": "claude-sonnet-5-5", "request_id": "r3", "usd": 0.5}
    )
    assert priced.cost_micro_usd == 500_000 and priced.meta["usd_eur"] == 0.85


async def test_month_report_and_reconciliation_append_corrections(
    db: Database, clock: FakeClock, tenant: Tenant
) -> None:
    metering = MeteringService(db, clock)
    profile = TenantProfile.model_validate(SAMPLE)
    at = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
    for index in range(3):
        await metering.record_whatsapp(
            tenant.id, profile, details(f"wa:{index}", "marketing", at + timedelta(minutes=index))
        )
    report = await metering.month_report(tenant.id, "Hotel Beispiel", profile, 2026, 9)
    assert [(r.kind, r.category, r.events) for r in report.rows] == [("whatsapp", "marketing", 3)]
    assert report.total_micro_eur == 3 * 113_100
    assert '"total_eur": 0.3393' in report.to_json() and "whatsapp;marketing;3" in report.to_csv()
    assert report.to_pdf().startswith(b"%PDF")
    # nothing to correct while the tables are unchanged
    assert await metering.reconcile_whatsapp(tenant.id, profile, 2026, 9) == 0
    async with db.scope() as session:
        session.add(
            PriceTable(
                tenant_id=None,
                region="DE",
                category="marketing",
                effective_from=date(2026, 9, 1),
                price_micro_eur=100_000,
                free_allowance_per_month=1,
                source_url="x",
                updated_by="test",
            )
        )
        await session.commit()
    delta = await metering.reconcile_whatsapp(tenant.id, profile, 2026, 9)
    assert delta == 2 * 100_000 - 3 * 113_100
    assert await metering.reconcile_whatsapp(tenant.id, profile, 2026, 9) == 0  # idempotent
    report = await metering.month_report(tenant.id, "Hotel Beispiel", profile, 2026, 9)
    assert report.total_micro_eur == 200_000
    async with db.scope() as session:
        corrections = list(
            (
                await session.execute(select(UsageEvent).where(UsageEvent.category == "correction"))
            ).scalars()
        )
    assert len(corrections) == 1 and corrections[0].quantity == 0
    assert corrections[0].occurred_at < datetime(2026, 10, 1, tzinfo=UTC)
    newest = await metering.newest_price_dates()
    assert newest[0] == date(2026, 10, 1) and newest[1] == date(2026, 9, 29)


async def test_allowance_counts_billable_rows_only_and_prices_come_from_the_table(
    db: Database, clock: FakeClock, tenant: Tenant
) -> None:
    metering = MeteringService(db, clock)
    profile = TenantProfile.model_validate(SAMPLE)
    async with db.scope() as session:
        session.add(
            PriceTable(
                tenant_id=None,
                region="DE",
                category="utility",
                effective_from=date(2026, 1, 1),
                price_micro_eur=45_600,
                free_allowance_per_month=2,
                source_url="x",
                updated_by="test",
            )
        )
        await session.commit()
    at = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
    for index in range(3):  # status rows Meta marks as not billable do not use the allowance
        free = await metering.record_whatsapp(
            tenant.id,
            profile,
            details(f"nb:{index}", "utility", at + timedelta(minutes=index), billable=False),
        )
        assert free is not None and free.cost_micro_eur == 0
    costs = []
    for index in range(3):
        event = await metering.record_whatsapp(
            tenant.id, profile, details(f"b:{index}", "utility", at + timedelta(minutes=10 + index))
        )
        assert event is not None
        costs.append(event.cost_micro_eur)
    assert costs == [0, 0, 45_600]
    assert await metering.reconcile_whatsapp(tenant.id, profile, 2026, 9) == 0  # both paths agree

    default = await metering.model_price_or_default("claude-sonnet-5-5", date(2026, 9, 29))
    assert default is not None and default.input_usd_per_mtok == 2.0
    await metering.set_model_price(
        "claude-sonnet-5-5", date(2026, 9, 1), input_usd=3.0, output_usd=15.0, actor="test"
    )
    configured = await metering.model_price_or_default("claude-sonnet-5-5", date(2026, 9, 29))
    assert configured is not None and configured.input_usd_per_mtok == 3.0
    assert configured.cache_write_usd_per_mtok == 3.75 and configured.cache_read_usd_per_mtok == 0.3
    earlier = await metering.model_price_or_default("claude-sonnet-5-5", date(2026, 8, 1))
    assert (
        earlier is not None and earlier.input_usd_per_mtok == 2.0
    )  # the row applies from its date
    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    event = await metering.record_llm(
        tenant.id, usage, {"model": "claude-sonnet-5-5", "request_id": "r1"}
    )
    assert event.cost_micro_usd == 3_000_000
    assert await metering.llm_usd_today(tenant.id, profile.timezone) == pytest.approx(3.0)
