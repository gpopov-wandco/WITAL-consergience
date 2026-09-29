"""Analytics (F27): period summaries for the owner dashboard and the Monday digest.

Implements: F27, F24, F25

Everything is computed from stored rows; messages flagged as special-category data
are left out (F25). Charts are inline SVG; the digest is text with an HTML variant.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from concierge.clock import Clock, format_local
from concierge.db.models import Conversation, Handoff, KbGap, Message, Reservation, UsageEvent
from concierge.db.session import Database
from concierge.reports.charts import bar_chart
from concierge.tenants.profile import TenantProfile

MICRO = 1_000_000


@dataclass
class Summary:
    start: datetime
    end: datetime
    conversations: int = 0
    by_language: dict[str, int] = field(default_factory=dict)
    by_channel: dict[str, int] = field(default_factory=dict)
    topics: dict[str, int] = field(default_factory=dict)
    unanswered: list[tuple[str, int]] = field(default_factory=list)
    handoffs: dict[str, int] = field(default_factory=dict)
    median_first_response_min: float | None = None
    sla_breaches: int = 0
    wa_by_category: dict[str, int] = field(default_factory=dict)
    llm_cost_eur: float = 0.0
    voice_minutes: float = 0.0
    journey: dict[str, int] = field(default_factory=dict)
    excluded_special: int = 0


class AnalyticsService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def summary(
        self, tenant_id: str, profile: TenantProfile, start: datetime, end: datetime
    ) -> Summary:
        summary = Summary(start=start, end=end)
        async with self.db.tenant_scope(tenant_id) as session:
            conversations = list(
                (
                    await session.execute(
                        select(Conversation).where(
                            Conversation.tenant_id == tenant_id,
                            Conversation.created_at >= start,
                            Conversation.created_at < end,
                        )
                    )
                ).scalars()
            )
            special_conversations = {
                c.id
                for c in conversations
                if isinstance(c.meta, dict) and c.meta.get("special_category")
            }
            summary.excluded_special = len(special_conversations)
            counted = [c for c in conversations if c.id not in special_conversations]
            summary.conversations = len(counted)
            for conversation in counted:
                summary.by_language[conversation.lang] = (
                    summary.by_language.get(conversation.lang, 0) + 1
                )
                summary.by_channel[conversation.channel] = (
                    summary.by_channel.get(conversation.channel, 0) + 1
                )
            messages = (
                await session.execute(
                    select(Message.intent, Message.conversation_id, Message.meta).where(
                        Message.tenant_id == tenant_id,
                        Message.direction == "out",
                        Message.occurred_at >= start,
                        Message.occurred_at < end,
                    )
                )
            ).all()
            for intent, conversation_id, meta in messages:
                if conversation_id in special_conversations:
                    continue
                if isinstance(meta, dict) and meta.get("special_category"):
                    continue
                topic = _topic(intent)
                if topic:
                    summary.topics[topic] = summary.topics.get(topic, 0) + 1
            gaps = list(
                (
                    await session.execute(
                        select(KbGap)
                        .where(
                            KbGap.tenant_id == tenant_id,
                            KbGap.status == "open",
                            KbGap.updated_at >= start,
                        )
                        .order_by(KbGap.count.desc())
                        .limit(10)
                    )
                ).scalars()
            )
            summary.unanswered = [(g.question[:120], g.count) for g in gaps]
            handoffs = list(
                (
                    await session.execute(
                        select(Handoff).where(
                            Handoff.tenant_id == tenant_id,
                            Handoff.created_at >= start,
                            Handoff.created_at < end,
                        )
                    )
                ).scalars()
            )
            response_minutes: list[float] = []
            for handoff in handoffs:
                summary.handoffs[handoff.priority] = summary.handoffs.get(handoff.priority, 0) + 1
                if handoff.sla_breached:
                    summary.sla_breaches += 1
                if handoff.acknowledged_at is not None:
                    delta = handoff.acknowledged_at - handoff.created_at
                    response_minutes.append(delta.total_seconds() / 60)
            if response_minutes:
                summary.median_first_response_min = round(statistics.median(response_minutes), 1)
            usage = (
                await session.execute(
                    select(
                        UsageEvent.kind,
                        UsageEvent.category,
                        func.count(),
                        func.sum(UsageEvent.quantity),
                        func.sum(UsageEvent.cost_micro_eur),
                    )
                    .where(
                        UsageEvent.tenant_id == tenant_id,
                        UsageEvent.occurred_at >= start,
                        UsageEvent.occurred_at < end,
                    )
                    .group_by(UsageEvent.kind, UsageEvent.category)
                )
            ).all()
            for kind, category, count, quantity, cost in usage:
                if kind == "whatsapp" and category != "correction":
                    summary.wa_by_category[category or "-"] = int(count)
                elif kind == "llm":
                    summary.llm_cost_eur += (cost or 0) / MICRO
                elif kind == "voice":
                    summary.voice_minutes += (quantity or 0) / 60
            reservations = (
                await session.execute(
                    select(Reservation.meta).where(Reservation.tenant_id == tenant_id)
                )
            ).all()
            for (meta,) in reservations:
                for entry in (meta or {}).get("journey_log", []):
                    if not isinstance(entry, dict):
                        continue
                    at = str(entry.get("at", ""))
                    if start.isoformat() <= at < end.isoformat():
                        outcome = str(entry.get("outcome", ""))
                        summary.journey[outcome] = summary.journey.get(outcome, 0) + 1
        summary.llm_cost_eur = round(summary.llm_cost_eur, 4)
        summary.voice_minutes = round(summary.voice_minutes, 1)
        return summary

    @staticmethod
    def charts(summary: Summary, lang: str = "de") -> dict[str, str]:
        titles = {
            "de": ("Gespräche nach Sprache", "Themen", "Übergaben nach Priorität"),
            "en": ("Conversations by language", "Topics", "Handoffs by priority"),
        }[lang if lang in {"de", "en"} else "de"]
        topics = sorted(summary.topics.items(), key=lambda kv: -kv[1])[:8]
        return {
            "languages": bar_chart(
                list(summary.by_language), list(summary.by_language.values()), title=titles[0]
            ),
            "topics": bar_chart([t for t, _ in topics], [n for _, n in topics], title=titles[1]),
            "handoffs": bar_chart(
                sorted(summary.handoffs),
                [summary.handoffs[k] for k in sorted(summary.handoffs)],
                title=titles[2],
            ),
        }

    def digest(
        self, summary: Summary, profile: TenantProfile, *, dashboard_url: str
    ) -> tuple[str, str, str]:
        """(subject, text, html) of the weekly digest in the tenant's default language."""
        lang = "en" if profile.default_language == "en" else "de"
        period = (
            f"{format_local(summary.start, profile.timezone, lang)} – "
            f"{format_local(summary.end, profile.timezone, lang)}"
        )
        labels = {
            "de": {
                "subject": f"{profile.name}: Wochenbericht Guest Concierge",
                "intro": f"Wochenbericht für {period}",
                "conversations": "Gespräche",
                "languages": "Sprachen",
                "topics": "Themen",
                "unanswered": "Unbeantwortete Fragen",
                "handoffs": "Übergaben",
                "response": "Mediane erste Reaktion (Min.)",
                "breaches": "SLA-Verletzungen",
                "wa": "WhatsApp-Nachrichten nach Kategorie",
                "llm": "Modellkosten (EUR)",
                "voice": "Telefonminuten",
                "journey": "Journey-Zustellung",
                "link": "Zum Dashboard",
                "none": "keine",
            },
            "en": {
                "subject": f"{profile.name}: Guest Concierge weekly report",
                "intro": f"Weekly report for {period}",
                "conversations": "Conversations",
                "languages": "Languages",
                "topics": "Topics",
                "unanswered": "Unanswered questions",
                "handoffs": "Handoffs",
                "response": "Median first response (min)",
                "breaches": "SLA breaches",
                "wa": "WhatsApp messages by category",
                "llm": "Model cost (EUR)",
                "voice": "Phone minutes",
                "journey": "Journey delivery",
                "link": "Open the dashboard",
                "none": "none",
            },
        }[lang]

        def fmt(mapping: dict[str, Any]) -> str:
            return ", ".join(f"{k}: {v}" for k, v in mapping.items()) or labels["none"]

        lines = [
            labels["intro"],
            "",
            f"{labels['conversations']}: {summary.conversations}",
            f"{labels['languages']}: {fmt(summary.by_language)}",
            f"{labels['topics']}: {fmt(dict(sorted(summary.topics.items(), key=lambda kv: -kv[1])[:8]))}",
            f"{labels['unanswered']}: "
            + ("; ".join(f"{q} ({n})" for q, n in summary.unanswered[:5]) or labels["none"]),
            f"{labels['handoffs']}: {fmt(summary.handoffs)}",
            f"{labels['response']}: {summary.median_first_response_min if summary.median_first_response_min is not None else '-'}",
            f"{labels['breaches']}: {summary.sla_breaches}",
            f"{labels['wa']}: {fmt(summary.wa_by_category)}",
            f"{labels['llm']}: {summary.llm_cost_eur:.2f}",
            f"{labels['voice']}: {summary.voice_minutes:.1f}",
            f"{labels['journey']}: {fmt(summary.journey)}",
            "",
            f"{labels['link']}: {dashboard_url}",
        ]
        charts = self.charts(summary, lang)
        html = (
            '<!doctype html><html><body style="font-family:sans-serif">'
            + "".join(f"<p>{_html(line)}</p>" for line in lines if line)
            + "".join(f"<div>{svg}</div>" for svg in charts.values())
            + "</body></html>"
        )
        return labels["subject"], "\n".join(lines), html


def _topic(intent: str | None) -> str:
    if not intent or ":" not in intent:
        return ""
    code, topic = intent.split(":", 1)
    if code not in {"A", "P", "Q", "H", "R"} or not topic:
        return ""
    return topic.split(".")[0]


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
