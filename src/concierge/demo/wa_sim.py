"""WhatsApp simulator: build and sign webhook payloads, post them to the service (F29).

Implements: F29, 10.3, 10.4

Payload builders cover text, button and list replies, media, location, statuses and
duplicates. ``WaSimClient`` signs with the tenant's app secret and posts to a running
service over HTTP (the CLI) or straight into the ingress (the demo page and tests).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx2

from concierge.security.signatures import sign_meta


def _envelope(
    phone_number_id: str, value: dict[str, Any], field: str = "messages", waba_id: str = "demo-waba"
) -> dict[str, Any]:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "4969000000", "phone_number_id": phone_number_id},
        **value,
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": waba_id, "changes": [{"field": field, "value": value}]}],
    }


@dataclass
class PayloadFactory:
    phone_number_id: str
    waba_id: str = "demo-waba"
    counter: int = 0

    def _wamid(self) -> str:
        self.counter += 1
        return f"wamid.sim.{int(time.time() * 1000)}.{self.counter}"

    def _message(
        self, sender: str, body: dict[str, Any], *, name: str | None, ts: int | None
    ) -> dict[str, Any]:
        contacts = [{"profile": {"name": name}, "wa_id": sender}] if name else []
        message = {
            "from": sender,
            "id": self._wamid(),
            "timestamp": str(ts or int(time.time())),
            **body,
        }
        return _envelope(
            self.phone_number_id,
            {"contacts": contacts, "messages": [message]},
            waba_id=self.waba_id,
        )

    def text(
        self, sender: str, text: str, *, name: str | None = "Sim Guest", ts: int | None = None
    ) -> dict[str, Any]:
        return self._message(sender, {"type": "text", "text": {"body": text}}, name=name, ts=ts)

    def button(
        self, sender: str, button_id: str, title: str, *, name: str | None = "Sim Guest"
    ) -> dict[str, Any]:
        return self._message(
            sender,
            {
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": button_id, "title": title},
                },
            },
            name=name,
            ts=None,
        )

    def list_reply(
        self, sender: str, row_id: str, title: str, *, name: str | None = "Sim Guest"
    ) -> dict[str, Any]:
        return self._message(
            sender,
            {
                "type": "interactive",
                "interactive": {
                    "type": "list_reply",
                    "list_reply": {"id": row_id, "title": title, "description": ""},
                },
            },
            name=name,
            ts=None,
        )

    def media(
        self,
        sender: str,
        kind: str = "image",
        media_id: str = "sim-media-1",
        *,
        name: str | None = "Sim Guest",
    ) -> dict[str, Any]:
        return self._message(
            sender,
            {
                "type": kind,
                kind: {"id": media_id, "mime_type": "image/jpeg", "sha256": "", "caption": ""},
            },
            name=name,
            ts=None,
        )

    def location(
        self,
        sender: str,
        latitude: float = 50.11,
        longitude: float = 8.68,
        *,
        name: str | None = "Sim Guest",
    ) -> dict[str, Any]:
        return self._message(
            sender,
            {
                "type": "location",
                "location": {
                    "latitude": latitude,
                    "longitude": longitude,
                    "name": "Messe Frankfurt",
                },
            },
            name=name,
            ts=None,
        )

    def status(
        self,
        wamid: str,
        status: str,
        recipient: str,
        *,
        billable: bool = True,
        category: str = "service",
    ) -> dict[str, Any]:
        entry = {
            "id": wamid,
            "status": status,
            "timestamp": str(int(time.time())),
            "recipient_id": recipient,
            "conversation": {"id": "sim-conv", "origin": {"type": category}},
            "pricing": {
                "billable": billable,
                "pricing_model": "PMP",
                "category": category,
                "type": "regular",
            },
        }
        return _envelope(self.phone_number_id, {"statuses": [entry]}, waba_id=self.waba_id)

    def template_status(self, name: str, language: str, event: str) -> dict[str, Any]:
        return _envelope(
            self.phone_number_id,
            {
                "event": event,
                "message_template_id": 1,
                "message_template_name": name,
                "message_template_language": language,
            },
            field="message_template_status_update",
            waba_id=self.waba_id,
        )


def sign(payload: dict[str, Any], app_secret: str) -> tuple[bytes, str]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return raw, sign_meta(raw, app_secret)


class WaSimClient:
    """Post signed webhooks to a running service."""

    def __init__(
        self, base_url: str, app_secret: str, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.app_secret = app_secret
        kwargs: dict[str, Any] = {"timeout": 10.0}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx2.AsyncClient(**kwargs)

    async def post(self, payload: dict[str, Any]) -> httpx2.Response:
        raw, signature = sign(payload, self.app_secret)
        return await self._client.post(
            f"{self.base_url}/wa/webhook",
            content=raw,
            headers={"content-type": "application/json", "x-hub-signature-256": signature},
        )

    async def sent(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/mock-graph/_sent")
        return dict(response.json())

    async def inject(self, code: int | str, times: int = 1) -> None:
        await self._client.post(
            f"{self.base_url}/mock-graph/_inject", json={"code": code, "times": times}
        )

    async def aclose(self) -> None:
        await self._client.aclose()
