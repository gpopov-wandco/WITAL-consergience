"""The provider-agnostic call engine (F26): disclosure, turns, filler, cancellation.

Implements: F26, C7, C8, C16, StGB § 201 (no audio recording), AI Act Art. 50(1)

One ``VoiceCallState`` per live call. ``begin`` speaks the disclosure first (it is
recorded as the first outbound message and cannot be skipped: no turn starts without
it). ``turn`` runs the Brain as a task, speaks a static filler after ``filler_ms`` and
the whole validated reply when it is ready; a newer response id cancels the running
generation. "Person" requests transfer during staffed hours and otherwise capture a
callback; a caller who declines the assistant gets a callback without transcript
retention. Audio is never handled here: only text reaches this module.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from concierge.agent.brain import Brain, BrainResult
from concierge.agent.guardrails import Guardrails
from concierge.agent.texts import GuestTexts
from concierge.channels.voice.port import VoiceReply
from concierge.channels.voice.speech import for_speech
from concierge.clock import Clock
from concierge.conversations.service import ConversationService, Inbound
from concierge.db.models import Tenant, VoiceCall
from concierge.db.session import Database
from concierge.handoff.requests import RequestService
from concierge.legal.service import LegalService
from concierge.log import get_logger
from concierge.reservations.service import to_e164
from concierge.security.crypto import Keyring
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

log = get_logger(__name__)

PHRASES_FILE = Path(__file__).resolve().parent / "phrases.yaml"
Emit = Any  # Callable[[VoiceReply], Awaitable[None]]; kept loose for the protocol handlers


class VoicePhrases:
    def __init__(self, data: dict[str, Any]) -> None:
        self.decline = _compile(data.get("decline") or {})
        self.goodbye = _compile(data.get("goodbye") or {})

    @classmethod
    def load(cls, path: Path = PHRASES_FILE) -> VoicePhrases:
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def declines(self, text: str, lang: str) -> bool:
        return _matches(self.decline, text, lang)

    def says_goodbye(self, text: str, lang: str) -> bool:
        """A farewell without a question: the caller is done."""
        return "?" not in text and _matches(self.goodbye, text, lang)


def _compile(table: dict[str, Any]) -> dict[str, list[re.Pattern[str]]]:
    return {
        lang: [re.compile(r"(?<!\w)" + re.escape(_norm(str(p))) + r"(?!\w)") for p in phrases or []]
        for lang, phrases in table.items()
    }


def _matches(table: dict[str, list[re.Pattern[str]]], text: str, lang: str) -> bool:
    lowered = _norm(text)
    ordered = [lang] + [code for code in table if code != lang]
    return any(p.search(lowered) for code in ordered for p in table.get(code, []))


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


@dataclass
class VoiceCallState:
    tenant: Tenant
    profile: TenantProfile
    provider: str
    call_id: str
    call_row_id: str
    caller_ref: str
    phone: str | None
    lang: str
    started_at: datetime
    guest_id: str | None = None
    conversation_id: str | None = None
    disclosed: bool = False
    transcript: list[tuple[str, str]] = field(default_factory=list)
    response_id: int = -1
    task: asyncio.Task[BrainResult] | None = None
    transfer_number: str | None = None
    transfer_failed: bool = False
    end_requested: bool = False
    declined: bool = False
    callback_id: str | None = None
    ended: bool = False
    outcome: str = "in_progress"

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.call_id}"


class VoiceEngine:
    """Owns the live calls; the protocol handlers translate messages to ``begin``/``turn``."""

    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        keyring: Keyring,
        brain: Brain,
        conversations: ConversationService,
        tenants: TenantService,
        requests: RequestService,
        legal: LegalService,
        texts: GuestTexts,
        guardrails: Guardrails | None = None,
        phrases: VoicePhrases | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.keyring = keyring
        self.brain = brain
        self.conversations = conversations
        self.tenants = tenants
        self.requests = requests
        self.legal = legal
        self.texts = texts
        self.guardrails = guardrails or Guardrails()
        self.phrases = phrases or VoicePhrases.load()
        self.calls: dict[str, VoiceCallState] = {}
        self.by_conversation: dict[str, VoiceCallState] = {}

    # ---------------------------------------------------------------- begin
    async def begin(
        self,
        tenant: Tenant,
        provider: str,
        call_id: str,
        *,
        from_number: str | None = None,
        to_number: str | None = None,
        agent_id: str | None = None,
    ) -> tuple[VoiceCallState, VoiceReply]:
        """Register the call and return the disclosure (spoken first, uninterruptible)."""
        key = f"{provider}:{call_id}"
        existing = self.calls.get(key)
        if existing is not None:  # a reconnect never repeats the disclosure
            return existing, VoiceReply("", complete=True)
        profile = self.tenants.profile_of(tenant)
        now = self.clock.now()
        lang = profile.default_language
        phone = to_e164(from_number, profile.contacts.phone_region) if from_number else None
        caller_ref = self.keyring.phone_ref(phone) if phone else f"anon_{call_id[:24]}"
        refused = ""
        if (
            provider == "retell"
            and profile.voice.eu_only
            and not profile.voice.us_transfer_acknowledged
        ):
            refused = "eu_only"
        elif tenant.kill_switch or tenant.paused:
            refused = "unavailable"
        async with self.db.tenant_scope(tenant.id) as session:
            row = VoiceCall(
                tenant_id=tenant.id,
                provider=provider,
                call_id=call_id,
                caller_ref=caller_ref,
                lang=lang,
                started_at=now,
                outcome=f"refused_{refused}" if refused else "in_progress",
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        state = VoiceCallState(
            tenant=tenant,
            profile=profile,
            provider=provider,
            call_id=call_id,
            call_row_id=row.id,
            caller_ref=caller_ref,
            phone=phone,
            lang=lang,
            started_at=now,
        )
        self.calls[key] = state
        if refused:
            state.ended = True
            state.outcome = f"refused_{refused}"
            log.warning("voice.refused", tenant=tenant.id, reason=refused, provider=provider)
            text = self.texts.render(
                "voice_unavailable", lang, phone=profile.contacts.reception_phone or "-"
            )
            return state, VoiceReply(text, complete=True, end_call=True)
        guest = await self.conversations.get_or_create_guest(
            tenant.id, "voice", caller_ref, phone=phone, lang=lang
        )
        conversation = await self.conversations.open_conversation(
            tenant.id, guest.id, "voice", lang=lang
        )
        state.guest_id = guest.id
        state.conversation_id = conversation.id
        self.by_conversation[conversation.id] = state
        disclosure = await self._disclosure(tenant.id, profile, lang)
        await self.conversations.record_outbound(
            tenant.id,
            conversation.id,
            disclosure,
            lang=lang,
            kind="disclosure",
            intent="A:voice_disclosure",
            meta={"call_id": call_id, "provider": provider},
            retention_days=profile.voice.transcript_retention_days,
            status="sent",
        )
        await self.conversations.mark_disclosed(tenant.id, conversation.id)
        async with self.db.tenant_scope(tenant.id) as session:
            stored = await session.get(VoiceCall, row.id)
            if stored is not None:
                stored.conversation_id = conversation.id
                stored.disclosure_spoken_at = now
                await session.commit()
        state.disclosed = True
        state.transcript.append(("agent", disclosure))
        log.info("voice.call_started", tenant=tenant.id, provider=provider, call=call_id)
        return state, VoiceReply(disclosure, complete=True)

    async def _disclosure(self, tenant_id: str, profile: TenantProfile, lang: str) -> str:
        for row in await self.legal.rows(tenant_id):
            if row.key == "voice_notice" and row.lang == lang and row.status == "approved":
                return row.text
        return self.texts.render(
            "voice_disclosure",
            lang,
            hotel=profile.name,
            days=profile.voice.transcript_retention_days,
            phone=profile.contacts.reception_phone or "-",
        )

    # ----------------------------------------------------------------- turn
    def cancel(self, state: VoiceCallState) -> bool:
        """Cancel the running generation (a newer response id arrived)."""
        task = state.task
        if task is not None and not task.done():
            task.cancel()
            state.task = None
            return True
        return False

    async def turn(
        self, state: VoiceCallState, response_id: int, utterance: str, emit: Emit
    ) -> str:
        """Answer one caller utterance; returns answered, ended, cancelled or stale."""
        if state.ended:
            return "ended"
        if response_id <= state.response_id:
            return "stale"
        self.cancel(state)
        state.response_id = response_id
        lang = state.lang
        text = utterance.strip()
        if not text:  # the caller stayed silent (reminder)
            reply = VoiceReply(self.texts.render("voice_no_reply", lang), complete=True)
            state.transcript.append(("agent", reply.text))
            await emit(reply)
            return "answered"
        guard = self.guardrails.check(text, lang)
        text = guard.text  # card, IBAN and ID numbers never reach the transcript (C8)
        state.transcript.append(("user", text))
        if not state.declined and self.phrases.declines(text, lang):
            return await self._decline(state, text, emit)
        if self.phrases.says_goodbye(text, lang):
            return await self._goodbye(state, text, emit)
        if guard.kind == "person":
            return await self._person(state, text, emit)
        task = asyncio.create_task(self._brain(state, text))
        state.task = task
        try:
            done, _pending = await asyncio.wait(
                {task}, timeout=state.profile.voice.filler_ms / 1000
            )
            if not done:
                await emit(VoiceReply(self.texts.render("voice_filler", lang), complete=False))
            result = await task
        except asyncio.CancelledError:
            return "cancelled"
        finally:
            if state.task is task:
                state.task = None
        if state.response_id != response_id:
            return "cancelled"
        return await self._speak(state, result, guard.kind, emit)

    async def _brain(self, state: VoiceCallState, text: str) -> BrainResult:
        return await self.brain.respond(
            state.tenant,
            Inbound(
                channel="voice",
                channel_key=state.caller_ref,
                text=text,
                phone=state.phone,
                meta={"call_id": state.call_id},
            ),
        )

    async def _speak(
        self, state: VoiceCallState, result: BrainResult, guard_kind: str, emit: Emit
    ) -> str:
        profile = state.profile
        lang = state.lang
        if result.language in profile.languages and result.language != state.lang:
            state.lang = result.language
            lang = result.language
        spoken = self._spoken(result, lang)
        if state.transfer_number:
            reply = VoiceReply(
                spoken or self.texts.render("voice_transfer", lang),
                complete=True,
                transfer_number=state.transfer_number,
                language=lang if lang != profile.default_language else None,
            )
            state.outcome = "transferred"
            state.ended = True
        elif result.handoff_id and guard_kind != "emergency" and not state.callback_id:
            await self._callback(state, "the assistant handed the call to staff")
            await self._keep_talking(state)
            note = (
                self.texts.render("voice_callback_saved", lang)
                if state.phone
                else self.texts.render(
                    "voice_callback_no_number", lang, phone=profile.contacts.reception_phone or "-"
                )
            )
            reply = VoiceReply(f"{spoken} {note}".strip() if spoken else note, complete=True)
        elif state.end_requested:
            reply = VoiceReply(
                spoken or self.texts.render("voice_goodbye", lang), complete=True, end_call=True
            )
            if state.outcome == "in_progress":
                state.outcome = "completed"
            state.ended = True
        else:
            reply = VoiceReply(spoken or self.texts.render("voice_no_reply", lang), complete=True)
            if result.handoff_id:
                await self._keep_talking(state)
        if reply.language is None and lang != profile.default_language:
            reply.language = lang
        state.transcript.append(("agent", reply.text))
        await self._persist_outcome(state)
        await emit(reply)
        return "ended" if state.ended else "answered"

    async def _persist_outcome(self, state: VoiceCallState) -> None:
        async with self.db.tenant_scope(state.tenant.id) as session:
            row = await session.get(VoiceCall, state.call_row_id)
            if row is not None and row.ended_at is None and row.outcome != state.outcome:
                row.outcome = state.outcome
                row.lang = state.lang
                await session.commit()

    def _spoken(self, result: BrainResult, lang: str) -> str:
        parts = [m.text for m in result.messages if m.kind not in {"disclosure", "banner"}]
        return for_speech(
            " ".join(p for p in parts if p),
            lang,
            website=self.texts.render("voice_website", lang),
        )

    # -------------------------------------------------- person and decline
    async def _person(self, state: VoiceCallState, text: str, emit: Emit) -> str:
        profile, lang = state.profile, state.lang
        await self._record_inbound(state, text)
        number = to_e164(profile.contacts.reception_phone, profile.contacts.phone_region)
        if number and profile.is_staffed(self.clock.now()):
            state.transfer_number = number
            state.outcome = "transferred"
            state.ended = True
            reply = VoiceReply(
                self.texts.render("voice_transfer", lang), complete=True, transfer_number=number
            )
        else:
            state.transfer_failed = True
            await self._callback(state, "caller asked for a person outside staffed hours")
            first = self.texts.render("voice_transfer_unavailable", lang)
            second = (
                self.texts.render("voice_callback_saved", lang)
                if state.phone
                else self.texts.render(
                    "voice_callback_no_number", lang, phone=profile.contacts.reception_phone or "-"
                )
            )
            reply = VoiceReply(f"{first} {second}", complete=True)
        await self._record_outbound(state, reply.text, intent="H:person")
        state.transcript.append(("agent", reply.text))
        await self._persist_outcome(state)
        await emit(reply)
        return "ended" if state.ended else "answered"

    async def _goodbye(self, state: VoiceCallState, text: str, emit: Emit) -> str:
        await self._record_inbound(state, text)
        if state.outcome == "in_progress":
            state.outcome = "completed"
        state.ended = True
        reply = VoiceReply(
            self.texts.render("voice_goodbye", state.lang), complete=True, end_call=True
        )
        await self._record_outbound(state, reply.text, intent="A:goodbye")
        state.transcript.append(("agent", reply.text))
        await self._persist_outcome(state)
        await emit(reply)
        return "ended"

    async def _decline(self, state: VoiceCallState, text: str, emit: Emit) -> str:
        state.declined = True
        state.outcome = "declined"
        state.ended = True
        await self._callback(state, "caller declined the AI assistant")
        reply = VoiceReply(
            self.texts.render("voice_declined", state.lang), complete=True, end_call=True
        )
        await self._persist_outcome(state)
        await emit(reply)
        log.info("voice.declined", tenant=state.tenant.id, call=state.call_id)
        return "ended"

    async def _keep_talking(self, state: VoiceCallState) -> None:
        """A handoff on the phone never silences the assistant: the caller is still there."""
        if state.conversation_id is not None:
            await self.conversations.update(
                state.tenant.id, state.conversation_id, status="bot_handling"
            )

    async def _callback(self, state: VoiceCallState, reason: str) -> None:
        if state.callback_id is not None:
            return
        row = await self.requests.create_callback(
            state.tenant.id,
            conversation_id=state.conversation_id,
            phone=state.phone,
            reason=reason,
            lang=state.lang,
            voice_call_id=state.call_row_id,
        )
        state.callback_id = row.id
        if state.outcome == "in_progress":
            state.outcome = "callback"

    async def _record_inbound(self, state: VoiceCallState, text: str) -> None:
        if state.conversation_id is None:
            return
        await self.conversations.record_inbound(
            state.tenant.id,
            state.conversation_id,
            Inbound(channel="voice", channel_key=state.caller_ref, text=text),
            lang=state.lang,
            retention_days=state.profile.voice.transcript_retention_days,
        )

    async def _record_outbound(self, state: VoiceCallState, text: str, *, intent: str) -> None:
        if state.conversation_id is None:
            return
        await self.conversations.record_outbound(
            state.tenant.id,
            state.conversation_id,
            text,
            lang=state.lang,
            intent=intent,
            meta={"call_id": state.call_id},
            retention_days=state.profile.voice.transcript_retention_days,
            status="sent",
        )

    # ------------------------------------------------------- tool backends
    async def transfer(self, conversation_id: str, reason: str) -> bool:
        """``transfer_to_human`` backend: only during staffed hours with a reception number."""
        state = self.by_conversation.get(conversation_id)
        if state is None:
            return False
        profile = state.profile
        number = to_e164(profile.contacts.reception_phone, profile.contacts.phone_region)
        if number is None or not profile.is_staffed(self.clock.now()):
            state.transfer_failed = True
            return False
        state.transfer_number = number
        return True

    async def end_call(self, conversation_id: str, reason: str) -> None:
        state = self.by_conversation.get(conversation_id)
        if state is not None:
            state.end_requested = True

    # ------------------------------------------------------------- lookup
    def state_for(self, provider: str, call_id: str) -> VoiceCallState | None:
        return self.calls.get(f"{provider}:{call_id}")

    def forget(self, state: VoiceCallState) -> None:
        self.cancel(state)
        self.calls.pop(state.key, None)
        if state.conversation_id:
            self.by_conversation.pop(state.conversation_id, None)
