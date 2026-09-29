"""In-process fake of the WhatsApp port for tests and the offline demo.

Implements: F29, 10.9

Records every send, hands out sequential message ids and injects Meta error codes
(131047 window closed, 130429 throttled, 190 token invalid) or timeouts on demand.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from concierge.channels.whatsapp.port import GraphError, MediaInfo, SendResult, TemplateRecord


@dataclass
class SentMessage:
    phone_number_id: str
    payload: dict[str, Any]
    wamid: str

    @property
    def to(self) -> str:
        return str(self.payload.get("to", ""))

    @property
    def text(self) -> str:
        kind = self.payload.get("type")
        if kind == "text":
            return str(self.payload.get("text", {}).get("body", ""))
        if kind == "interactive":
            return str(self.payload.get("interactive", {}).get("body", {}).get("text", ""))
        if kind == "template":
            return "template:" + str(self.payload.get("template", {}).get("name", ""))
        return ""


@dataclass
class FakeGraph:
    sent: list[SentMessage] = field(default_factory=list)
    read_marks: list[tuple[str, str, bool]] = field(default_factory=list)
    templates: list[TemplateRecord] = field(default_factory=list)
    media: dict[str, tuple[MediaInfo, bytes]] = field(default_factory=dict)
    phone: dict[str, Any] = field(
        default_factory=lambda: {
            "display_phone_number": "+49 69 000000",
            "verified_name": "Hotel Beispiel",
            "quality_rating": "GREEN",
        }
    )
    _errors: deque[int | str] = field(default_factory=deque)
    _counter: int = 0

    def inject_error(self, code: int | str, times: int = 1) -> None:
        """Queue an error for the next sends: a Meta code, or ``"timeout"``."""
        for _ in range(times):
            self._errors.append(code)

    def messages_to(self, to: str) -> list[SentMessage]:
        return [m for m in self.sent if m.to == to]

    async def send_message(
        self, phone_number_id: str, payload: dict[str, Any], *, access_token: str
    ) -> SendResult:
        if not access_token:
            return SendResult(ok=False, error=GraphError(190, "missing token", status=401))
        if self._errors:
            code = self._errors.popleft()
            if code == "timeout":
                return SendResult(
                    ok=False,
                    error=GraphError(-1, "timeout", details={"timeout": True}),
                    unknown=True,
                )
            if code == 131047:
                return SendResult(
                    ok=False,
                    error=GraphError(
                        131047,
                        "Re-engagement message",
                        status=400,
                        details={
                            "details": "Message failed to send because more than 24 hours have passed since the customer last replied to this number."
                        },
                    ),
                )
            if code == 130429:
                return SendResult(
                    ok=False,
                    error=GraphError(130429, "Rate limit hit", status=429, retry_after=2.0),
                )
            if code == 190:
                return SendResult(
                    ok=False, error=GraphError(190, "Invalid OAuth access token", status=401)
                )
            return SendResult(
                ok=False, error=GraphError(int(code), f"injected error {code}", status=400)
            )
        self._counter += 1
        wamid = f"wamid.fake.{self._counter}"
        self.sent.append(SentMessage(phone_number_id, dict(payload), wamid))
        return SendResult(ok=True, wamid=wamid)

    async def mark_read(
        self, phone_number_id: str, message_id: str, *, access_token: str, typing: bool = False
    ) -> None:
        self.read_marks.append((phone_number_id, message_id, typing))

    async def media_info(self, media_id: str, *, access_token: str) -> MediaInfo:
        if media_id not in self.media:
            raise GraphError(100, "Unsupported get request: media not found", status=400)
        return self.media[media_id][0]

    async def download_media(self, url: str, *, access_token: str, max_bytes: int) -> bytes:
        for info, data in self.media.values():
            if info.url == url:
                if len(data) > max_bytes:
                    raise GraphError(-4, "media larger than the allowed size")
                return data
        raise GraphError(-3, "media download failed: HTTP 404", status=404)

    def add_media(self, media_id: str, data: bytes, mime_type: str) -> MediaInfo:
        info = MediaInfo(
            url=f"https://lookaside.fake/{media_id}",
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            file_size=len(data),
        )
        self.media[media_id] = (info, data)
        return info

    async def phone_info(self, phone_number_id: str, *, access_token: str) -> dict[str, Any]:
        if not access_token:
            raise GraphError(190, "Invalid OAuth access token", status=401)
        return {"id": phone_number_id, **self.phone}

    async def subscribed_apps(self, waba_id: str, *, access_token: str) -> list[dict[str, Any]]:
        return [{"whatsapp_business_api_data": {"id": "app", "name": "Guest Concierge"}}]

    async def list_templates(self, waba_id: str, *, access_token: str) -> list[TemplateRecord]:
        return list(self.templates)

    async def create_template(
        self, waba_id: str, template: dict[str, Any], *, access_token: str
    ) -> TemplateRecord:
        record = TemplateRecord(
            name=str(template["name"]),
            language=str(template["language"]),
            category=str(template["category"]),
            status="PENDING",
            id=f"tpl_{len(self.templates) + 1}",
            components=list(template.get("components", [])),
        )
        self.templates.append(record)
        return record
