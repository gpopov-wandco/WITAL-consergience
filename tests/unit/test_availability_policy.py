"""Availability policy (F07): one test per rule; chain order, breaker, single-flight, cache, budget."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from tests.unit.test_profile import SAMPLE

from concierge.agent.texts import guest_texts
from concierge.agent.validators import FORBIDDEN_BINDING
from concierge.availability.chain import AvailabilityService, ChainConfig
from concierge.availability.models import (
    AvailabilityAnswer,
    Money,
    Occupancy,
    Quote,
    Restriction,
    SourceUnavailable,
    Stay,
    status_of,
)
from concierge.availability.policy import asks_for_units, summarize, tool_result, wording
from concierge.availability.port import BaseAdapter
from concierge.clock import FakeClock
from concierge.db.models import Tenant
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F07")

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
STAY = Stay(date(2026, 10, 3), date(2026, 10, 5))
OCC = Occupancy(2, 0)
TZ = "Europe/Berlin"
TEXTS = guest_texts()
LANGS = ["de", "en", "ru", "uk", "tr", "it"]
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def quote(**kw: Any) -> Quote:
    base: dict[str, Any] = {
        "room_type": "dz",
        "room_name": "Doppelzimmer",
        "source": "manual",
        "fetched_at": NOW,
        "valid_until": NOW + timedelta(hours=1),
        "per_night": Money(12000),
        "total": Money(24000),
        "units_left": 3,
        "tax_inclusive": True,
    }
    base.update(kw)
    return Quote(**base)


def answer(
    source: str = "manual", quotes: list[Quote] | None = None, **kw: Any
) -> AvailabilityAnswer:
    quotes = quotes if quotes is not None else [quote(source=source)]
    base: dict[str, Any] = {
        "status": status_of(quotes),
        "source": source,
        "fetched_at": NOW,
        "valid_until": NOW + timedelta(hours=1),
        "stay": STAY,
        "occupancy": OCC,
        "quotes": quotes,
        "updated_at": NOW,
    }
    base.update(kw)
    return AvailabilityAnswer(**base)


# ------------------------------------------------------------------ rules
def test_rule1_model_sees_only_quote_numbers_and_vat_only_when_known() -> None:
    result = tool_result(answer(), "de", TEXTS, tz=TZ)
    quotes = result["quotes"]
    assert (
        quotes[0]["per_night"] == "120" and quotes[0]["total"] == "240" and quotes[0]["nights"] == 2
    )
    allowed = {"120", "240", "2", "29.09.2026", "12:00", "03.10.2026", "05.10.2026", "0"}
    for number in _NUMBER.findall(result["wording"]):
        assert number in allowed, number
    assert "inkl. MwSt." in result["wording"] and quotes[0]["tax_inclusive"] is True
    unknown_tax = answer(quotes=[quote(tax_inclusive=None)])
    text = wording(unknown_tax, "de", TEXTS, tz=TZ)
    assert (
        "MwSt" not in text
        and "tax_inclusive" not in tool_result(unknown_tax, "de", TEXTS, tz=TZ)["quotes"][0]
    )
    exclusive = wording(answer(quotes=[quote(tax_inclusive=False)]), "de", TEXTS, tz=TZ)
    assert "zzgl. MwSt." in exclusive
    dumped = json.dumps(result, ensure_ascii=False)
    assert "units_left" not in dumped and '"binding": false' in dumped


def test_rule2_wording_by_source() -> None:
    live = wording(answer("live_pms", [quote(source="live_pms")]), "de", TEXTS, tz=TZ)
    assert live.startswith("Das Buchungssystem zeigt aktuell:") and "unverbindlich" in live
    cached = wording(
        answer("live_pms_cached", [quote(source="live_pms_cached")]), "de", TEXTS, tz=TZ
    )
    assert cached.startswith("Stand 12:00:")
    calendar = wording(answer("manual"), "de", TEXTS, tz=TZ)
    assert (
        calendar.startswith("Laut unserem Kalender (Stand 29.09.2026):") and "Rezeption" in calendar
    )
    ical = wording(
        answer("ical", [quote(source="ical", per_night=None, total=None)]), "en", TEXTS, tz=TZ
    )
    assert ical.startswith("According to our calendar (updated 29 Sep 2026)") and "EUR" not in ical
    none = wording(answer("none", []), "de", TEXTS, tz=TZ)
    assert none.startswith("Ich frage die Rezeption") and "unverbindlich" not in none


def test_rule3_never_binding_in_any_language() -> None:
    for source in ("live_pms", "live_pms_cached", "manual", "ical"):
        result = tool_result(answer(source, [quote(source=source)]), "de", TEXTS, tz=TZ)
        assert result["binding"] is False
        for lang in LANGS:
            text = wording(answer(source, [quote(source=source)]), lang, TEXTS, tz=TZ).lower()
            for phrase in FORBIDDEN_BINDING[lang] + FORBIDDEN_BINDING["en"]:
                assert phrase not in text, (lang, phrase)
    assert quote().binding is False


def test_rule4_units_left_only_when_asked() -> None:
    silent = tool_result(answer(), "de", TEXTS, tz=TZ)
    assert "units_left" not in silent["quotes"][0] and "Noch 3 frei" not in silent["wording"]
    asked = tool_result(answer(), "de", TEXTS, tz=TZ, ask_units=True)
    assert asked["quotes"][0]["units_left"] == 3 and "Noch 3 frei" in asked["wording"]
    assert asks_for_units("Wie viele Zimmer sind noch frei?")
    assert asks_for_units("How many rooms are left?")
    assert not asks_for_units("Haben Sie am 3.10. ein Zimmer?")


def test_rule6_extras_only_from_pms_text() -> None:
    plain = tool_result(answer(), "de", TEXTS, tz=TZ)
    assert "untrusted_text" not in plain
    pms = answer("live_pms", [quote(source="live_pms", extras={"city_tax": "2 EUR per night"})])
    result = tool_result(pms, "de", TEXTS, tz=TZ)
    assert result["untrusted_text"] == {"city_tax": "2 EUR per night"}
    assert "city_tax" not in result["wording"] and "2 EUR" not in result["wording"]


def test_rule7_restriction_names_rule_and_alternative() -> None:
    min_stay = answer(quotes=[quote(restrictions=[Restriction("min_stay", "3", "2026-10-06")])])
    text = wording(min_stay, "de", TEXTS, tz=TZ)
    assert "Mindestaufenthalt 3 Nächte" in text and "Alternative: Aufenthalt bis 06.10.2026" in text
    assert min_stay.status == "restricted"
    closed = answer(
        quotes=[quote(restrictions=[Restriction("closed_arrival", "2026-10-03", "2026-10-04")])]
    )
    text = wording(closed, "en", TEXTS, tz=TZ)
    assert (
        "no arrival possible on 03 Oct 2026" in text
        and "Alternative: arrival on 04 Oct 2026" in text
    )
    occupancy = answer(quotes=[quote(restrictions=[Restriction("max_occupancy", "2", "2")])])
    text = wording(occupancy, "de", TEXTS, tz=TZ)
    assert "höchstens 2 Personen pro Zimmer" in text and "Alternative: 2 Zimmer" in text
    result = tool_result(occupancy, "de", TEXTS, tz=TZ)
    assert result["quotes"][0]["restrictions"] == [
        {"kind": "max_occupancy", "value": "2", "alternative": "2"}
    ]


def test_money_and_summaries_in_six_languages() -> None:
    assert Money(12050).format("de") == "120,50 EUR" and Money(12050).format("en") == "120.50 EUR"
    assert Money(12000).format("it") == "120 EUR" and Money(-500).format("de") == "-5 EUR"
    unavailable = answer(quotes=[quote(units_left=0)])
    assert unavailable.status == "unavailable"
    for lang in LANGS:
        for source, quotes in (
            ("manual", None),
            (
                "live_pms",
                [
                    quote(
                        source="live_pms", restrictions=[Restriction("min_stay", "3", "2026-10-06")]
                    )
                ],
            ),
            ("manual", [quote(units_left=0)]),
            ("none", []),
        ):
            text = wording(answer(source, quotes), lang, TEXTS, tz=TZ, ask_units=True)
            assert "{" not in text and text.strip(), (lang, source, text)
        summary = summarize(unavailable, lang, TEXTS, tz=TZ)
        assert summary.status == "unavailable" and summary.summary


# ------------------------------------------------------------------ chain
class FakeAdapter(BaseAdapter):
    def __init__(
        self, name: str, *, fail: int = 0, delay_s: float = 0.0, source: str = "manual"
    ) -> None:
        self.name = name
        self.fail = fail
        self.delay_s = delay_s
        self.source = source
        self.calls = 0
        self.clock: FakeClock | None = None

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Any, promo: str = ""
    ) -> AvailabilityAnswer:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail > 0:
            self.fail -= 1
            raise SourceUnavailable("boom")
        now = self.clock.now() if self.clock else NOW
        valid = now + (timedelta(seconds=120) if self.source == "live_pms" else timedelta(hours=1))
        return AvailabilityAnswer(
            status="available",
            source=self.source,  # type: ignore[arg-type]
            fetched_at=now,
            valid_until=valid,
            stay=stay,
            occupancy=occupancy,
            quotes=[quote(source=self.source, fetched_at=now, valid_until=valid)],
            updated_at=now,
        )


def _tenant(**availability: Any) -> tuple[Tenant, TenantProfile]:
    profile = TenantProfile.model_validate(
        dict(SAMPLE, availability={**SAMPLE["availability"], **availability})
    )
    tenant = Tenant(
        id="ten_test",
        slug="hotel-beispiel",
        name="Hotel Beispiel",
        profile=profile.model_dump(mode="json"),
        public_key="pk_demo",
    )
    return tenant, profile


def _service(clock: FakeClock, adapters: dict[str, FakeAdapter]) -> AvailabilityService:
    for adapter in adapters.values():
        adapter.clock = clock

    async def factory(source: str, tenant: Tenant, profile: TenantProfile) -> Any:
        return adapters.get(source)

    return AvailabilityService(clock, factory, config=ChainConfig(call_timeout_s=1.0))


async def test_chain_first_configured_source_wins_and_none_is_unknown() -> None:
    clock = FakeClock(NOW)
    manual = FakeAdapter("manual")
    service = _service(clock, {"manual": manual})
    tenant, profile = _tenant(chain=["beds24", "manual", "none"])
    result = await service.check(tenant, profile, STAY, OCC)
    assert (
        result.source == "manual"
        and result.source_name == "manual"
        and "beds24:not_configured" in result.reasons
    )
    tenant, profile = _tenant(chain=["none"])
    unknown = await service.check(tenant, profile, STAY, OCC)
    assert (
        unknown.status == "unknown"
        and unknown.source == "none"
        and "no_live_data" in unknown.reasons
    )


async def test_breaker_opens_after_three_failures_for_sixty_seconds() -> None:
    clock = FakeClock(NOW)
    flaky = FakeAdapter("beds24", fail=3, source="live_pms")
    manual = FakeAdapter("manual")
    service = _service(clock, {"beds24": flaky, "manual": manual})
    tenant, profile = _tenant(chain=["beds24", "manual", "none"])
    for i in range(3):
        result = await service.check(
            tenant, profile, Stay(date(2026, 10, 3 + i), date(2026, 10, 5 + i)), OCC
        )
        assert result.source == "manual" and "beds24:SourceUnavailable" in result.reasons
    assert flaky.calls == 3 and service.breaker(tenant.id, "beds24").is_open
    result = await service.check(tenant, profile, Stay(date(2026, 10, 10), date(2026, 10, 12)), OCC)
    assert flaky.calls == 3 and "beds24:breaker_open" in result.reasons
    clock.advance(timedelta(seconds=61))
    result = await service.check(tenant, profile, Stay(date(2026, 10, 12), date(2026, 10, 14)), OCC)
    assert flaky.calls == 4 and result.source == "live_pms"


async def test_single_flight_per_cache_key() -> None:
    clock = FakeClock(NOW)
    slow = FakeAdapter("manual", delay_s=0.05)
    service = _service(clock, {"manual": slow})
    tenant, profile = _tenant(chain=["manual"])
    results = await asyncio.gather(*(service.check(tenant, profile, STAY, OCC) for _ in range(5)))
    assert slow.calls == 1 and all(r.source == "manual" for r in results)
    other = await service.check(tenant, profile, STAY, Occupancy(1, 0))
    assert slow.calls == 2 and other.occupancy.adults == 1


async def test_rule5_cache_validity_and_expiry_force_requery() -> None:
    clock = FakeClock(NOW)
    live = FakeAdapter("beds24", source="live_pms")
    service = _service(clock, {"beds24": live})
    tenant, profile = _tenant(chain=["beds24"])
    first = await service.check(tenant, profile, STAY, OCC)
    assert first.source == "live_pms" and live.calls == 1
    clock.advance(timedelta(seconds=60))
    assert (
        await service.check(tenant, profile, STAY, OCC)
    ).source == "live_pms" and live.calls == 1
    clock.advance(timedelta(minutes=5))
    cached = await service.check(tenant, profile, STAY, OCC)
    assert (
        cached.source == "live_pms_cached"
        and cached.quotes[0].source == "live_pms_cached"
        and live.calls == 1
    )
    clock.advance(timedelta(minutes=10))
    assert (
        await service.check(tenant, profile, STAY, OCC)
    ).source == "live_pms" and live.calls == 2
    manual = FakeAdapter("manual")
    service = _service(clock, {"manual": manual})
    tenant, profile = _tenant(chain=["manual"])
    await service.check(tenant, profile, STAY, OCC)
    clock.advance(timedelta(minutes=30))
    await service.check(tenant, profile, STAY, OCC)
    assert manual.calls == 1
    clock.advance(timedelta(minutes=31))
    await service.check(tenant, profile, STAY, OCC)
    assert manual.calls == 2


async def test_budget_moves_to_next_source_and_high_demand_hides_prices() -> None:
    clock = FakeClock(NOW)
    pms = FakeAdapter("beds24", source="live_pms")
    manual = FakeAdapter("manual")
    service = _service(clock, {"beds24": pms, "manual": manual})
    tenant, profile = _tenant(chain=["beds24", "manual"], rate_limits={"beds24": "2 per minute"})
    for day in (3, 4):
        result = await service.check(
            tenant, profile, Stay(date(2026, 10, day), date(2026, 10, day + 2)), OCC
        )
        assert result.source == "live_pms"
    third = await service.check(tenant, profile, Stay(date(2026, 10, 8), date(2026, 10, 10)), OCC)
    assert third.source == "manual" and "beds24:budget" in third.reasons and pms.calls == 2
    tenant, profile = _tenant(chain=["manual"], high_demand_dates=["2026-10-04"])
    hot = await service.check(tenant, profile, Stay(date(2026, 10, 3), date(2026, 10, 5)), OCC)
    assert not hot.prices_shown and "high_demand_no_live_price" in hot.reasons
    assert "nennt die Rezeption den Preis" in wording(hot, "de", TEXTS, tz=TZ)
    tenant, profile = _tenant(chain=["beds24"], high_demand_dates=["2026-10-04"])
    service = _service(clock, {"beds24": FakeAdapter("beds24", source="live_pms")})
    assert (
        await service.check(tenant, profile, Stay(date(2026, 10, 3), date(2026, 10, 5)), OCC)
    ).prices_shown


async def test_source_unknown_falls_through_and_timeout_counts_as_failure() -> None:
    clock = FakeClock(NOW)

    class Unknown(FakeAdapter):
        async def quote(
            self, stay: Stay, occupancy: Occupancy, room_types: Any, promo: str = ""
        ) -> AvailabilityAnswer:
            self.calls += 1
            return replace(
                await super().quote(stay, occupancy, room_types), status="unknown", quotes=[]
            )

    hanging = FakeAdapter("beds24", delay_s=5.0, source="live_pms")
    service = _service(
        clock,
        {
            "beds24": hanging,
            "ical": Unknown("ical", source="ical"),
            "manual": FakeAdapter("manual"),
        },
    )
    service.config.call_timeout_s = 0.05
    tenant, profile = _tenant(chain=["beds24", "ical", "manual"])
    result = await service.check(tenant, profile, STAY, OCC)
    assert result.source == "manual"
    assert "beds24:TimeoutError" in result.reasons and "ical:unknown" in result.reasons
