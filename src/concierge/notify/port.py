"""The e-mail port (F18) and the in-memory fake used by tests and the demo.

Implements: F18, F13, 6.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    reply_to: str | None = None
    idempotency_key: str | None = None
    tenant_id: str | None = None


@dataclass(frozen=True)
class EmailResult:
    ok: bool
    provider_id: str = ""
    error: str = ""
    retryable: bool = False


class EmailPort(Protocol):
    name: str

    async def send(self, message: EmailMessage, *, sender: str) -> EmailResult: ...


@dataclass
class FakeEmail:
    name: str = "fake"
    sent: list[EmailMessage] = field(default_factory=list)
    fail_next: int = 0

    async def send(self, message: EmailMessage, *, sender: str) -> EmailResult:
        if self.fail_next > 0:
            self.fail_next -= 1
            return EmailResult(ok=False, error="injected failure", retryable=True)
        self.sent.append(message)
        return EmailResult(ok=True, provider_id=f"fake-{len(self.sent)}")

    def to(self, address: str) -> list[EmailMessage]:
        return [m for m in self.sent if m.to == address]
