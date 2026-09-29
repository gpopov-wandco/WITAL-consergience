"""Voice simulators (F26, F29): scripted sockets for tests and a live WebSocket caller.

Implements: F26, F29

``RetellSimClient`` and ``TwilioSimClient`` drive the provider protocol handlers in
process through a scripted socket: no network, no audio, exact message shapes. The
live simulator connects to a running server (``concierge demo voice-sim``) and plays
the Retell custom-LLM client side so a demo can be heard as text.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import websockets

from concierge.channels.voice.retell import RetellProtocol
from concierge.channels.voice.session import VoiceEngine
from concierge.channels.voice.twilio_cr import TwilioRelayProtocol
from concierge.db.models import Tenant


class ScriptedSocket:
    """An in-memory ``VoiceSocket``: the test pushes inbound messages, reads the sent ones."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self._event = asyncio.Event()

    async def receive(self) -> dict[str, Any] | None:
        return await self.inbox.get()

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        self._event.set()

    def push(self, message: dict[str, Any]) -> None:
        self.inbox.put_nowait(message)

    def close(self) -> None:
        self.inbox.put_nowait(None)

    async def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], *, wait_s: float = 5.0
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while True:
            for message in self.sent:
                if predicate(message):
                    return message
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("no matching message from the protocol handler")
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except TimeoutError:
                continue


