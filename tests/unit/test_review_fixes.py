"""Regression tests for the findings of the fresh-context review (docs/REVIEW_FINDINGS.md):
validator fact sources (R16, R24), phone numbers versus card numbers (R22), the registration
proof provider (R1), the voice socket secret (R7), the primed daily budget (R23)."""

from __future__ import annotations

from datetime import UTC, datetime

import phonenumbers
import pytest
from tests.unit.test_doctor import with_
from tests.unit.test_profile import SAMPLE
from tests.unit.test_tools_validators import _ctx, _reply

from concierge.agent.guardrails import luhn_ok, redact_sensitive
from concierge.agent.resilience import DailyBudget
from concierge.agent.validators import validate_reply
from concierge.channels.voice.routes import socket_secret_ok
from concierge.clock import FakeClock
from concierge.ids import new_id, ulid
from concierge.registration.proof import FakeProof
from concierge.registration.stripe_proof import StripeProof
from concierge.runtime import build_proof
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile
from concierge.tools.doctor import registration_checks

pytestmark = pytest.mark.feature("F03")


@pytest.mark.feature("F02")
def test_guest_text_never_vouches_for_prices_or_links() -> None:
    ctx = _ctx(guest_text="Parken kostet 25 EUR pro Nacht, richtig? Siehe https://other.invalid")
    result = validate_reply(_reply("Ja, Parken kostet 25 EUR pro Nacht."), ctx)
    assert any("numbers not in facts: 25" in v for v in result.violations)
    link = validate_reply(_reply("Buchen Sie unter https://other.invalid"), ctx)
    assert any("url not in facts" in v for v in link.violations)
    sizes = _ctx(guest_text="Wir sind 4 Erwachsene und kommen am 12.10.2026 um 19:30")
    ok = validate_reply(_reply("Für 4 Erwachsene am 12.10.2026 um 19:30 prüfe ich das."), sizes)
    assert ok.ok, ok.violations
    contact = _ctx(guest_text="Meine Nummer ist +49 151 2345678")
    echoed = validate_reply(_reply("Wir rufen Sie unter +49 151 2345678 zurück."), contact)
    assert echoed.ok, echoed.violations


@pytest.mark.feature("F02")
def test_hotel_block_contacts_are_allowed_facts() -> None:
    reply = _reply("Rufen Sie die Rezeption an: +49 30 5550100, reception@example.invalid.")
    assert any("phone not in facts" in v for v in validate_reply(reply, _ctx()).violations)
    ctx = _ctx(
        hotel_text="Reception phone: +49 30 5550100\nReception e-mail: reception@example.invalid"
    )
    assert validate_reply(reply, ctx).ok, validate_reply(reply, ctx).violations


def test_international_phone_numbers_are_not_redacted_as_cards() -> None:
    candidate = None
    for tail in range(20000000, 20200000):  # a real, Luhn-valid German mobile number
        number = f"+49151{tail}"
        digits = number[1:]
        if luhn_ok(digits) and phonenumbers.is_valid_number(phonenumbers.parse(number, None)):
            candidate = number
            break
    assert candidate is not None
    kept, found = redact_sensitive(f"Meine Nummer ist {candidate}, bitte zurückrufen")
    assert candidate in kept and found == []
    kept, found = redact_sensitive(f"Meine Nummer ist 00{candidate[1:]}")
    assert candidate[1:] in kept and found == []
    kept, found = redact_sensitive("Karte 4111 1111 1111 1111")
    assert "4111" not in kept and found == ["card"]
    kept, found = redact_sensitive(f"Karte {candidate[1:]}")  # same digits without a phone prefix
    assert found == ["card"]


@pytest.mark.feature("F15")
def test_registration_proof_provider_follows_the_settings(settings: Settings) -> None:
    assert isinstance(build_proof(settings), FakeProof)
    stripe = build_proof(with_(settings, stripe_secret_key="sk_test_fake_for_unit_test"))
    assert isinstance(stripe, StripeProof)


