"""Tenant profile schema (Appendix C)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from concierge.tenants.profile import TenantProfile, validation_errors

pytestmark = pytest.mark.feature("F21")

SAMPLE = {
    "slug": "hotel-beispiel",
    "name": "Hotel Beispiel",
    "timezone": "Europe/Berlin",
    "status": "demo",
    "ai_mode": "full",
    "languages": ["de", "en", "ru", "uk", "tr", "it"],
    "contacts": {
        "reception_phone": "+49 69 000000",
        "email": "reception@example.invalid",
        "privacy_url": "https://example.invalid/privacy",
    },
    "staffed_hours": {"mon-sun": "07:00-22:00"},
    "handoff": {
        "sla": {"p1_min": 10, "p2_min": 120},
        "contacts": [
            {"name": "Front desk", "whatsapp": "+49 69 000001", "email": "desk@example.invalid"}
        ],
    },
    "availability": {
        "chain": ["manual", "none"],
        "stale_after_hours": 36,
        "booking_url": "https://example.invalid/book?from={arrival}&to={departure}&adults={adults}",
    },
    "journeys": {
        "quiet_hours": "21:00-08:00",
        "t_minus_7": True,
        "t_minus_2": {"at": "10:00"},
        "review_request": {"enabled": False},
    },
    "registration": {"enabled": False},
    "voice": {
        "provider": "retell",
        "eu_only": False,
        "us_transfer_acknowledged": False,
        "included_minutes": 300,
        "overage_eur_per_min": 0.19,
    },
    "widget": {
        "public_key": "pk_demo",
        "origins": ["http://localhost:8000"],
        "primary_color": "#C22113",
    },
    "limits": {"daily_llm_budget_usd": 5, "messages_per_guest_per_hour": 20},
    "pricing": {"price_table": "default_de", "model_prices": "default", "usd_eur": "default"},
    "legal": {"status": "draft_unreviewed"},
}


def test_sample_profile_validates() -> None:
    profile = TenantProfile.model_validate(SAMPLE)
    assert profile.journeys.is_quiet(datetime(2026, 1, 1, 22, 0).time())
    assert not profile.journeys.is_quiet(datetime(2026, 1, 1, 9, 0).time())
    assert validation_errors(SAMPLE) == []


def test_errors_carry_field_paths() -> None:
    bad = dict(SAMPLE, languages=["ru"], widget={"public_key": "nope", "primary_color": "red"})
    errors = validation_errors(bad)
    assert any(e.startswith("languages:") for e in errors)
    assert any(e.startswith("widget.public_key:") for e in errors)
    assert any(e.startswith("widget.primary_color:") for e in errors)


def test_unknown_keys_rejected() -> None:
    errors = validation_errors(dict(SAMPLE, unexpected=1))
    assert errors and "unexpected" in errors[0]


def test_booking_url_placeholders() -> None:
    bad = dict(SAMPLE, availability={"booking_url": "https://x.invalid/?a={secret}"})
    assert any("booking_url" in e and "secret" in e for e in validation_errors(bad))


def test_eu_only_gate_for_retell() -> None:
    bad = dict(SAMPLE, voice={"provider": "retell", "eu_only": True})
    assert any("us_transfer_acknowledged" in e for e in validation_errors(bad))
    ok = dict(
        SAMPLE, voice={"provider": "retell", "eu_only": True, "us_transfer_acknowledged": True}
    )
    assert validation_errors(ok) == []


def test_checkout_reminder_never_before_seven() -> None:
    bad = dict(SAMPLE, journeys={"checkout": {"at": "06:30"}})
    assert any("07:00" in e for e in validation_errors(bad))


def test_staffed_hours_and_next_start() -> None:
    profile = TenantProfile.model_validate(
        dict(SAMPLE, staffed_hours={"mon-fri": "07:00-22:00", "sat-sun": "closed"})
    )
    monday_noon = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)  # 12:00 Berlin
    assert profile.is_staffed(monday_noon)
    saturday = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
    assert not profile.is_staffed(saturday)
    nxt = profile.next_staffed_start(saturday)
    assert nxt == datetime(2026, 10, 5, 5, 0, tzinfo=UTC)  # Monday 07:00 CEST
    late = datetime(2026, 9, 28, 21, 0, tzinfo=UTC)  # 23:00 Berlin Monday
    assert not profile.is_staffed(late)
    assert profile.next_staffed_start(late) == datetime(2026, 9, 29, 5, 0, tzinfo=UTC)


def test_bad_staffed_hours_key() -> None:
    assert any(
        "staffed_hours" in e
        for e in validation_errors(dict(SAMPLE, staffed_hours={"weekdays": "07:00-22:00"}))
    )
