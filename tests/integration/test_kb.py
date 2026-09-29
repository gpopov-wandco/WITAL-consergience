"""Knowledge base lifecycle, resolver windows, snapshot stability, sensitivity and gaps."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from tests.unit.test_profile import SAMPLE

from concierge.clock import FakeClock
from concierge.db.session import Database
from concierge.kb.blocks import BLOCKS, REQUIRED_KEYS, validate_key
from concierge.kb.fixtures import FIXTURES_DIR, import_kb_file
from concierge.kb.store import KbError, KbService
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

pytestmark = pytest.mark.feature("F05")


@pytest.fixture
async def tenant_id(db: Database, clock: FakeClock) -> str:
    tenant = await TenantService(db, clock).create(TenantProfile.model_validate(SAMPLE), actor="t")
    return str(tenant.id)


@pytest.fixture
def kb(db: Database, clock: FakeClock) -> KbService:
    return KbService(db, clock)


def test_block_catalogue_matches_appendix_d() -> None:
    expected = {
        "identity",
        "contact_hours",
        "checkin",
        "checkout",
        "late_arrival",
        "rooms",
        "rates_policy",
        "cancellation",
        "payment",
        "city_tax",
        "breakfast",
        "parking",
        "pets",
        "extras",
        "accessibility",
        "wifi",
        "luggage",
        "directions",
        "nearby",
        "events",
        "house_rules",
        "invoicing",
        "lost_property",
        "emergency",
        "handoff",
        "upsells",
        "tone",
        "refusals",
        "legal",
    }
    assert set(BLOCKS) == expected
    assert "parking" in REQUIRED_KEYS and BLOCKS["parking"].volatile
    assert BLOCKS["wifi"].sensitivity == "verified_guest_only"
    assert validate_key("directions.airport") == "directions.airport"
    with pytest.raises(ValueError, match="Appendix D"):
        validate_key("swimming_pool")


async def test_lifecycle_and_versions(kb: KbService, tenant_id: str) -> None:
    draft = await kb.upsert(
        tenant_id, "parking", "de", text="Garage 18 EUR", value={"price_cents_per_night": 1800}
    )
    assert draft.status == "draft" and draft.version == 1 and draft.volatile
    with pytest.raises(KbError, match="approver"):
        await kb.set_status(tenant_id, draft.id, "approved", actor="owner")
    pending = await kb.set_status(tenant_id, draft.id, "pending_approval", actor="staff")
    approved = await kb.set_status(
        tenant_id, pending.id, "approved", actor="owner", approver="Anna Beispiel"
    )
    assert approved.approver == "Anna Beispiel" and approved.approved_at is not None
    v2 = await kb.upsert(
        tenant_id,
        "parking",
        "de",
        text="Garage 20 EUR",
        status="approved",
        approver="Anna Beispiel",
    )
    assert v2.version == 2
    rows = await kb.entries(tenant_id, lang="de")
    statuses = {r.version: r.status for r in rows}
    assert statuses == {1: "retired", 2: "approved"}
    with pytest.raises(KbError, match="cannot move"):
        await kb.set_status(tenant_id, rows[1].id, "draft", actor="x")
    with pytest.raises(KbError, match="unknown status"):
        await kb.upsert(tenant_id, "parking", "de", text="x", status="published")


async def test_only_approved_entries_reach_guests(kb: KbService, tenant_id: str) -> None:
    await kb.upsert(tenant_id, "breakfast", "de", text="Frühstück 07:00 bis 10:00, 16 EUR")
    snapshot = await kb.snapshot(tenant_id, "de")
    assert snapshot.entries == [] and snapshot.text.strip() == ""
    await kb.upsert(
        tenant_id,
        "breakfast",
        "de",
        text="Frühstück 07:00 bis 10:00, 16 EUR",
        status="approved",
        approver="A",
    )
    snapshot = await kb.snapshot(tenant_id, "de")
    assert snapshot.keys == ["breakfast"]


async def test_resolver_uses_stay_date_windows(kb: KbService, tenant_id: str) -> None:
    await kb.upsert(
        tenant_id,
        "breakfast",
        "en",
        text="Summer breakfast 06:30",
        status="approved",
        approver="A",
        valid_from=date(2026, 6, 1),
        valid_to=date(2026, 9, 30),
    )
    await kb.upsert(
        tenant_id,
        "breakfast",
        "en",
        text="Winter breakfast 07:00",
        status="approved",
        approver="A",
        valid_from=date(2026, 10, 1),
        valid_to=None,
    )
    assert (
        await kb.lookup(tenant_id, "breakfast", "en", stay_date=date(2026, 7, 1))
    ).text.startswith("Summer")
    assert (
        await kb.lookup(tenant_id, "breakfast", "en", stay_date=date(2026, 12, 1))
    ).text.startswith("Winter")
    assert await kb.lookup(tenant_id, "breakfast", "en", stay_date=date(2026, 1, 1)) is None
    with pytest.raises(KbError, match="valid_to"):
        await kb.upsert(
            tenant_id,
            "breakfast",
            "en",
            text="x",
            valid_from=date(2026, 2, 1),
            valid_to=date(2026, 1, 1),
        )


async def test_overdue_volatile_entries_are_ignored(
    kb: KbService, tenant_id: str, clock: FakeClock
) -> None:
    await kb.upsert(
        tenant_id,
        "parking",
        "en",
        text="Garage EUR 18",
        status="approved",
        approver="A",
        review_days=10,
    )
    await kb.upsert(
        tenant_id,
        "house_rules",
        "en",
        text="No smoking",
        status="approved",
        approver="A",
        review_days=10,
    )
    assert (await kb.snapshot(tenant_id, "en")).keys == ["house_rules", "parking"]
    clock.advance(timedelta(days=11))
    snapshot = await kb.snapshot(tenant_id, "en")
    assert snapshot.keys == ["house_rules"]
    assert snapshot.excluded_overdue == ["parking"]
    assert [e.key for e in await kb.overdue(tenant_id)] == ["house_rules", "parking"]


async def test_snapshot_bytes_are_stable_and_exclude_non_public(
    kb: KbService, tenant_id: str
) -> None:
    await kb.upsert(
        tenant_id, "wifi", "en", text="password beispiel2026", status="approved", approver="A"
    )
    await kb.upsert(tenant_id, "tone", "en", text="calm", status="approved", approver="A")
    await kb.upsert(
        tenant_id,
        "parking",
        "en",
        text="Garage EUR 18",
        value={"price_cents_per_night": 1800, "clearance_m": 2.1},
        status="approved",
        approver="A",
    )
    await kb.upsert(tenant_id, "checkin", "en", text="From 15:00", status="approved", approver="A")
    first = await kb.snapshot(tenant_id, "en")
    second = await kb.snapshot(tenant_id, "en")
    assert first.text == second.text and first.digest == second.digest
    assert "beispiel2026" not in first.text and "calm" not in first.text
    assert first.text.index("## checkin") < first.text.index("## parking")
    assert '"clearance_m": 2.1' in first.text
    assert await kb.lookup(tenant_id, "wifi", "en") is None
    assert (await kb.lookup(tenant_id, "wifi", "en", verified=True)).text == "password beispiel2026"


async def test_language_fallback_to_english(kb: KbService, tenant_id: str) -> None:
    await kb.upsert(
        tenant_id, "pets", "en", text="Dogs welcome, EUR 15", status="approved", approver="A"
    )
    view = await kb.lookup(tenant_id, "pets", "tr")
    assert view is not None and view.fallback_from == "en"
    snapshot = await kb.snapshot(tenant_id, "tr")
    assert "(text available in en only)" in snapshot.text
    await kb.upsert(
        tenant_id,
        "pets",
        "tr",
        text="Köpekler kabul edilir, 15 EUR",
        status="approved",
        approver="A",
    )
    assert (await kb.lookup(tenant_id, "pets", "tr")).fallback_from is None


async def test_search_gaps_and_coverage(kb: KbService, tenant_id: str) -> None:
    count = await import_kb_file(kb, tenant_id, FIXTURES_DIR / "hotel_beispiel.yaml", approve=True)
    assert count >= 30 * 6
    hits = await kb.search(tenant_id, "Wo kann ich parken und was kostet die Garage", "de")
    assert hits and hits[0].key == "parking"
    hits_uk = await kb.search(tenant_id, "паркування гараж", "uk")
    assert hits_uk and hits_uk[0].key == "parking"
    assert await kb.search(tenant_id, "", "de") == []
    await kb.record_gap(tenant_id, "Gibt es einen Pool?", "de", None)
    await kb.record_gap(tenant_id, "Gibt es einen Pool?", "de", None)
    report = await kb.gap_report(tenant_id)
    assert len(report) == 1 and report[0].count == 2
    coverage = await kb.coverage(tenant_id, ["de", "en", "ru", "uk", "tr", "it"])
    assert all(status == "approved" for status in coverage["parking"].values())
    assert set(coverage) == set(REQUIRED_KEYS)


async def test_fixture_values_are_consistent_across_languages(
    kb: KbService, tenant_id: str
) -> None:
    await import_kb_file(kb, tenant_id, FIXTURES_DIR / "hotel_beispiel.yaml", approve=True)
    for lang in ("de", "en", "ru", "uk", "tr", "it"):
        parking = await kb.lookup(tenant_id, "parking", lang)
        breakfast = await kb.lookup(tenant_id, "breakfast", lang)
        checkin = await kb.lookup(tenant_id, "checkin", lang)
        assert (
            parking is not None
            and "18" in parking.text
            and ("2,10" in parking.text or "2.10" in parking.text)
        ), lang
        assert (
            breakfast is not None
            and "07:00" in breakfast.text
            and "10:00" in breakfast.text
            and "16" in breakfast.text
        ), lang
        assert checkin is not None and "15:00" in checkin.text, lang
        assert parking.fallback_from is None
        wifi = await kb.lookup(tenant_id, "wifi", lang, verified=True)
        assert wifi is not None and wifi.sensitivity == "verified_guest_only"
