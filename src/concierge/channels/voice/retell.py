"""Retell adapter (F26): webhook signature, event parsing, custom-LLM protocol, API.

Implements: F26, F31

Signature per the ``retell-sdk`` verifier (see ``docs/VERIFIED_FACTS.md``):
``X-Retell-Signature: v={timestamp_ms},d={hex}`` with HMAC-SHA256 keyed by the API
key over ``body + timestamp``; the compact JSON re-serialisation of the parsed body is
accepted too. The custom-LLM WebSocket protocol echoes ``ping_pong`` at once, answers
``call_details`` with the disclosure (``response_id`` 0) and every
``response_required``/``reminder_required`` with chunks whose ``content_complete`` is
true only on the last one; a newer ``response_id`` abandons the running generation.
Agent creation and number import are REST calls marked VERIFY (docs unreachable in
the build); ``doctor --live`` and ``concierge voice provision`` exercise them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

import httpx2

from concierge.channels.voice.port import (
    CallEnd,
    CallStart,
    ProvisionResult,
    VoiceReply,
    VoiceSocket,
)
from concierge.channels.voice.session import VoiceCallState, VoiceEngine
from concierge.db.models import Tenant
from concierge.log import get_logger
from concierge.tenants.profile import TenantProfile

log = get_logger(__name__)

SIGNATURE_TOLERANCE_MS = 5 * 60 * 1000
REDACTED = "<hidden>"  # printed in place of the SIP password in every payload echo
RETELL_API_BASE = "https://api.retellai.com"
LANGUAGE_CODES = {
    "de": "de-DE",
    "en": "en-US",
    "ru": "ru-RU",
    "uk": "uk-UA",
    "tr": "tr-TR",
    "it": "it-IT",
}


# ------------------------------------------------------------------ signature
def sign(body: bytes, api_key: str, timestamp_ms: int) -> str:
    digest = hmac.new(
        api_key.encode("utf-8"), body + str(timestamp_ms).encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"v={timestamp_ms},d={digest}"


def verify_signature(body: bytes, header: str | None, api_key: str, *, now_ms: int) -> bool:
    """Constant-time check of the raw body (or its compact re-serialisation)."""
    if not header or not api_key:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    raw_ts, digest = parts.get("v", ""), parts.get("d", "")
    if not raw_ts.isdigit() or len(digest) != 64:
        return False
    timestamp_ms = int(raw_ts)
    if abs(now_ms - timestamp_ms) > SIGNATURE_TOLERANCE_MS:
        return False
    candidates = [body]
    try:
        parsed = json.loads(body)
        candidates.append(
            json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
    except ValueError:
        pass
    for candidate in candidates:
        expected = hmac.new(
            api_key.encode("utf-8"), candidate + raw_ts.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, digest):
            return True
    return False


# -------------------------------------------------------------------- events
@dataclass(frozen=True)
class RetellEvent:
    kind: str
    start: CallStart
    end: CallEnd | None = None
    raw: dict[str, Any] | None = None


def parse_event(payload: Any) -> RetellEvent | None:
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("event", ""))
    call = payload.get("call")
    if kind not in {"call_started", "call_ended", "call_analyzed"} or not isinstance(call, dict):
        return None
    call_id = str(call.get("call_id", ""))
    if not call_id:
        return None
    start = CallStart(
        provider="retell",
        call_id=call_id,
        from_number=_opt(call.get("from_number")),
        to_number=_opt(call.get("to_number")),
        agent_id=_opt(call.get("agent_id")),
    )
    end: CallEnd | None = None
    if kind in {"call_ended", "call_analyzed"}:
        started = _ms(call.get("start_timestamp"))
        ended = _ms(call.get("end_timestamp"))
        duration = max(0, (ended - started) // 1000) if started and ended else 0
        transcript = [
            (str(t.get("role", "")), str(t.get("content", "")))
            for t in (call.get("transcript_object") or [])
            if isinstance(t, dict)
        ]
        cost = call.get("call_cost") if isinstance(call.get("call_cost"), dict) else {}
        combined = cost.get("combined_cost") if isinstance(cost, dict) else None
        micro: int | None = None
        if isinstance(combined, int | float):
            micro = round(float(combined) * 10_000)  # Retell reports cents (VERIFY) -> micro EUR
        end = CallEnd(
            provider="retell",
            call_id=call_id,
            duration_s=duration,
            transcript=transcript,
            provider_cost_micro_eur=micro,
            disconnect_reason=str(call.get("disconnection_reason", "")),
        )
    return RetellEvent(kind=kind, start=start, end=end, raw=call)


def _opt(value: Any) -> str | None:
    return str(value) if value else None


def _ms(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


# ------------------------------------------------------------ ws protocol
class RetellProtocol:
    """Runs one custom-LLM WebSocket session against the engine."""

    def __init__(
        self, engine: VoiceEngine, tenant: Tenant, socket: VoiceSocket, call_id: str
    ) -> None:
        self.engine = engine
        self.tenant = tenant
        self.socket = socket
        self.call_id = call_id
        self.state: VoiceCallState | None = None
        self._turns: set[asyncio.Task[str]] = set()

    async def run(self) -> None:
        await self.socket.send(
            {"response_type": "config", "config": {"auto_reconnect": True, "call_details": True}}
        )
        try:
            while True:
                message = await self.socket.receive()
                if message is None:
                    break
                kind = str(message.get("interaction_type", ""))
                if kind == "ping_pong":
                    await self.socket.send(
                        {"response_type": "ping_pong", "timestamp": message.get("timestamp")}
                    )
                    continue
                if kind == "call_details":
                    raw_call = message.get("call")
                    await self._begin(raw_call if isinstance(raw_call, dict) else {})
                    continue
                if kind == "update_only":
                    continue
                if kind in {"response_required", "reminder_required"}:
                    if self.state is None:
                        await self._begin({})
                    state = self.state
                    if state is None or state.ended:
                        continue
                    response_id = _int(message.get("response_id"))
                    utterance = "" if kind == "reminder_required" else _last_user(message)
                    task = asyncio.create_task(
                        self.engine.turn(
                            state,
                            response_id,
                            utterance,
                            lambda reply, rid=response_id: self._send(reply, rid),
                        )
                    )
                    self._turns.add(task)
                    task.add_done_callback(self._turns.discard)
        finally:
            for task in list(self._turns):
                task.cancel()
            if self.state is not None:
                self.engine.cancel(self.state)

    async def _begin(self, call: dict[str, Any]) -> None:
        state, reply = await self.engine.begin(
            self.tenant,
            "retell",
            self.call_id,
            from_number=_opt(call.get("from_number")),
            to_number=_opt(call.get("to_number")),
            agent_id=_opt(call.get("agent_id")),
        )
        self.state = state
        if reply.text or reply.end_call:
            await self._send(reply, 0)

    async def _send(self, reply: VoiceReply, response_id: int) -> None:
        message: dict[str, Any] = {
            "response_type": "response",
            "response_id": response_id,
            "content": reply.text,
            "content_complete": reply.complete,
            "end_call": reply.end_call,
        }
        if reply.transfer_number:
            message["transfer_number"] = reply.transfer_number
        await self.socket.send(message)


def _last_user(message: dict[str, Any]) -> str:
    transcript = message.get("transcript")
    if not isinstance(transcript, list):
        return ""
    for turn in reversed(transcript):
        if isinstance(turn, dict) and turn.get("role") == "user":
            return str(turn.get("content", ""))
    return ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- REST API
def agent_payload(
    profile: TenantProfile, *, websocket_url: str, webhook_url: str
) -> dict[str, Any]:
    """The create-agent body (custom LLM over WebSocket); field names VERIFY."""
    return {
        "agent_name": f"{profile.name} Guest Concierge",
        "response_engine": {"type": "custom-llm", "llm_websocket_url": websocket_url},
        "voice_id": profile.voice.voice_id,
        "language": LANGUAGE_CODES.get(profile.default_language, "de-DE"),
        "webhook_url": webhook_url,
        "begin_message": "",
        "enable_backchannel": False,
        "interruption_sensitivity": 0.7,
        "opt_out_sensitive_data_storage": True,
        "data_storage_setting": "everything_except_pii",
    }


def number_payload(
    profile: TenantProfile, *, agent_id: str, sip_user: str, sip_password: str
) -> dict[str, Any]:
    return {
        "phone_number": profile.voice.number,
        "termination_uri": profile.voice.sip_termination_uri,
        "sip_trunk_auth_username": sip_user,
        "sip_trunk_auth_password": sip_password,
        "inbound_agent_id": agent_id,
        "nickname": profile.slug,
    }


class RetellClient:
    """Minimal REST client for provisioning and reconciliation (endpoints VERIFY)."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = RETELL_API_BASE,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=timeout,
        )

    async def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/create-agent", payload)

    async def import_phone_number(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/import-phone-number", payload)

    async def get_call(self, call_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v2/get-call/{call_id}")
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(path, json=payload)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def aclose(self) -> None:
        await self._client.aclose()


async def provision(
    profile: TenantProfile,
    *,
    api_key: str,
    websocket_url: str,
    webhook_url: str,
    sip_user: str,
    sip_password: str,
    dry_run: bool,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> ProvisionResult:
    """Create the agent and import the SIP number; a dry run only returns the payloads."""
    agent = agent_payload(profile, websocket_url=websocket_url, webhook_url=webhook_url)
    steps = [
        "Retell dashboard: create an API key with the webhook badge and set RETELL_API_KEY.",
        "sipgate (or the carrier): create a SIP trunk for the WITALCO number and store "
        "voice_sip_user / voice_sip_password with `concierge secrets set`.",
        "Hotel: forward the reception line to the WITALCO number on no answer, busy and "
        "out of hours.",
        "Sign the Retell DPA with SCCs and set voice.us_transfer_acknowledged after the "
        "transfer assessment (US processing).",
    ]
    if dry_run or not api_key:
        number = number_payload(
            profile, agent_id="<agent_id>", sip_user="<user>", sip_password=REDACTED
        )
        return ProvisionResult("retell", "", profile.voice.number, [agent, number], steps, False)
    client = RetellClient(api_key, transport=transport)
    try:
        created = await client.create_agent(agent)
        agent_id = str(created.get("agent_id", ""))
        number = number_payload(
            profile, agent_id=agent_id, sip_user=sip_user, sip_password=sip_password
        )
        imported = await client.import_phone_number(number)
        redacted = dict(number, sip_trunk_auth_password=REDACTED)
        return ProvisionResult(
            "retell",
            agent_id,
            str(imported.get("phone_number", profile.voice.number)),
            [agent, redacted],
            steps,
            True,
        )
    finally:
        await client.aclose()
