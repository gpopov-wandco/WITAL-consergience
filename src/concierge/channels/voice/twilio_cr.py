"""Twilio ConversationRelay adapter (F26): request signature, TwiML, relay protocol.

Implements: F26, F31

The signature follows Twilio's request validator (see ``docs/VERIFIED_FACTS.md``):
base64(HMAC-SHA1(auth token, URL + sorted POST parameters)), checked for the URL as
received, without and with an explicit port, and with ``bodySHA256`` for JSON bodies.
The relay WebSocket receives ``setup``, ``prompt`` (``voicePrompt``), ``interrupt``,
``dtmf`` and ``error`` messages and sends ``text`` tokens (``last`` on the final one),
``end`` (with ``handoffData`` for a transfer) and ``language``; the message schema is
VERIFY against the Twilio docs and simulator-tested.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2

from concierge.channels.voice.port import ProvisionResult, VoiceReply, VoiceSocket
from concierge.channels.voice.session import VoiceCallState, VoiceEngine
from concierge.db.models import Tenant
from concierge.log import get_logger
from concierge.tenants.profile import TenantProfile

log = get_logger(__name__)

TWILIO_API_BASE = "https://api.twilio.com"
LANGUAGE_CODES = {
    "de": "de-DE",
    "en": "en-US",
    "ru": "ru-RU",
    "uk": "uk-UA",
    "tr": "tr-TR",
    "it": "it-IT",
}


# ------------------------------------------------------------------ signature
def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _url_variants(url: str) -> list[str]:
    parts = urlsplit(url)
    variants = [url]
    host = parts.hostname or ""
    if parts.port is not None:
        without = parts._replace(netloc=host)
        variants.append(urlunsplit(without))
    else:
        port = 443 if parts.scheme == "https" else 80
        variants.append(urlunsplit(parts._replace(netloc=f"{host}:{port}")))
    return variants


def verify_signature(
    auth_token: str,
    url: str,
    params: Mapping[str, str],
    signature: str | None,
    *,
    body: bytes | None = None,
) -> bool:
    if not signature or not auth_token:
        return False
    if body is not None and "bodySHA256=" in url:
        expected_hash = url.split("bodySHA256=", 1)[1].split("&", 1)[0]
        if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), expected_hash):
            return False
        params = {}
    return any(
        hmac.compare_digest(compute_signature(auth_token, candidate, params), signature)
        for candidate in _url_variants(url)
    )


# ---------------------------------------------------------------------- TwiML
def relay_twiml(
    *,
    websocket_url: str,
    action_url: str,
    language: str,
    status_callback: str = "",
) -> str:
    code = LANGUAGE_CODES.get(language, "de-DE")
    attributes = [
        f"url={quoteattr(websocket_url)}",
        f"language={quoteattr(code)}",
        f"ttsLanguage={quoteattr(code)}",
        f"transcriptionLanguage={quoteattr(code)}",
        'interruptible="true"',
        'dtmfDetection="false"',
    ]
    connect_attrs = f" action={quoteattr(action_url)}"
    if status_callback:
        connect_attrs += f" statusCallback={quoteattr(status_callback)}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"<Connect{connect_attrs}><ConversationRelay {' '.join(attributes)}/></Connect>"
        "</Response>"
    )


def dial_twiml(number: str, *, caller_id: str | None = None) -> str:
    attrs = f" callerId={quoteattr(caller_id)}" if caller_id else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"<Dial{attrs}>{_escape(number)}</Dial></Response>"
    )


def hangup_twiml(text: str = "", *, language: str = "de") -> str:
    say = ""
    if text:
        code = LANGUAGE_CODES.get(language, "de-DE")
        say = f"<Say language={quoteattr(code)}>{_escape(text)}</Say>"
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{say}<Hangup/></Response>'


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def quoteattr(value: str) -> str:
    """A double-quoted XML attribute value (no XML parser involved)."""
    return '"' + _escape(value).replace('"', "&quot;") + '"'


# ------------------------------------------------------------ ws protocol
class TwilioRelayProtocol:
    """Runs one ConversationRelay WebSocket session against the engine."""

    def __init__(self, engine: VoiceEngine, tenant: Tenant, socket: VoiceSocket) -> None:
        self.engine = engine
        self.tenant = tenant
        self.socket = socket
        self.state: VoiceCallState | None = None
        self.response_id = 0
        self._turns: set[asyncio.Task[str]] = set()

    async def run(self) -> None:
        try:
            while True:
                message = await self.socket.receive()
                if message is None:
                    break
                kind = str(message.get("type", ""))
                if kind == "setup":
                    started, reply = await self.engine.begin(
                        self.tenant,
                        "twilio_cr",
                        str(message.get("callSid") or message.get("call_sid") or ""),
                        from_number=_opt(message.get("from")),
                        to_number=_opt(message.get("to")),
                    )
                    self.state = started
                    if reply.text or reply.end_call:
                        await self._send(reply)
                    continue
                state = self.state
                if state is None or state.ended:
                    continue
                if kind == "interrupt":
                    self.engine.cancel(state)
                    continue
                if kind == "prompt":
                    if not bool(message.get("last", True)):
                        continue
                    self.response_id += 1
                    task = asyncio.create_task(
                        self.engine.turn(
                            state,
                            self.response_id,
                            str(message.get("voicePrompt", "")),
                            self._send,
                        )
                    )
                    self._turns.add(task)
                    task.add_done_callback(self._turns.discard)
                elif kind == "error":
                    log.warning(
                        "voice.twilio_error",
                        tenant=self.tenant.id,
                        description=str(message.get("description", ""))[:120],
                    )
        finally:
            for task in list(self._turns):
                task.cancel()
            if self.state is not None:
                self.engine.cancel(self.state)

    async def _send(self, reply: VoiceReply) -> None:
        if reply.language:
            code = LANGUAGE_CODES.get(reply.language)
            if code:
                await self.socket.send(
                    {"type": "language", "ttsLanguage": code, "transcriptionLanguage": code}
                )
        if reply.text:
            await self.socket.send({"type": "text", "token": reply.text, "last": reply.complete})
        if reply.transfer_number:
            await self.socket.send(
                {"type": "end", "handoffData": json.dumps({"transfer": reply.transfer_number})}
            )
        elif reply.end_call:
            await self.socket.send({"type": "end", "handoffData": json.dumps({"reason": "end"})})


def parse_handoff_data(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _opt(value: Any) -> str | None:
    return str(value) if value else None


# ---------------------------------------------------------------- REST API
async def provision(
    profile: TenantProfile,
    *,
    account_sid: str,
    auth_token: str,
    voice_url: str,
    status_url: str,
    dry_run: bool,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> ProvisionResult:
    """Point the tenant's Twilio number at the incoming-call URL (number SID looked up)."""
    steps = [
        "Twilio console: buy or port a German number (regulatory bundle approval time VERIFY).",
        "Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in the environment.",
        "Hotel: forward the reception line to the Twilio number on no answer, busy and "
        "out of hours.",
    ]
    update = {"VoiceUrl": voice_url, "VoiceMethod": "POST", "StatusCallback": status_url}
    if dry_run or not account_sid or not auth_token:
        return ProvisionResult("twilio_cr", "", profile.voice.number, [update], steps, False)
    async with httpx2.AsyncClient(
        base_url=TWILIO_API_BASE, auth=(account_sid, auth_token), transport=transport, timeout=20
    ) as client:
        listed = await client.get(
            f"/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers.json",
            params={"PhoneNumber": profile.voice.number},
        )
        listed.raise_for_status()
        numbers = listed.json().get("incoming_phone_numbers") or []
        if not numbers:
            raise ValueError(f"number {profile.voice.number} is not in the Twilio account")
        sid = str(numbers[0].get("sid", ""))
        updated = await client.post(
            f"/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers/{sid}.json", data=update
        )
        updated.raise_for_status()
    return ProvisionResult("twilio_cr", sid, profile.voice.number, [update], steps, True)
