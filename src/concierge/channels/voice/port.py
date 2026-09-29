"""Voice data types and the socket abstraction (F26).

Implements: F26, 6.2

Provider protocols (Retell, Twilio ConversationRelay) speak JSON over a WebSocket;
``VoiceSocket`` hides the transport so the same handlers run against Starlette in
production and against a scripted in-memory socket in the simulators and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# the name of the tenant secret, not a secret value
VOICE_SOCKET_SECRET = "voice_socket_secret"  # nosec B105
"""Tenant secret that the provider presents in the WebSocket path (set by provisioning)."""


@dataclass(frozen=True)
class CallStart:
    provider: str
    call_id: str
    from_number: str | None = None
    to_number: str | None = None
    agent_id: str | None = None


@dataclass(frozen=True)
class CallEnd:
    provider: str
    call_id: str
    duration_s: int
    transcript: list[tuple[str, str]] = field(default_factory=list)
    provider_cost_micro_eur: int | None = None
    ended_at: datetime | None = None
    disconnect_reason: str = ""


@dataclass
class VoiceReply:
    """One chunk spoken to the caller; ``complete`` is true only on the last chunk."""

    text: str
    complete: bool = True
    end_call: bool = False
    transfer_number: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class ProvisionResult:
    provider: str
    agent_id: str
    number: str
    payloads: list[dict[str, Any]]
    human_steps: list[str]
    applied: bool


class VoiceSocket(Protocol):
    async def receive(self) -> dict[str, Any] | None:
        """The next JSON message, or None once the peer closed the socket."""
        ...

    async def send(self, message: dict[str, Any]) -> None: ...
