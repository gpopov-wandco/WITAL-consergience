"""The ``WhatsAppPort``: what the channel needs from the Graph API.

Implements: 6.2, 6.6, F14, F18

The port is deliberately small: it sends one Graph ``messages`` payload, marks a
message read, resolves and downloads media, and reads phone, subscription and
template information. Access tokens are per tenant and passed per call. The real
adapter is :mod:`concierge.channels.whatsapp.graph`; the fake is
:mod:`concierge.channels.whatsapp.fake`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class GraphError(Exception):
    """A Graph API error with Meta's code and, when present, subcode and retry hint."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        subcode: int | None = None,
        status: int | None = None,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.subcode = subcode
        self.status = status
        self.retry_after = retry_after
        self.details = details or {}

    @property
    def is_network(self) -> bool:
        return self.code == -1


@dataclass(frozen=True)
class SendResult:
    ok: bool
    wamid: str | None = None
    error: GraphError | None = None
    unknown: bool = False
    """``unknown`` means the request timed out: the message may or may not have been sent."""


@dataclass(frozen=True)
class MediaInfo:
    url: str
    mime_type: str
    sha256: str
    file_size: int


@dataclass
class TemplateRecord:
    name: str
    language: str
    category: str
    status: str
    id: str = ""
    reason: str = ""
    components: list[dict[str, Any]] = field(default_factory=list)


class WhatsAppPort(Protocol):
    async def send_message(
        self, phone_number_id: str, payload: dict[str, Any], *, access_token: str
    ) -> SendResult: ...

    async def mark_read(
        self, phone_number_id: str, message_id: str, *, access_token: str, typing: bool = False
    ) -> None: ...

    async def media_info(self, media_id: str, *, access_token: str) -> MediaInfo: ...

    async def download_media(self, url: str, *, access_token: str, max_bytes: int) -> bytes: ...

    async def phone_info(self, phone_number_id: str, *, access_token: str) -> dict[str, Any]: ...

    async def subscribed_apps(self, waba_id: str, *, access_token: str) -> list[dict[str, Any]]: ...

    async def list_templates(self, waba_id: str, *, access_token: str) -> list[TemplateRecord]: ...

    async def create_template(
        self, waba_id: str, template: dict[str, Any], *, access_token: str
    ) -> TemplateRecord: ...
