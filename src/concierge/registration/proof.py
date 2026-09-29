"""The payment-proof port (F15): strong customer authentication as the electronic signature.

Implements: F15, 6.2

``PaymentProofPort.start`` opens a hosted checkout with a small pre-authorisation and
returns where to send the guest; the provider's confirmation (webhook or the fake's
callback) carries the instrument's assignment number, which is stored with the
Meldeschein data. ``release`` cancels the pre-authorisation. No card data ever reaches
this service. Whether a per-card token counts as the "Zuordnungsnummer" is VERIFY
(PSP and lawyer), recorded in the compliance pack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ProofSession:
    session_ref: str
    redirect_url: str


@dataclass(frozen=True)
class ProofCompletion:
    proof_id: str
    session_ref: str
    assignment_number: str
    payment_intent: str = ""


class PaymentProofPort(Protocol):
    name: str

    async def start(
        self,
        *,
        proof_id: str,
        amount_cents: int,
        currency: str,
        description: str,
        success_url: str,
        cancel_url: str,
    ) -> ProofSession: ...

    async def release(self, session_ref: str) -> None: ...


@dataclass
class FakeProof:
    """Completes on a callback the hosted form calls; demo and tests only."""

    base_url: str
    name: str = "fake"
    sessions: dict[str, ProofSession] = field(default_factory=dict)
    released: list[str] = field(default_factory=list)
    counter: int = 0

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
        self.counter += 1
        session = ProofSession(
            session_ref=f"fake_cs_{self.counter}",
            redirect_url=f"{self.base_url}/r/proof/fake/{proof_id}",
        )
        self.sessions[proof_id] = session
        return session

    async def release(self, session_ref: str) -> None:
        self.released.append(session_ref)

    def completion(self, proof_id: str) -> ProofCompletion:
        session = self.sessions[proof_id]
        return ProofCompletion(
            proof_id=proof_id,
            session_ref=session.session_ref,
            assignment_number=f"fake_pm_{proof_id[-8:]}",
            payment_intent=f"fake_pi_{proof_id[-8:]}",
        )