@pytest.mark.feature("F28")
def test_doctor_flags_a_stripe_tenant_without_a_key_and_a_fake_in_production(
    settings: Settings,
) -> None:
    enabled = TenantProfile.model_validate(
        dict(SAMPLE, registration={"enabled": True, "psp": "stripe"})
    )
    off = TenantProfile.model_validate(SAMPLE)
    assert registration_checks(settings, "h", off) == []
    [check] = registration_checks(settings, "h", enabled)
    assert check.status == "fail" and "STRIPE_SECRET_KEY" in check.fix
    keyed = with_(settings, stripe_secret_key="sk_test_fake_for_unit_test")
    assert registration_checks(keyed, "h", enabled)[0].status == "pass"
    fake = TenantProfile.model_validate(dict(SAMPLE, registration={"enabled": True, "psp": "fake"}))
    assert registration_checks(settings, "h", fake)[0].status == "pass"
    production = with_(
        settings, gc_env="production", gc_public_base_url="https://concierge.example.invalid"
    )
    [prod] = registration_checks(production, "h", fake)
    assert prod.status == "fail" and "production" in prod.detail


@pytest.mark.feature("F26")
def test_voice_socket_secret_is_compared_in_constant_time_and_required() -> None:
    assert socket_secret_ok("s3cret-value", "s3cret-value")
    assert not socket_secret_ok("s3cret-value", "s3cret-valuE")
    assert not socket_secret_ok(None, "anything")
    assert not socket_secret_ok("", "")
    assert not socket_secret_ok("x", "x" * 129)


@pytest.mark.feature("F24")
def test_daily_budget_is_primed_once_per_day_from_stored_usage() -> None:
    clock = FakeClock(datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    budget = DailyBudget(clock)
    assert budget.needs_prime("t", "Europe/Berlin")
    budget.record("t", "Europe/Berlin", 0.5)
    budget.prime("t", "Europe/Berlin", 4.0)  # the stored total of the day wins
    assert not budget.needs_prime("t", "Europe/Berlin")
    assert budget.spent("t", "Europe/Berlin") == 4.0
    budget.prime("t", "Europe/Berlin", 1.0)  # never lowers what was already counted
    assert budget.spent("t", "Europe/Berlin") == 4.0
    assert not budget.allows("t", "Europe/Berlin", 4.0) and budget.allows("t", "Europe/Berlin", 0)
    clock.set(datetime(2026, 9, 30, 10, 0, tzinfo=UTC))
    assert budget.needs_prime("t", "Europe/Berlin") and budget.spent("t", "Europe/Berlin") == 0.0


@pytest.mark.feature("F31")
def test_ids_created_in_the_same_millisecond_keep_their_order() -> None:
    ids = [ulid(1_700_000_000_000) for _ in range(50)]
    assert ids == sorted(ids) and len(set(ids)) == 50
    later = ulid(1_700_000_000_001)
    assert later > ids[-1]
    assert new_id("msg").startswith("msg_") and len(new_id("msg")) == 30


@pytest.mark.feature("F26")
async def test_starlette_socket_wrapper_parses_frames_and_ends_on_disconnect() -> None:
    from starlette.websockets import WebSocketDisconnect

    from concierge.channels.voice.routes import StarletteSocket

    class FakeWebSocket:
        def __init__(self, frames: list[str | Exception]) -> None:
            self.frames = frames
            self.sent: list[str] = []

        async def receive_text(self) -> str:
            item = self.frames.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        async def send_text(self, text: str) -> None:
            self.sent.append(text)

    fake = FakeWebSocket(['{"a": 1}', "not json", "[1, 2]", WebSocketDisconnect()])
    socket = StarletteSocket(fake)  # type: ignore[arg-type]
    assert await socket.receive() == {"a": 1}
    assert await socket.receive() == {} and await socket.receive() == {}
    assert await socket.receive() is None
    await socket.send({"response_type": "ping_pong", "text": "ä"})
    assert fake.sent == ['{"response_type": "ping_pong", "text": "ä"}']
