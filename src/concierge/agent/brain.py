"""The conversation engine: one channel-agnostic ``Brain.respond`` (F01).

Implements: F01, F03, F04, F11, F17, F23, C6, C7, C8, C9, C17, 8.1, 8.2, 8.4

Per guest message, under a per-conversation lock: language choice (sticky),
guardrails (emergency, STOP, person, other guest, card redaction), the AI disclosure
inserted by code on first contact, the bot-silent rule while staff handle the
conversation, then either the model path (prompt assembly, tool loop with caps,
validation with one regeneration, failover chain, circuit breaker, budget) or the
deterministic responder. Every failure ends in a handoff or the responder, never in
silence. Side effects: messages recorded, handoffs created, usage reported.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from concierge.agent.guardrails import Guardrails
from concierge.agent.languages import choose_language, unsupported_note_languages
from concierge.agent.prompting import PromptAssembler, tenant_block
from concierge.agent.resilience import CircuitBreaker, DailyBudget
from concierge.agent.responder import Responder, ResponderContext, Slots, extract_slots
from concierge.agent.schemas import AgentReply, Button, HandoffDecision, output_schema
from concierge.agent.texts import GuestTexts
from concierge.agent.tools import ToolBackends, ToolRunner, ToolSession
from concierge.agent.upsells import Offer, UpsellContext, UpsellPolicy
from concierge.agent.validators import ValidationContext, corrective_message, validate_reply
from concierge.clock import Clock, local_date
from concierge.conversations.service import BOT_SILENT_STATUSES, ConversationService, Inbound
from concierge.db.models import Conversation, Guest, Suppression, Tenant
from concierge.handoff.service import HandoffService
from concierge.kb.store import KbService
from concierge.llm.port import (
    LLMError,
    LLMPort,
    LLMRequest,
    Message,
    TextPart,
    ToolResult,
    Usage,
)
from concierge.log import get_logger
from concierge.metering.prices import ModelPriceRow, default_model_price, model_cost_usd
from concierge.security.crypto import Keyring
from concierge.security.limiter import MovingWindowLimiter
from concierge.tenants.profile import TenantProfile

log = get_logger(__name__)

UNSUPPORTED_KINDS = frozenset(
    {
        "media",
        "audio",
        "image",
        "document",
        "video",
        "sticker",
        "location",
        "contacts",
        "unsupported",
    }
)
CHAT_BUTTON_IDS = frozenset(
    {
        "book",
        "person",
        "eta",
        "directions",
        "parking",
        "breakfast",
        "yes",
        "no",
        "more",
        "verify",
        "privacy",
        "eta_before_18",
        "eta_18_22",
        "eta_after_22",
        "stay_ok",
        "stay_issue",
        "upsell_yes",
        "upsell_no",
    }
)
JOURNEY_BUTTON_IDS = frozenset(
    {"eta_before_18", "eta_18_22", "eta_after_22", "stay_ok", "stay_issue"}
)


class JourneyReplies(Protocol):
    """Replies to journey template buttons (F13); implemented by the journey engine."""

    async def eta_window(
        self, tenant: Tenant, guest: Guest, conversation: Conversation, button_id: str, lang: str
    ) -> str | None: ...

    async def stay_feedback(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        guest: Guest,
        conversation: Conversation,
        ok: bool,
        lang: str,
    ) -> tuple[str, str | None]: ...


StopHook = Callable[[str, str | None], Awaitable[int]]
UpsellRequestHook = Callable[[Tenant, Conversation, str, str], Awaitable[str]]
"""(tenant, conversation, upsell id, lang) -> request id; records the guest's yes for staff."""


PriceLookup = Callable[[str, date], Awaitable["ModelPriceRow | None"]]
"""The model price valid on a day (the metering table, then the built-in defaults)."""
SpentLoader = Callable[[str, str], Awaitable[float]]
"""USD already spent today by a tenant (tenant id, time zone), read once per day."""


@dataclass
class ModelConfig:
    primary: str = "claude-sonnet-5-5"
    failover: str = "claude-sonnet-5"
    cheap: str = ""
    structured_outputs: bool = True
    turn_timeout_s: float = 25.0
    inference_geo: str = "global"
    max_tokens: int = 1024
    effort_override: Literal["low", "medium"] | None = None
    """Forces one effort for every turn (the live evaluation compares low and medium)."""


@dataclass
class Outbound:
    text: str
    lang: str
    buttons: list[Button] = field(default_factory=list)
    kind: str = "text"
    intent: str | None = None
    message_id: str | None = None
    """The stored ``message`` row, set once the turn is persisted."""


@dataclass
class BrainResult:
    conversation_id: str
    guest_id: str
    language: str
    mode: str
    messages: list[Outbound]
    reply: AgentReply | None = None
    handoff_id: str | None = None
    events: list[str] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    silent: bool = False
    inbound_message_id: str | None = None
    """The stored row of the guest message being answered (excluded from the history)."""


