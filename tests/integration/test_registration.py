"""Registration module (F15): closed field set, signed links, arrival-day gate with
FakeProof, deletion after the retention period, export, the hosted form and the Stripe
adapter against httpx2.MockTransport."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest
from sqlalchemy import select

from concierge.app import create_app
from concierge.channels.whatsapp.fake import FakeGraph
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.db.models import DeletionLog, Meldeschein, PaymentProof, Tenant
from concierge.db.session import Database
from concierge.demo.seed import seed_demo
from concierge.registration.proof import FakeProof
from concierge.registration.service import FIELDS, MeldescheinData, RegistrationError, load_texts
from concierge.registration.stripe_proof import StripeProof, StripeProofError
from concierge.reservations.service import ReservationInput
from concierge.security.crypto import Keyring
from concierge.security.signatures import sign_stripe
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile

pytestmark = pytest.mark.feature("F15")

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
RAW = {
    "arrival": "2026-10-06",
    "departure": "2026-10-08",
    "family_name": "Muster",
    "first_name": "Anna",
    "birth_date": "1990-05-04",
    "nationalities": "at, IT",
    "address_street": "Beispielweg 3",
    "address_postal_code": "1010",
    "address_city": "Wien",
    "address_country": "at",
    "companions_count": "1",
    "companions_nationality": "AT",
    "id_document_number": "P1234567",
}


@pytest.fixture
async def stack(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> tuple[Services, Tenant, FakeProof]:
    clock.set(NOW)
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    proof = FakeProof(settings.public_base_url())
    services = build_services(ctx, llm=None, wa_port=FakeGraph(), demo=True, proof=proof)
    await seed_demo(services)
    tenant = await services.tenants.require("hotel-beispiel")
    return services, tenant, proof


async def _reservation(services: Services, tenant: Tenant, **kw: Any) -> Any:
    base: dict[str, Any] = {
        "res_id": "REG-1",
        "guest_name": "Anna Muster",
        "phone": "+4915120000009",
        "lang": "de",
        "arrival": date(2026, 10, 6),
        "departure": date(2026, 10, 8),
        "adults": 2,
    }
    base.update(kw)
    return (
        await services.reservations.upsert(tenant.id, ReservationInput(**base), actor="test")
    ).reservation


def test_closed_field_set() -> None:
    data = MeldescheinData.model_validate(RAW)
    assert data.nationalities == ["AT", "IT"] and data.address_country == "AT"
    assert set(MeldescheinData.model_fields) == set(FIELDS)
    with pytest.raises(ValueError, match="extra"):
        MeldescheinData.model_validate({**RAW, "email": "a@example.invalid"})
    with pytest.raises(ValueError, match="departure"):
        MeldescheinData.model_validate({**RAW, "departure": "2026-10-06"})
    with pytest.raises(ValueError, match="nationalit"):
        MeldescheinData.model_validate({**RAW, "nationalities": "Austria"})
    with pytest.raises(ValueError, match="companions"):
        MeldescheinData.model_validate({**RAW, "companions_nationality": ""})
    texts = load_texts()
    assert texts.status == "draft_unreviewed"
    for lang in ("de", "en", "ru", "uk", "tr", "it"):
        block = texts.lang(lang)
        assert set(block["fields"]) == set(FIELDS) and "{arrival}" in block["not_arrival_day"]
        assert "compliant" not in json.dumps(block).lower()


async def test_link_save_confirm_gate_export_and_purge(
    stack: tuple[Services, Tenant, FakeProof], clock: FakeClock
) -> None:
    services, tenant, proof = stack
    registration = services.registration
    reservation = await _reservation(services, tenant)
    profile = TenantProfile.model_validate(tenant.profile)
    link = registration.link(tenant, reservation)
    assert link.startswith("http://127.0.0.1:8000/r/")
    resolved_tenant, resolved = await registration.resolve(link.rsplit("/", 1)[1])
    assert resolved_tenant.id == tenant.id and resolved.id == reservation.id
    with pytest.raises(RegistrationError) as bad:
        await registration.resolve("not-a-token")
    assert bad.value.code == "invalid_link"
    row = await registration.save(
        tenant, profile, reservation, registration.parse(RAW), actor="guest"
    )
    assert row.delete_after == date(2026, 10, 8) + timedelta(days=380)
    assert b"Muster" not in row.data_enc and registration.decrypt(row)["family_name"] == "Muster"
    with pytest.raises(RegistrationError) as early:
        await registration.start_confirmation(tenant, profile, row, success_url="s", cancel_url="c")
    assert early.value.code == "not_arrival_day" and "06.10.2026" in str(early.value)
    clock.set(datetime(2026, 10, 5, 23, 30, tzinfo=UTC))  # 01:30 local on 6 October
    session = await registration.start_confirmation(
        tenant, profile, row, success_url="s", cancel_url="c"
    )
    assert session.redirect_url.startswith("http://127.0.0.1:8000/r/proof/fake/")
    proof_id = session.redirect_url.rsplit("/", 1)[1]
    confirmed = await registration.complete(proof.completion(proof_id))
    assert confirmed is not None and confirmed.confirmed_at == clock.now() and proof.released
    assert (await registration.complete(proof.completion(proof_id))) is not None
    with pytest.raises(RegistrationError) as locked:
        await registration.save(
            tenant, profile, reservation, registration.parse(RAW), actor="guest"
        )
    assert locked.value.code == "confirmed"
    rows = await registration.export(tenant.id, date(2026, 10, 1), date(2026, 10, 31))
    assert (
        len(rows) == 1
        and rows[0]["family_name"] == "Muster"
        and rows[0]["nationalities"] == ["AT", "IT"]
    )
    assert (
        rows[0]["confirmation"]["assignment_number"].startswith("fake_pm_")
        and rows[0]["confirmed_at"]
    )
    assert set(rows[0]) >= set(FIELDS)
    assert await registration.purge(today=date(2027, 10, 22)) == 0
    assert await registration.purge(today=date(2027, 10, 23)) == 1
    async with services.ctx.db.scope() as db_session:
        assert (await db_session.execute(select(Meldeschein))).scalars().all() == []
        assert (await db_session.execute(select(PaymentProof))).scalars().all() == []
        logs = (await db_session.execute(select(DeletionLog))).scalars().all()
    assert (
        len(logs) == 1
        and logs[0].table_name == "meldeschein"
        and logs[0].row_count == 1
        and logs[0].reason == "retention"
    )
    disabled = profile.model_copy(
        update={"registration": profile.registration.model_copy(update={"enabled": False})}
    )
    with pytest.raises(RegistrationError) as off:
        await registration.save(
            tenant, disabled, reservation, registration.parse(RAW), actor="guest"
        )
    assert off.value.code == "disabled"
    assert await registration.link_builder(tenant, disabled, reservation) is None


async def test_hosted_form_flow(
    stack: tuple[Services, Tenant, FakeProof], clock: FakeClock
) -> None:
    services, tenant, _proof = stack
    reservation = await _reservation(services, tenant, res_id="REG-2", lang="en")
    app = create_app(services, start_worker=False)
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    )
    link = services.registration.link(tenant, reservation)
    path = link.replace("http://127.0.0.1:8000", "")
    page = await client.get(path)
    assert (
        page.status_code == 200
        and "Pre-check-in registration form" in page.text
        and "draft_unreviewed" in page.text
    )
    assert 'name="id_document_number"' in page.text and 'name="email"' not in page.text
    invalid = await client.get("/r/garbage")
    assert invalid.status_code == 404
    bad = await client.post(path, data={**RAW, "departure": "2026-10-06"})
    assert bad.status_code == 200 and "departure" in bad.text
    saved = await client.post(path, data=RAW)
    assert saved.status_code == 303
    page = await client.get(path)
    assert "Your details are saved" in page.text and 'value="Muster"' in page.text
    refused = await client.post(f"{path}/confirm")
    assert refused.status_code == 200 and "day of arrival only" in refused.text
    clock.set(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))
    started = await client.post(f"{path}/confirm")
    assert started.status_code == 303 and "/r/proof/fake/" in started.headers["location"]
    callback = await client.get(started.headers["location"].replace("http://127.0.0.1:8000", ""))
    assert callback.status_code == 200 and callback.json()["confirmed"] is True
    page = await client.get(f"{path}/done")
    assert "The registration form is confirmed" in page.text
    assert (await client.get("/r/proof/fake/proof_nope")).status_code == 404
    await client.aclose()


async def test_stripe_adapter_and_webhook(
    stack: tuple[Services, Tenant, FakeProof], clock: FakeClock
) -> None:
    services, tenant, _proof = stack
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "Bearer sk_test_x"
        body = parse_qs(request.content.decode())
        calls.append((request.url.path, body))
        if request.url.path == "/v1/checkout/sessions":
            return httpx2.Response(
                200,
                json={
                    "id": "cs_test_1",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_1",
                    "payment_intent": "pi_1",
                },
            )
        if request.url.path == "/v1/checkout/sessions/cs_test_1":
            return httpx2.Response(200, json={"id": "cs_test_1", "payment_intent": "pi_1"})
        if request.url.path == "/v1/payment_intents/pi_1/cancel":
            return httpx2.Response(200, json={"id": "pi_1", "status": "canceled"})
        if request.url.path == "/v1/payment_intents/pi_bad/cancel":
            return httpx2.Response(
                402, json={"error": {"code": "card_declined", "type": "card_error"}}
            )
        return httpx2.Response(404, json={"error": {"type": "invalid_request_error"}})

    stripe = StripeProof("sk_test_x", transport=httpx2.MockTransport(handler))
    assert stripe.test_mode
    session = await stripe.start(
        proof_id="proof_1",
        amount_cents=100,
        currency="EUR",
        description="Hotel Beispiel: Meldeschein",
        success_url="https://x/s",
        cancel_url="https://x/c",
    )
    assert session.session_ref == "cs_test_1" and session.redirect_url.startswith(
        "https://checkout.stripe.com/"
    )
    body = calls[0][1]
    assert body["mode"] == ["payment"] and body["payment_intent_data[capture_method]"] == ["manual"]
    assert body["line_items[0][price_data][unit_amount]"] == ["100"] and body[
        "client_reference_id"
    ] == ["proof_1"]
    assert body["payment_method_options[card][request_three_d_secure]"] == ["any"]
    await stripe.release("cs_test_1")
    assert [c[0] for c in calls[1:]] == [
        "/v1/checkout/sessions/cs_test_1",
        "/v1/payment_intents/pi_1/cancel",
    ]
    with pytest.raises(StripeProofError, match="card_declined"):
        await stripe.release("pi_bad")
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "client_reference_id": "proof_1",
                "payment_intent": {"id": "pi_1", "payment_method": "pm_123"},
            }
        },
    }
    completion = StripeProof.parse_event(json.dumps(event).encode())
    assert (
        completion is not None
        and completion.assignment_number == "pm_123"
        and completion.payment_intent == "pi_1"
    )
    assert StripeProof.parse_event(b'{"type": "payment_intent.created"}') is None
    assert StripeProof.parse_event(b"nope") is None
    # the signed webhook end to end with a pending proof
    reservation = await _reservation(services, tenant, res_id="REG-3")
    profile = TenantProfile.model_validate(tenant.profile)
    registration = services.registration
    row = await registration.save(
        tenant, profile, reservation, registration.parse(RAW), actor="guest"
    )
    clock.set(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))
    started = await registration.start_confirmation(
        tenant, profile, row, success_url="s", cancel_url="c"
    )
    proof_id = started.redirect_url.rsplit("/", 1)[1]
    secret = "whsec_test"
    services.ctx.settings.stripe_webhook_secret = type(services.ctx.settings.stripe_webhook_secret)(
        secret
    )
    app = create_app(services, start_worker=False)
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    )
    payload = json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_9",
                    "client_reference_id": proof_id,
                    "payment_intent": {"id": "pi_9", "payment_method": "pm_9"},
                }
            },
        }
    ).encode()
    unsigned = await client.post(
        "/r/psp/webhook", content=payload, headers={"content-type": "application/json"}
    )
    assert unsigned.status_code == 401
    header = sign_stripe(payload, secret.encode(), int(clock.now().timestamp()))
    signed = await client.post(
        "/r/psp/webhook",
        content=payload,
        headers={"content-type": "application/json", "stripe-signature": header},
    )
    assert signed.status_code == 200 and signed.json() == {"received": True, "handled": True}
    confirmed = await registration.get(tenant.id, reservation.id)
    assert confirmed is not None and confirmed.confirmed_at is not None
    stored = await registration.proof_for(tenant.id, confirmed.id)
    assert stored is not None and stored.status == "confirmed"
    assert (
        services.ctx.keyring.decrypt_str(
            stored.assignment_number_enc or b"", tenant.id, "assignment_number"
        )
        == "pm_9"
    )
    await client.aclose()


async def test_a_stripe_tenant_never_confirms_through_the_fake_in_production(
    stack: tuple[Services, Tenant, FakeProof], clock: FakeClock
) -> None:
    services, tenant, _proof = stack
    registration = services.registration
    reservation = await _reservation(services, tenant)
    profile = TenantProfile.model_validate(tenant.profile)
    stripe_profile = profile.model_copy(
        update={"registration": profile.registration.model_copy(update={"psp": "stripe"})}
    )
    row = await registration.save(
        tenant, profile, reservation, registration.parse(RAW), actor="guest"
    )
    clock.set(datetime(2026, 10, 5, 23, 30, tzinfo=UTC))  # the arrival day, local time
    registration.allow_fake_proof = False  # what production sets
    with pytest.raises(RegistrationError) as refused:
        await registration.start_confirmation(
            tenant, stripe_profile, row, success_url="s", cancel_url="c"
        )
    assert refused.value.code == "psp_unavailable"
    session = await registration.start_confirmation(
        tenant, profile, row, success_url="s", cancel_url="c"
    )  # psp fake stays allowed: the profile chose it
    assert session.redirect_url.startswith("http://127.0.0.1:8000/r/proof/fake/")
