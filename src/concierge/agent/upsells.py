"""Honest upsells (F16): one relevant, fully priced offer per conversation, never pushy.

Implements: F16, C6, C12, C17

Offers come only from approved KB ``upsells.*`` entries (price, what it includes,
conditions, availability check). An offer appears inside a relevant topic, at most once
per conversation, never in complaint or safety threads or after a "no". The guest
confirms explicitly; the assistant records a request and staff or the PMS post any
charge. Late check-out is requested, never confirmed. Offers outside a conversation are
marketing templates with their own opt-in and are not created here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from concierge.agent.schemas import Button
from concierge.agent.texts import GuestTexts
from concierge.db.models import Conversation
from concierge.kb.store import KbService, KbView

TOPIC_TO_UPSELL: dict[str, str] = {
    "breakfast": "upsells.breakfast",
    "checkout": "upsells.late_checkout",
    "late_checkout": "upsells.late_checkout",
}
BLOCKED_TOPICS: frozenset[str] = frozenset(
    {"complaint", "emergency", "allergy", "safety", "other_guest", "sensitive_data", "opt_out"}
)
BLOCKED_PRIORITIES: frozenset[str] = frozenset({"P0", "P1"})


@dataclass(frozen=True)
class Offer:
    upsell_id: str
    key: str
    text: str
    price_cents: int
    includes: str
    conditions: str
    availability_check: bool
    buttons: list[Button]


@dataclass(frozen=True)
class UpsellContext:
    conversation: Conversation
    intent_code: str
    topic: str
    handoff_needed: bool
    open_priority: str | None
    """Priority of an open handoff in this conversation, if any."""


class UpsellPolicy:
    def __init__(self, kb: KbService, texts: GuestTexts) -> None:
        self.kb = kb
        self.texts = texts

    @staticmethod
    def allowed(ctx: UpsellContext) -> tuple[bool, str]:
        """Whether an offer may appear; the reason names the rule that blocks it."""
        if ctx.intent_code != "A":
            return False, "not_an_answer"
        if ctx.topic not in TOPIC_TO_UPSELL:
            return False, "topic_not_relevant"
        if ctx.conversation.upsell_offered:
            return False, "already_offered"
        if ctx.conversation.meta.get("upsell_declined"):
            return False, "declined_before"
        if ctx.handoff_needed or ctx.topic in BLOCKED_TOPICS:
            return False, "handoff_or_blocked_topic"
        if ctx.open_priority in BLOCKED_PRIORITIES:
            return False, "open_urgent_handoff"
        if ctx.conversation.meta.get("complaint_open"):
            return False, "complaint_thread"
        return True, "ok"

    async def offer(self, tenant_id: str, ctx: UpsellContext, lang: str) -> Offer | None:
        ok, _reason = self.allowed(ctx)
        if not ok:
            return None
        view = await self.kb.lookup(tenant_id, TOPIC_TO_UPSELL[ctx.topic], lang)
        if view is None:
            return None
        return self.offer_from_view(view, lang)

    def offer_from_view(self, view: KbView, lang: str) -> Offer | None:
        value: dict[str, Any] = view.value or {}
        price = value.get("price_cents")
        includes = str(value.get("includes", "") or "")
        conditions = str(value.get("conditions", "") or "")
        if not isinstance(price, int) or price < 0 or not includes or not view.text.strip():
            return None
        text = f"{view.text.strip()} {self.texts.render('upsell_offer_question', lang)}"
        return Offer(
            upsell_id=str(value.get("id") or view.key.rsplit(".", 1)[-1]),
            key=view.key,
            text=text,
            price_cents=price,
            includes=includes,
            conditions=conditions,
            availability_check=bool(value.get("availability_check", False)),
            buttons=[
                Button(id="upsell_yes", title=self.texts.button("upsell_yes", lang)),
                Button(id="upsell_no", title=self.texts.button("upsell_no", lang)),
            ],
        )
