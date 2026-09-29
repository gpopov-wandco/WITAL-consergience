"""Booking links (F10): template rendering with escaping and the Mews deep link."""

from __future__ import annotations

from datetime import date

import pytest
from tests.unit.test_profile import SAMPLE

from concierge.availability.booking import booking_link, mews_booking_url, render_booking_url
from concierge.availability.models import Occupancy, Stay
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F10")

STAY = Stay(date(2026, 10, 3), date(2026, 10, 5))


def test_template_rendering_escapes_every_value() -> None:
    template = "https://example.invalid/book?from={arrival}&to={departure}&a={adults}&c={children}&l={lang}&r={room}"
    url = render_booking_url(template, STAY, Occupancy(2, 1), "de", "Deluxe & Co/ä")
    assert url == (
        "https://example.invalid/book?from=2026-10-03&to=2026-10-05&a=2&c=1&l=de&r=Deluxe%20%26%20Co%2F%C3%A4"
    )
    assert "{" not in render_booking_url(
        "https://x.invalid/{arrival}/{room}", STAY, Occupancy(1), "en"
    )
    assert (
        render_booking_url("https://x.invalid/{arrival}", STAY, Occupancy(1), "<script>")
        == "https://x.invalid/2026-10-03"
    )


def test_mews_deep_link_keeps_existing_query_and_adds_parameters() -> None:
    url = mews_booking_url(
        "https://app.mews.com/distributor/abc?x=1&mewsStart=old", STAY, Occupancy(2, 1), "de"
    )
    assert url.startswith("https://app.mews.com/distributor/abc?x=1&")
    assert (
        "mewsStart=2026-10-03" in url and "mewsEnd=2026-10-05" in url and "mewsStart=old" not in url
    )
    assert (
        "mewsAdultCount=2" in url
        and "mewsChildCount=1" in url
        and "language=de" in url
        and "currency=EUR" in url
    )


def test_booking_link_from_profile() -> None:
    profile = TenantProfile.model_validate(SAMPLE)
    link = booking_link(profile, STAY, Occupancy(2), "en", "dz")
    assert link == "https://example.invalid/book?from=2026-10-03&to=2026-10-05&adults=2"
    none = TenantProfile.model_validate(
        dict(SAMPLE, availability={"chain": ["none"], "booking_url": ""})
    )
    assert booking_link(none, STAY, Occupancy(2), "en") is None
    mews = TenantProfile.model_validate(
        dict(
            SAMPLE,
            availability={"chain": ["mews"], "booking_url": "https://app.mews.com/distributor/abc"},
        )
    )
    assert "mewsStart=2026-10-03" in (booking_link(mews, STAY, Occupancy(2), "de") or "")