class BrainFailure(Exception):
    """The model path could not produce a validated reply; the caller degrades."""

    def __init__(self, reason: str, *, degrade: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.degrade = degrade


BackendsFactory = Callable[
    [Tenant, TenantProfile, Conversation, Guest, ToolSession], Awaitable[ToolBackends]
]
AvailabilityLookup = Callable[
    [Tenant, TenantProfile, Conversation, Slots, str, str], Awaitable[dict[str, Any] | None]
]
"""(tenant, profile, conversation, slots, lang, guest text) -> responder summary or None."""
UsageSink = Callable[[str, str, Usage, dict[str, Any]], Awaitable[None]]
AlertSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
QaSink = Callable[[str, str, "BrainResult"], Awaitable[None]]


class Brain:
    def __init__(
        self,
        *,
        kb: KbService,
        conversations: ConversationService,
        handoffs: HandoffService,
        texts: GuestTexts,
        prompts: PromptAssembler,
        responder: Responder,
        guardrails: Guardrails,
        clock: Clock,
        keyring: Keyring,
        llm: LLMPort | None = None,
        models: ModelConfig | None = None,
        backends_factory: BackendsFactory | None = None,
        availability_lookup: AvailabilityLookup | None = None,
        journey_replies: JourneyReplies | None = None,
        stop_hook: StopHook | None = None,
        upsell_request: UpsellRequestHook | None = None,
        usage_sink: UsageSink | None = None,
        alert_sink: AlertSink | None = None,
        breaker: CircuitBreaker | None = None,
        budget: DailyBudget | None = None,
        demo_banner: bool = False,
        qa_sink: QaSink | None = None,
        price_lookup: PriceLookup | None = None,
        spent_loader: SpentLoader | None = None,
    ) -> None:
        self.kb = kb
        self.conversations = conversations
        self.handoffs = handoffs
        self.texts = texts
        self.prompts = prompts
        self.responder = responder
        self.guardrails = guardrails
        self.clock = clock
        self.keyring = keyring
        self.llm = llm
        self.models = models or ModelConfig()
        self.backends_factory = backends_factory
        self.availability_lookup = availability_lookup
        self.journey_replies = journey_replies
        self.stop_hook = stop_hook
        self.upsell_request = upsell_request
        self.upsells = UpsellPolicy(kb, texts)
        self.usage_sink = usage_sink
        self.alert_sink = alert_sink
        self.breaker = breaker or CircuitBreaker(clock)
        self.budget = budget or DailyBudget(clock)
        self.guest_limiter = MovingWindowLimiter()
        self.demo_banner = demo_banner
        self.price_lookup = price_lookup
        self.spent_loader = spent_loader
        self.qa_sink = qa_sink

    # ------------------------------------------------------------ entry
    async def respond(self, tenant: Tenant, inbound: Inbound) -> BrainResult:
        """One guest message; turns of the same guest on a channel run one at a time."""
        key = f"{tenant.id}:{inbound.channel}:{inbound.channel_key}"
        async with self.conversations.lock(key):
            return await self._respond_locked(tenant, inbound)

    async def _respond_locked(self, tenant: Tenant, inbound: Inbound) -> BrainResult:
        profile = TenantProfile.model_validate(tenant.profile)
        guest = await self.conversations.get_or_create_guest(
            tenant.id,
            inbound.channel,
            inbound.channel_key,
            phone=inbound.phone,
            name=inbound.profile_name,
            lang=profile.default_language,
            wa_user_id=inbound.wa_user_id,
        )
        conversation = await self.conversations.open_conversation(
            tenant.id, guest.id, inbound.channel, lang=guest.lang or profile.default_language
        )
        async with self.conversations.lock(conversation.id):
            try:
                return await self._turn(tenant, profile, guest, conversation, inbound)
            except Exception:  # every failure ends in a handoff, never silence
                log.exception("brain.turn_failed", tenant=tenant.id, conversation=conversation.id)
                return await self._emergency_degrade(tenant, profile, conversation, guest, inbound)

    # ------------------------------------------------------------- turn
    async def _turn(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        guest: Guest,
        conversation: Conversation,
        inbound: Inbound,
    ) -> BrainResult:
        has_history = conversation.last_inbound_at is not None
        lang, supported = choose_language(
            inbound.text,
            conversation.lang if has_history else None,
            profile.languages,
            profile.default_language,
            inbound.language_override,
        )
        reply_lang = lang if supported else "en"
        guard = self.guardrails.check(inbound.text, lang)
        retention = profile.retention.chat_days
        special = self.guardrails.special_categories(guard.text, lang)
        stored = Inbound(**{**inbound.__dict__, "text": guard.text})
        if special:
            stored.meta = {**stored.meta, "special_category": True}
        inbound_row = await self.conversations.record_inbound(
            tenant.id, conversation.id, stored, lang=lang, retention_days=retention
        )
        result = BrainResult(conversation.id, guest.id, reply_lang, "static", [])
        result.inbound_message_id = inbound_row.id
        if special:
            result.events.append("special_category")
            await self.conversations.merge_meta(tenant.id, conversation.id, special_category=True)
        if guard.injection:
            result.events.append("injection_marker")
            log.warning(
                "guardrail.injection_marker", tenant=tenant.id, conversation=conversation.id
            )
        if guard.redactions:
            result.events.append("redacted:" + ",".join(guard.redactions))

        disclosure = None
        if conversation.ai_disclosed_at is None:
            disclosure = Outbound(
                self.texts.render(
                    "disclosure",
                    reply_lang,
                    hotel=profile.name,
                    privacy_url=profile.contacts.privacy_url or "-",
                ),
                reply_lang,
                kind="disclosure",
                intent="A:disclosure",
            )
            await self.conversations.mark_disclosed(tenant.id, conversation.id)
            result.events.append("disclosed")

        # emergencies come first, before anything else
        if guard.kind == "emergency":
            result.messages.append(
                Outbound(
                    self.texts.render("emergency", reply_lang), reply_lang, intent="H:emergency"
                )
            )
            if disclosure:
                result.messages.append(disclosure)
            await self._handoff(
                tenant,
                profile,
                conversation,
                result,
                "P0",
                "emergency",
                f"emergency keyword: {guard.matched}",
            )
            return await self._finish(tenant, profile, conversation, result)

        if disclosure:
            result.messages.append(disclosure)

        if inbound.kind in UNSUPPORTED_KINDS:
            result.messages.append(
                Outbound(
                    self.texts.render("unsupported_media", reply_lang),
                    reply_lang,
                    intent="Q:media",
                    buttons=self._buttons(["person"], reply_lang, inbound.channel),
                )
            )
            return await self._finish(tenant, profile, conversation, result)

        # STOP is honoured in every conversation state, also while staff handle the case (C9)
        if guard.kind == "stop":
            await self._suppress(tenant, conversation, inbound)
            result.messages.append(
                Outbound(
                    self.texts.render("opt_out_confirm", reply_lang), reply_lang, intent="A:opt_out"
                )
            )
            result.events.append("stop")
            return await self._finish(tenant, profile, conversation, result)

        if conversation.status in BOT_SILENT_STATUSES:
            result.mode = "silent"
            result.silent = True
            if not conversation.meta.get("staff_ack_sent"):
                result.messages.append(
                    Outbound(
                        self.texts.render("staff_active_ack", reply_lang),
                        reply_lang,
                        intent="A:staff_active",
                    )
                )
                await self.conversations.merge_meta(tenant.id, conversation.id, staff_ack_sent=True)
            return await self._finish(tenant, profile, conversation, result)

        per_hour = profile.limits.messages_per_guest_per_hour
        limiter_key = f"{tenant.id}:{inbound.channel}:{inbound.channel_key}"
        if not self.guest_limiter.hit(limiter_key, f"{per_hour}/hour"):
            result.messages.append(
                Outbound(
                    self.texts.render("rate_limited", reply_lang),
                    reply_lang,
                    intent="A:rate_limited",
                )
            )
            result.events.append("rate_limited")
            return await self._finish(tenant, profile, conversation, result)

        if guard.kind == "other_guest":
            result.messages.append(
                Outbound(
                    self.texts.render("other_guest_refusal", reply_lang),
                    reply_lang,
                    intent="R:other_guest",
                )
            )
            await self._handoff(
                tenant,
                profile,
                conversation,
                result,
                "P3",
                "other_guest",
                "question about another guest",
            )
            return await self._finish(tenant, profile, conversation, result)

        if guard.kind == "person":
            await self._handoff(
                tenant,
                profile,
                conversation,
                result,
                "P2",
                "person_requested",
                "guest asked for a person",
            )
            result.messages.append(self._handoff_ack(profile, reply_lang, tenant.id))
            return await self._finish(tenant, profile, conversation, result)

        if (
            inbound.button_id in JOURNEY_BUTTON_IDS
            and self.journey_replies is not None
            and await self._journey_button(
                tenant, profile, guest, conversation, str(inbound.button_id), reply_lang, result
            )
        ):
            return await self._finish(tenant, profile, conversation, result)

        if inbound.button_id in {"upsell_yes", "upsell_no"} and conversation.meta.get(
            "upsell_pending"
        ):
            await self._upsell_answer(
                tenant, conversation, str(inbound.button_id) == "upsell_yes", reply_lang, result
            )
            return await self._finish(tenant, profile, conversation, result)

        if guard.redactions:
            result.messages.append(
                Outbound(
                    self.texts.render("card_warning", reply_lang),
                    reply_lang,
                    intent="R:sensitive_data",
                )
            )
            if not re.search(r"[^\W\d_]{3,}", guard.text.replace("[redacted]", "")):
                return await self._finish(tenant, profile, conversation, result)

        if not supported:
            result.messages.append(
                Outbound(
                    self.texts.render(
                        "unsupported_language",
                        "en",
                        languages=unsupported_note_languages(profile.languages),
                    ),
                    "en",
                    intent="A:unsupported_language",
                )
            )
            result.events.append(f"unsupported_language:{lang}")

        degraded = tenant.kill_switch or tenant.paused
        if degraded:
            result.messages.append(
                Outbound(
                    self.texts.render(
                        "degraded", reply_lang, phone=profile.contacts.reception_phone or "-"
                    ),
                    reply_lang,
                    intent="H:degraded",
                )
            )
            await self._handoff(
                tenant,
                profile,
                conversation,
                result,
                "P2",
                "degraded",
                "kill switch or paused tenant",
            )
            result.mode = "degraded"
            return await self._finish(tenant, profile, conversation, result)

        if self.spent_loader is not None and self.budget.needs_prime(tenant.id, profile.timezone):
            # after a restart the in-memory total starts from the stored usage of the day
            self.budget.prime(
                tenant.id, profile.timezone, await self.spent_loader(tenant.id, profile.timezone)
            )
        use_model = (
            self.llm is not None
            and tenant.ai_mode == "full"
            and self.breaker.allows()
            and self.budget.allows(tenant.id, profile.timezone, profile.limits.daily_llm_budget_usd)
        )
        reply: AgentReply | None = None
        if use_model:
            try:
                reply = await self._model_turn(
                    tenant, profile, guest, conversation, inbound, guard.text, reply_lang, result
                )
                result.mode = "model"
            except BrainFailure as failure:
                result.events.append(f"model_failure:{failure.reason}")
                log.warning(
                    "brain.model_failure",
                    tenant=tenant.id,
                    conversation=conversation.id,
                    reason=failure.reason,
                )
                if failure.degrade and self.alert_sink is not None:
                    await self.alert_sink("llm_degraded", tenant.id, {"reason": failure.reason})
        if reply is None:
            reply = await self._responder_turn(
                tenant, profile, conversation, guard.text, reply_lang, result
            )
            result.mode = "responder" if result.mode != "model" else "model"
            if (
                result.mode == "responder"
                and self.demo_banner
                and not conversation.meta.get("demo_banner_sent")
            ):
                result.messages.append(
                    Outbound(
                        self.texts.render("demo_banner", reply_lang),
                        reply_lang,
                        kind="banner",
                        intent="A:demo_banner",
                    )
                )
                await self.conversations.merge_meta(
                    tenant.id, conversation.id, demo_banner_sent=True
                )
        result.reply = reply
        await self._post_process(
            tenant,
            profile,
            conversation,
            inbound,
            reply,
            reply_lang,
            result,
            complaint_cue=guard.complaint,
        )
        return await self._finish(tenant, profile, conversation, result)

    # ------------------------------------------------------------- model
    async def _model_turn(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        guest: Guest,
        conversation: Conversation,
        inbound: Inbound,
        guest_text: str,
        reply_lang: str,
        result: BrainResult,
    ) -> AgentReply:
        assert self.llm is not None
        now = self.clock.now()
        stay_date = _stay_date(conversation)
        snapshot = await self.kb.snapshot(tenant.id, reply_lang, stay_date=stay_date)
        persona, policy = await self._staff_texts(tenant.id, reply_lang)
        system = self.prompts.system_blocks(
            profile,
            kb_snapshot=snapshot.text,
            channel=inbound.channel,
            persona=persona,
            policy=policy,
        )
        window_open: bool | None = None
        if inbound.channel == "whatsapp":
            window_open = (
                conversation.window_expires_at is not None and conversation.window_expires_at > now
            )
        verified = conversation.verified_reservation_id is not None
        envelope = self.prompts.envelope(
            now=now,
            tz=profile.timezone,
            reply_language=reply_lang,
            verified=verified,
            window_open=window_open,
            channel=inbound.channel,
            guest_kind="verified" if verified else "unknown",
        )
        history = await self.conversations.history(
            tenant.id, conversation.id, limit=12, exclude_id=result.inbound_message_id
        )
        messages = _history_messages(history)
        messages.append(Message("user", [TextPart(envelope + "\n\n" + guest_text)]))
        tool_session = ToolSession(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            guest_id=guest.id,
            lang=reply_lang,
            channel=inbound.channel,
            verified=verified,
            stay_date=stay_date,
            verify_attempts=await self.conversations.verify_attempt_state(
                tenant.id, conversation.id
            ),
            guest_text=guest_text,
            local_now=self.clock.now().astimezone(ZoneInfo(profile.timezone)),
            key_safe_from=time.fromisoformat(profile.journeys.key_safe.at),
        )
        backends = (
            await self.backends_factory(tenant, profile, conversation, guest, tool_session)
            if self.backends_factory
            else self._default_backends(tenant, profile, conversation, result)
        )
        runner = ToolRunner(self.kb, backends)
        request = LLMRequest(
            model=self.models.primary,
            system=system,
            messages=messages,
            tools=runner.specs(inbound.channel),
            max_tokens=self.models.max_tokens,
            effort=self.models.effort_override or "low",
            output_schema=output_schema() if self.models.structured_outputs else None,
            inference_geo=self.models.inference_geo,
            metadata={"user_id": guest.id},
        )
        limits = profile.limits
        tool_texts: list[str] = []
        allow_binding = False
        calls = 0
        tool_runs = 0
        repaired = False
        regenerated = False
        try:
            async with asyncio.timeout(self.models.turn_timeout_s):
                while calls < limits.max_model_calls_per_message:
                    response = await self._complete_with_failover(request, tenant, profile, result)
                    calls += 1
                    if response.stop_reason == "tool_use" and response.tool_uses:
                        results: list[ToolResult] = []
                        for use in response.tool_uses:
                            if tool_runs >= limits.max_tool_runs_per_message:
                                results.append(
                                    ToolResult(
                                        use.id,
                                        json.dumps(
                                            {
                                                "error": True,
                                                "instruction": "tool budget for this message is exhausted; answer with the facts you have or hand off",
                                            }
                                        ),
                                        is_error=True,
                                    )
                                )
                                continue
                            outcome = await runner.run(use.name, use.input, tool_session)
                            tool_runs += 1
                            results.append(ToolResult(use.id, outcome.content, outcome.is_error))
                            tool_texts.append(outcome.content)
                            allow_binding = allow_binding or outcome.binding_confirmation
                            result.events.append(f"tool:{use.name}")
                        request.messages.append(
                            Message("assistant", list(response.content), raw=response.raw)
                        )
                        request.messages.append(Message("user", list(results)))
                        request.effort = self.models.effort_override or "medium"
                        continue
                    if response.stop_reason in {"refusal", "max_tokens"}:
                        raise BrainFailure(f"stop_reason:{response.stop_reason}")
                    try:
                        reply = _parse_reply(response.text)
                    except ValueError as exc:
                        if repaired:
                            raise BrainFailure("invalid_json") from exc
                        repaired = True
                        request.messages.append(
                            Message("assistant", list(response.content), raw=response.raw)
                        )
                        request.messages.append(
                            Message(
                                "user",
                                [
                                    TextPart(
                                        "Your reply was not one valid JSON object with the required fields. Return exactly one JSON object now."
                                    )
                                ],
                            )
                        )
                        continue
                    ctx = ValidationContext(
                        lang=reply_lang,
                        canary=self.prompts.canary,
                        kb_text=snapshot.text,
                        tool_texts=tool_texts,
                        envelope=envelope,
                        guest_text=guest_text,
                        hotel_text=tenant_block(profile, persona, policy),
                        channel=inbound.channel,
                        allow_binding_words=allow_binding,
                        max_chars=limits.reply_max_chars,
                        hard_cap_chars=limits.reply_hard_cap_chars,
                        allowed_button_ids=frozenset()
                        if inbound.channel == "voice"
                        else CHAT_BUTTON_IDS,
                    )
                    validation = validate_reply(reply, ctx)
                    if validation.ok:
                        if tool_session.verified and conversation.verified_reservation_id is None:
                            result.events.append("verified")
                        await self.conversations.set_verify_attempts(
                            tenant.id, conversation.id, tool_session.verify_attempts
                        )
                        if (
                            tool_session.lang != reply_lang
                            and tool_session.lang in profile.languages
                        ):
                            await self.conversations.update(
                                tenant.id, conversation.id, lang=tool_session.lang
                            )
                        return reply
                    result.events.append("validator:" + ";".join(validation.violations))
                    if regenerated:
                        raise BrainFailure("validation:" + ";".join(validation.violations))
                    regenerated = True
                    request.messages.append(
                        Message("assistant", list(response.content), raw=response.raw)
                    )
                    request.messages.append(
                        Message("user", [TextPart(corrective_message(validation))])
                    )
        except TimeoutError as exc:
            self.breaker.record_failure()
            raise BrainFailure("turn_timeout") from exc
        raise BrainFailure("call_cap_reached")

    async def _complete_with_failover(
        self, request: LLMRequest, tenant: Tenant, profile: TenantProfile, result: BrainResult
    ) -> Any:
        assert self.llm is not None
        chain = [self.models.primary, self.models.failover]
        if self.models.cheap:
            chain.append(self.models.cheap)
        last: LLMError | None = None
        for index, model in enumerate(chain):
            request.model = model
            if index > 0:
                request.effort = None if model == self.models.cheap else request.effort
            try:
                response = await self.llm.complete(request)
            except LLMError as exc:
                last = exc
                result.events.append(f"llm_error:{model}:{exc.kind}")
                if exc.kind == "spend_cap":
                    self.breaker.record_failure()
                    if self.alert_sink is not None:
                        await self.alert_sink(
                            "spend_cap",
                            tenant.id,
                            {"model": model, "request_id": exc.request_id or ""},
                        )
                    raise BrainFailure("spend_cap", degrade=True) from exc
                if exc.kind in {"auth", "invalid_request"}:
                    self.breaker.record_failure()
                    raise BrainFailure(f"{exc.kind}", degrade=exc.kind == "auth") from exc
                self.breaker.record_failure()
                continue
            self.breaker.record_success()
            result.usage.append(response.usage)
            result.models.append(response.model)
            await self._meter(tenant, profile, response.model, response.usage, response.request_id)
            return response
        raise BrainFailure(f"all_models_failed:{last.kind if last else 'unknown'}")

    async def _meter(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        model: str,
        usage: Usage,
        request_id: str | None,
    ) -> None:
        row = (
            await self.price_lookup(model, self.clock.now().date())
            if self.price_lookup is not None
            else default_model_price(model)
        )
        usd = 0.0
        if row is not None:
            usd = model_cost_usd(
                row,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_read_tokens=usage.cache_read_tokens,
            )
        self.budget.record(tenant.id, profile.timezone, usd)
        if self.usage_sink is not None:
            await self.usage_sink(
                tenant.id,
                "llm",
                usage,
                {"model": model, "usd": usd, "request_id": request_id or ""},
            )

    async def _staff_texts(self, tenant_id: str, lang: str) -> tuple[str, str]:
        views, _ = await self.kb.resolve(tenant_id, lang, sensitivities=("staff_only",))
        persona = next((v.text for v in views if v.key == "tone"), "")
        policy = next((v.text for v in views if v.key == "refusals"), "")
        return persona, policy

    def _default_backends(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        conversation: Conversation,
        result: BrainResult,
    ) -> ToolBackends:
        async def create_handoff(priority: str, reason: str) -> str:
            created = await self.handoffs.create(
                tenant.id,
                conversation.id,
                priority=priority,
                trigger="tool",
                reason=reason,
                profile=profile,
            )
            result.handoff_id = created.id
            return created.id

        return ToolBackends(create_handoff=create_handoff)

    # --------------------------------------------------------- responder
    async def _responder_turn(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        conversation: Conversation,
        text: str,
        reply_lang: str,
        result: BrainResult,
    ) -> AgentReply:
        slots = _slots_from_meta(conversation.meta.get("slots"))
        availability_summary = None
        if self.availability_lookup is not None:
            intent = self.responder.intents.match(text, reply_lang)
            if slots is not None or (intent is not None and intent.name == "availability"):
                candidate = extract_slots(text, slots)
                if candidate.complete:
                    availability_summary = await self.availability_lookup(
                        tenant, profile, conversation, candidate, reply_lang, text
                    )
        ctx = ResponderContext(
            tenant_id=tenant.id,
            lang=reply_lang,
            channel=conversation.channel,
            verified=conversation.verified_reservation_id is not None,
            stay_date=_stay_date(conversation),
            local_now=self.clock.now().astimezone(ZoneInfo(profile.timezone)),
            key_safe_from=time.fromisoformat(profile.journeys.key_safe.at),
            hotel_phone=profile.contacts.reception_phone,
            previous_slots=slots,
            availability_summary=availability_summary,
        )
        reply, _views, new_slots = await self.responder.respond(text, ctx)
        if new_slots is not None:
            payload = {
                "arrival": new_slots.arrival.isoformat() if new_slots.arrival else None,
                "departure": new_slots.departure.isoformat() if new_slots.departure else None,
                "adults": new_slots.adults,
                "children": new_slots.children,
            }
            await self.conversations.merge_meta(
                tenant.id, conversation.id, slots=payload if not new_slots.complete else None
            )
        return reply

    # --------------------------------------------------------- post steps
    async def _post_process(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        conversation: Conversation,
        inbound: Inbound,
        reply: AgentReply,
        reply_lang: str,
        result: BrainResult,
        *,
        complaint_cue: bool = False,
    ) -> None:
        code = reply.intent_code
        normalised = _normalise_question(inbound.text)
        meta = dict(conversation.meta)
        last_question = meta.get("last_question")
        repeats = int(meta.get("repeat_count", 0)) + 1 if last_question == normalised else 0
        unresolved = int(meta.get("unresolved_turns", 0))
        unresolved = unresolved + 1 if code in {"H", "R"} else 0
        redirects = int(meta.get("redirect_count", 0))
        text = reply.reply_text
        buttons = list(reply.buttons)
        handoff_needed = reply.handoff.needed
        priority = reply.handoff.priority if reply.handoff.priority != "none" else "P3"
        trigger = "model" if result.mode == "model" else "responder"
        reason = reply.handoff.reason or reply.intent
        if code == "R" and "off_topic" in reply.topic:
            redirects += 1
            if redirects >= 2:
                text = self.texts.render("off_topic_person", reply_lang)
                buttons = self._buttons(["person"], reply_lang, inbound.channel)
            else:
                text = self.texts.render("off_topic_redirect", reply_lang)
        if repeats >= 1 and code in {"H", "R", "Q"} and not handoff_needed:
            text = self.texts.render("same_question", reply_lang)
            buttons = self._buttons(["person"], reply_lang, inbound.channel)
            result.events.append("same_question")
        if unresolved >= 3 and not handoff_needed:
            handoff_needed = True
            priority = "P3"
            trigger = "unresolved_turns"
            reason = "three unresolved turns"
        if complaint_cue and not handoff_needed:
            # F17: a complaint reaches staff even when the model answered it
            handoff_needed = True
            priority = "P1"
            trigger = "complaint_cue"
            reason = "complaint cue in the guest's message"
        elif complaint_cue and priority in {"P2", "P3"}:
            priority = "P1"  # an in-house complaint is P1 whatever the generic reason was
        if reply.confidence < profile.limits.min_confidence and not handoff_needed:
            handoff_needed = True
            priority = "P3"
            trigger = "low_confidence"
            reason = f"model confidence {reply.confidence:.2f}"
        result.messages.append(Outbound(text, reply_lang, buttons=buttons[:3], intent=reply.intent))
        if handoff_needed:
            await self._handoff(tenant, profile, conversation, result, priority, trigger, reason)
            if code != "H" or reply.topic in {"kb_miss", "unresolved"}:
                result.messages.append(self._handoff_ack(profile, reply_lang, tenant.id))
        else:
            await self._maybe_offer(tenant, conversation, reply, reply_lang, result)
        await self.conversations.merge_meta(
            tenant.id,
            conversation.id,
            last_question=normalised,
            repeat_count=repeats,
            unresolved_turns=unresolved,
            redirect_count=redirects,
        )

    async def _maybe_offer(
        self,
        tenant: Tenant,
        conversation: Conversation,
        reply: AgentReply,
        lang: str,
        result: BrainResult,
    ) -> None:
        if conversation.channel == "voice":
            return
        open_handoff = await self.handoffs.open_for_conversation(tenant.id, conversation.id)
        ctx = UpsellContext(
            conversation=conversation,
            intent_code=reply.intent_code,
            topic=reply.topic,
            handoff_needed=reply.handoff.needed,
            open_priority=open_handoff.priority if open_handoff else None,
        )
        offer: Offer | None = await self.upsells.offer(tenant.id, ctx, lang)
        if offer is None:
            return
        result.messages.append(
            Outbound(offer.text, lang, buttons=list(offer.buttons), intent="A:upsell")
        )
        result.events.append(f"upsell_offered:{offer.upsell_id}")
        await self.conversations.update(tenant.id, conversation.id, upsell_offered=True)
        await self.conversations.merge_meta(
            tenant.id, conversation.id, upsell_pending=offer.upsell_id
        )

    async def _upsell_answer(
        self,
        tenant: Tenant,
        conversation: Conversation,
        accepted: bool,
        lang: str,
        result: BrainResult,
    ) -> None:
        upsell_id = str(conversation.meta.get("upsell_pending") or "")
        if accepted and self.upsell_request is not None:
            request_id = await self.upsell_request(tenant, conversation, upsell_id, lang)
            result.events.append(f"upsell_requested:{upsell_id}:{request_id}")
            result.messages.append(
                Outbound(self.texts.render("request_recorded", lang), lang, intent="A:upsell")
            )
            await self.conversations.merge_meta(
                tenant.id, conversation.id, upsell_pending=None, upsell_requested=upsell_id
            )
            return
        result.events.append(f"upsell_declined:{upsell_id}")
        result.messages.append(
            Outbound(self.texts.render("upsell_declined_ack", lang), lang, intent="A:upsell")
        )
        await self.conversations.merge_meta(
            tenant.id, conversation.id, upsell_pending=None, upsell_declined=True
        )

    async def _handoff(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        conversation: Conversation,
        result: BrainResult,
        priority: str,
        trigger: str,
        reason: str,
    ) -> None:
        created = await self.handoffs.create(
            tenant.id,
            conversation.id,
            priority=priority,
            trigger=trigger,
            reason=reason,
            profile=profile,
        )
        result.handoff_id = created.id
        result.events.append(f"handoff:{created.priority}:{trigger}")

    def _handoff_ack(self, profile: TenantProfile, lang: str, tenant_id: str) -> Outbound:
        window = profile.handoff.response_window_text.get(lang)
        if window:
            return Outbound(
                self.texts.render("handoff_ack_with_window", lang, window=window),
                lang,
                intent="H:handoff",
            )
        return Outbound(self.texts.render("handoff_ack", lang), lang, intent="H:handoff")

    def _buttons(self, ids: list[str], lang: str, channel: str) -> list[Button]:
        if channel == "voice":
            return []
        return [Button(id=i, title=self.texts.button(i, lang)) for i in ids]

    async def _suppress(self, tenant: Tenant, conversation: Conversation, inbound: Inbound) -> None:
        subject = self.keyring.blind_index(
            inbound.phone or inbound.channel_key, tenant.id, "phone" if inbound.phone else "channel"
        )
        async with self.conversations.db.tenant_scope(tenant.id) as session:
            from sqlalchemy import select

            existing = await session.scalar(
                select(Suppression).where(
                    Suppression.tenant_id == tenant.id,
                    Suppression.channel == inbound.channel,
                    Suppression.subject_bidx == subject,
                )
            )
            if existing is None:
                session.add(
                    Suppression(
                        tenant_id=tenant.id,
                        channel=inbound.channel,
                        subject_bidx=subject,
                        reason="stop",
                    )
                )
            await session.commit()
        await self.conversations.update(tenant.id, conversation.id, stopped_at=self.clock.now())
        if self.stop_hook is not None:
            await self.stop_hook(tenant.id, inbound.phone)

    async def _journey_button(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        guest: Guest,
        conversation: Conversation,
        button_id: str,
        lang: str,
        result: BrainResult,
    ) -> bool:
        assert self.journey_replies is not None
        if button_id.startswith("eta_"):
            text = await self.journey_replies.eta_window(
                tenant, guest, conversation, button_id, lang
            )
            if text is None:
                return False
            result.messages.append(Outbound(text, lang, intent="A:eta"))
            result.events.append(f"journey:{button_id}")
            return True
        text, handoff_id = await self.journey_replies.stay_feedback(
            tenant, profile, guest, conversation, button_id == "stay_ok", lang
        )
        buttons = self._buttons(["person"], lang, conversation.channel) if handoff_id else []
        result.messages.append(
            Outbound(
                text,
                lang,
                intent="A:stay_ok" if handoff_id is None else "H:complaint",
                buttons=buttons,
            )
        )
        if handoff_id is not None:
            result.handoff_id = handoff_id
            result.events.append("handoff:P1:stay_check")
        result.events.append(f"journey:{button_id}")
        return True

    async def _finish(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        conversation: Conversation,
        result: BrainResult,
    ) -> BrainResult:
        retention = profile.retention.chat_days
        for outbound in result.messages:
            record = await self.conversations.record_outbound(
                tenant.id,
                conversation.id,
                outbound.text,
                lang=outbound.lang,
                kind=outbound.kind,
                intent=outbound.intent,
                meta={"buttons": [b.id for b in outbound.buttons]} if outbound.buttons else {},
                retention_days=retention,
            )
            outbound.message_id = record.id
        if (
            result.mode not in {"silent"}
            and conversation.status == "new"
            and result.handoff_id is None
        ):
            await self.conversations.update(tenant.id, conversation.id, status="bot_handling")
        if self.qa_sink is not None:
            await self.qa_sink(tenant.id, conversation.id, result)
        return result

    async def _emergency_degrade(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        conversation: Conversation,
        guest: Guest,
        inbound: Inbound,
    ) -> BrainResult:
        lang = conversation.lang or profile.default_language
        result = BrainResult(conversation.id, guest.id, lang, "degraded", [])
        result.messages.append(
            Outbound(
                self.texts.render("degraded", lang, phone=profile.contacts.reception_phone or "-"),
                lang,
                intent="H:degraded",
            )
        )
        try:
            await self._handoff(
                tenant,
                profile,
                conversation,
                result,
                "P2",
                "engine_failure",
                "unexpected engine failure",
            )
            await self._finish(tenant, profile, conversation, result)
        except Exception:
            log.exception("brain.degrade_failed", tenant=tenant.id, conversation=conversation.id)
        return result


def _history_messages(history: list[tuple[str, str]]) -> list[Message]:
    """Text-only history with merged consecutive roles, starting with a user turn."""
    merged: list[tuple[str, str]] = []
    for role, text in history:
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + "\n\n" + text)
        else:
            merged.append((role, text))
    while merged and merged[0][0] != "user":
        merged.pop(0)
    return [Message(role, [TextPart(text)]) for role, text in merged]  # type: ignore[arg-type]


def _parse_reply(text: str) -> AgentReply:
    body = text.strip()
    if not body.startswith("{"):
        start, end = body.find("{"), body.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object in reply")
        body = body[start : end + 1]
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON") from exc
    try:
        return AgentReply.model_validate(data)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def _slots_from_meta(previous: Any) -> Slots | None:
    if not isinstance(previous, dict):
        return None

    def _day(value: Any) -> date | None:
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    adults = previous.get("adults")
    children = previous.get("children") or 0
    return Slots(
        arrival=_day(previous.get("arrival")),
        departure=_day(previous.get("departure")),
        adults=int(adults) if adults is not None else None,
        children=int(children),
    )


def _normalise_question(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())[:200]


def _stay_date(conversation: Conversation) -> date | None:
    value = conversation.meta.get("stay_date")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def window_expiry(received_at: datetime) -> datetime:
    return received_at + timedelta(hours=24)


def local_today(clock: Clock, tz: str) -> date:
    return local_date(clock.now(), tz)


DEFAULT_HANDOFF_DECISION = HandoffDecision(needed=False)
