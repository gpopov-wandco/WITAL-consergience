"""Stripe hosted Checkout as the payment-proof provider (F15).

Implements: F15, C3, C4

A Checkout Session in payment mode with manual capture pre-authorises
``preauth_cents`` (default 100) with 3-D Secure requested, so the guest authenticates
strongly; ``client_reference_id`` carries our proof id. The ``checkout.session.completed``
webhook (signed with ``STRIPE_WEBHOOK_SECRET``) reports the payment method, which is
stored as the assignment number, and the PaymentIntent is cancelled afterwards to
release the hold. Endpoint shapes follow the Stripe API as of the build (VERIFY with
the PSP: the mapping of a card token to the "Zuordnungsnummer", the amount and the
release; see docs/VERIFIED_FACTS.md).
"""

from __future__ import annotations

import json
from typing import Any

import httpx2

from concierge.registration.proof import ProofCompletion, ProofSession

DEFAULT_BASE_URL = "https://api.stripe.com"  # VERIFY


class StripeProofError(RuntimeError):
    pass


class StripeProof:
    name = "stripe"

    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout_s = timeout_s

    @property
    def test_mode(self) -> bool:
        return self._secret_key.startswith(("sk_test_", "rk_test_"))

    async def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self.timeout_s}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            async with httpx2.AsyncClient(**kwargs) as client:
                response = await client.post(
                    f"{self.base_url}{path}",
                    data=data,
                    headers={"Authorization": f"Bearer {self._secret_key}"},
                )
        except httpx2.HTTPError as exc:
            raise StripeProofError(f"stripe transport error: {type(exc).__name__}") from exc
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise StripeProofError("stripe body is not JSON") from exc
        if response.status_code >= 400 or not isinstance(payload, dict):
            code = ""
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                code = str(payload["error"].get("code") or payload["error"].get("type") or "")
            raise StripeProofError(f"stripe error {response.status_code} {code}".strip())
        return payload

    async def start(
        self,
        *,
        proof_id: str,
        amount_cents: int,
        currency: str,
        description: str,
        success_url: str,
        cancel_url: str,
    ) -> ProofSession:
        data = {
            "mode": "payment",
            "client_reference_id": proof_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "payment_intent_data[capture_method]": "manual",
            "payment_intent_data[description]": description[:200],
            "payment_method_options[card][request_three_d_secure]": "any",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(max(amount_cents, 50)),
            "line_items[0][price_data][product_data][name]": description[:120],
        }
        payload = await self._post("/v1/checkout/sessions", data)
        session_id = str(payload.get("id", ""))
        url = str(payload.get("url", ""))
        if not session_id or not url:
            raise StripeProofError("stripe session lacks id or url")
        return ProofSession(session_ref=session_id, redirect_url=url)

    async def release(self, session_ref: str) -> None:
        """Cancel the pre-authorisation of a completed session (its PaymentIntent)."""
        if session_ref.startswith("pi_"):
            await self._post(f"/v1/payment_intents/{session_ref}/cancel", {})
            return
        session = await self._post(f"/v1/checkout/sessions/{session_ref}", {})
        intent = session.get("payment_intent")
        if isinstance(intent, str) and intent:
            await self._post(f"/v1/payment_intents/{intent}/cancel", {})

    @staticmethod
    def parse_event(payload: bytes) -> ProofCompletion | None:
        """The completion carried by a verified ``checkout.session.completed`` event."""
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict) or event.get("type") != "checkout.session.completed":
            return None
        data = event.get("data", {})
        session = data.get("object", {}) if isinstance(data, dict) else {}
        if not isinstance(session, dict):
            return None
        proof_id = str(session.get("client_reference_id") or "")
        intent = session.get("payment_intent")
        intent_id = intent if isinstance(intent, str) else str((intent or {}).get("id", ""))
        method = ""
        if isinstance(intent, dict):
            method = str(intent.get("payment_method") or "")
        if not method:
            method = str(session.get("payment_method") or intent_id)
        if not proof_id:
            return None
        return ProofCompletion(
            proof_id=proof_id,
            session_ref=str(session.get("id", "")),
            assignment_number=method,
            payment_intent=intent_id,
        )