class RetellSimClient:
    """Plays Retell's side of the custom-LLM protocol against ``RetellProtocol``."""

    def __init__(
        self,
        engine: VoiceEngine,
        tenant: Tenant,
        *,
        call_id: str = "call_sim_1",
        from_number: str | None = "+4915120000009",
        to_number: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.socket = ScriptedSocket()
        self.protocol = RetellProtocol(engine, tenant, self.socket, call_id)
        self.call_id = call_id
        self.from_number = from_number
        self.to_number = to_number or tenant.voice_number
        self.agent_id = agent_id or tenant.voice_agent_id
        self.transcript: list[dict[str, str]] = []
        self.response_id = 0
        self.task: asyncio.Task[None] | None = None
        self.greeting: dict[str, Any] | None = None

    async def __aenter__(self) -> RetellSimClient:
        self.task = asyncio.create_task(self.protocol.run())
        await self.socket.wait_for(lambda m: m.get("response_type") == "config")
        self.socket.push(
            {
                "interaction_type": "call_details",
                "call": {
                    "call_id": self.call_id,
                    "from_number": self.from_number,
                    "to_number": self.to_number,
                    "agent_id": self.agent_id,
                    "direction": "inbound",
                },
            }
        )
        self.greeting = await self.socket.wait_for(
            lambda m: m.get("response_type") == "response" and m.get("response_id") == 0
        )
        if self.greeting.get("content"):
            self.transcript.append({"role": "agent", "content": str(self.greeting["content"])})
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.hangup()

    async def ping(self) -> dict[str, Any]:
        stamp = 1_700_000_000_000 + len(self.socket.sent)
        self.socket.push({"interaction_type": "ping_pong", "timestamp": stamp})
        return await self.socket.wait_for(
            lambda m: m.get("response_type") == "ping_pong" and m.get("timestamp") == stamp
        )

    def speak(self, text: str) -> int:
        """Push a ``response_required`` without waiting (for cancellation tests)."""
        self.response_id += 1
        self.transcript.append({"role": "user", "content": text})
        self.socket.push(
            {
                "interaction_type": "response_required",
                "response_id": self.response_id,
                "transcript": list(self.transcript),
            }
        )
        return self.response_id

    async def say(self, text: str, *, wait_s: float = 8.0) -> list[dict[str, Any]]:
        """One caller utterance; returns every response chunk for its response id."""
        response_id = self.speak(text)
        return await self.responses(response_id, wait_s=wait_s)

    async def remind(self) -> list[dict[str, Any]]:
        self.response_id += 1
        self.socket.push(
            {
                "interaction_type": "reminder_required",
                "response_id": self.response_id,
                "transcript": list(self.transcript),
            }
        )
        return await self.responses(self.response_id)

    async def responses(self, response_id: int, *, wait_s: float = 8.0) -> list[dict[str, Any]]:
        final = await self.socket.wait_for(
            lambda m: (
                m.get("response_type") == "response"
                and m.get("response_id") == response_id
                and bool(m.get("content_complete"))
            ),
            wait_s=wait_s,
        )
        chunks = [
            m
            for m in self.socket.sent
            if m.get("response_type") == "response" and m.get("response_id") == response_id
        ]
        if final.get("content"):
            self.transcript.append({"role": "agent", "content": str(final["content"])})
        return chunks

    async def hangup(self) -> None:
        if self.task is None:
            return
        self.socket.close()
        try:
            await asyncio.wait_for(self.task, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            self.task.cancel()
        self.task = None


class TwilioSimClient:
    """Plays Twilio's side of the ConversationRelay protocol against ``TwilioRelayProtocol``."""

    def __init__(
        self,
        engine: VoiceEngine,
        tenant: Tenant,
        *,
        call_sid: str = "CA_sim_1",
        from_number: str | None = "+4915120000009",
        to_number: str | None = None,
    ) -> None:
        self.socket = ScriptedSocket()
        self.protocol = TwilioRelayProtocol(engine, tenant, self.socket)
        self.call_sid = call_sid
        self.from_number = from_number
        self.to_number = to_number or tenant.voice_number
        self.task: asyncio.Task[None] | None = None
        self.greeting: dict[str, Any] | None = None

    async def __aenter__(self) -> TwilioSimClient:
        self.task = asyncio.create_task(self.protocol.run())
        self.socket.push(
            {
                "type": "setup",
                "callSid": self.call_sid,
                "from": self.from_number,
                "to": self.to_number,
            }
        )
        self.greeting = await self.socket.wait_for(
            lambda m: m.get("type") == "text" and bool(m.get("last"))
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.hangup()

    def prompt(self, text: str, *, last: bool = True) -> None:
        self.socket.push({"type": "prompt", "voicePrompt": text, "lang": "de-DE", "last": last})

    def interrupt(self) -> None:
        self.socket.push({"type": "interrupt", "utteranceUntilInterrupt": ""})

    async def say(self, text: str, *, wait_s: float = 8.0) -> list[dict[str, Any]]:
        """One utterance; returns the messages sent after it up to the final one."""
        start = len(self.socket.sent)
        self.prompt(text)
        await self.socket.wait_for(
            lambda m: (m.get("type") == "text" and bool(m.get("last"))) or m.get("type") == "end",
            wait_s=wait_s,
        )
        # the predicate may match an older message; wait until a new final arrives
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while loop.time() < deadline:
            new = self.socket.sent[start:]
            if any(
                (m.get("type") == "text" and bool(m.get("last"))) or m.get("type") == "end"
                for m in new
            ):
                return new
            await asyncio.sleep(0.01)
        raise TimeoutError("no reply from the relay protocol")

    async def hangup(self) -> None:
        if self.task is None:
            return
        self.socket.close()
        try:
            await asyncio.wait_for(self.task, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            self.task.cancel()
        self.task = None


async def live_call(
    base_url: str,
    public_key: str,
    utterances: list[str],
    *,
    from_number: str = "+4915120000009",
    call_id: str = "call_live_sim",
    wait_s: float = 30.0,
) -> list[tuple[str, str]]:
    """Play a Retell custom-LLM client against a running server; returns (role, text)."""
    ws_base = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    url = f"{ws_base}/voice/retell/llm/{public_key}/{call_id}"
    lines: list[tuple[str, str]] = []
    transcript: list[dict[str, str]] = []
    async with websockets.connect(url) as connection:

        async def wait_response(response_id: int) -> str:
            deadline = asyncio.get_running_loop().time() + wait_s
            while asyncio.get_running_loop().time() < deadline:
                raw = await asyncio.wait_for(connection.recv(), timeout=wait_s)
                message = json.loads(raw)
                if message.get("response_type") == "ping_pong":
                    continue
                if (
                    message.get("response_type") == "response"
                    and message.get("response_id") == response_id
                ):
                    content = str(message.get("content", ""))
                    if content:
                        lines.append(("agent", content))
                    if message.get("content_complete"):
                        return content
            raise TimeoutError("no reply from the server")

        first = json.loads(await asyncio.wait_for(connection.recv(), timeout=wait_s))
        if first.get("response_type") != "config":
            raise RuntimeError(f"unexpected first message {first!r}")
        await connection.send(
            json.dumps(
                {
                    "interaction_type": "call_details",
                    "call": {
                        "call_id": call_id,
                        "from_number": from_number,
                        "direction": "inbound",
                    },
                }
            )
        )
        greeting = await wait_response(0)
        transcript.append({"role": "agent", "content": greeting})
        for index, text in enumerate(utterances, start=1):
            lines.append(("user", text))
            transcript.append({"role": "user", "content": text})
            await connection.send(
                json.dumps(
                    {
                        "interaction_type": "response_required",
                        "response_id": index,
                        "transcript": list(transcript),
                    }
                )
            )
            reply = await wait_response(index)
            transcript.append({"role": "agent", "content": reply})
    return lines
