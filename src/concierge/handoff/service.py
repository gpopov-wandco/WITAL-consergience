"""Handoff records with SLA timers and conversation state transitions.

Implements: F17, C16

Priorities and default first-reply targets: P0 at once (alerts repeat every
``p0_repeat_min`` minutes to the next contact until acknowledged), P1 within
``sla.p1_min`` minutes in staffed hours, P2 within ``sla.p2_min`` minutes, P3 by the
next business morning. Outside staffed hours a P1 or P2 timer starts at the next
staffed start. A missed timer escalates and sets ``sla_breached``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from concierge.clock import Clock
from concierge.db.models import Conversation, ConversationNote, Handoff
from concierge.db.session import Database
from concierge.tenants.profile import TenantProfile

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
CreatedHook = Callable[[str, Handoff], Awaitable[None]]


@dataclass(frozen=True)
class HandoffCreated:
    id: str
    priority: str
    sla_due_at: datetime | None
    created: bool


class HandoffService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock
        self.on_created: list[CreatedHook] = []
        """Called after a new handoff is committed (staff alerts register here)."""

    def sla_due(self, profile: TenantProfile, priority: str, now: datetime) -> datetime | None:
        sla = profile.handoff.sla
        if priority == "P0":
            return now
        if priority == "P3":
            return profile.next_staffed_start(now)
        start = now if profile.is_staffed(now) else profile.next_staffed_start(now)
        minutes = sla.p1_min if priority == "P1" else sla.p2_min
        return start + timedelta(minutes=minutes)

    async def create(
        self,
        tenant_id: str,
        conversation_id: str,
        *,
        priority: str,
        trigger: str,
        reason: str,
        profile: TenantProfile,
        summary: str = "",
    ) -> HandoffCreated:
        if priority not in PRIORITY_ORDER:
            raise ValueError(f"unknown priority {priority!r}")
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            existing = await session.scalar(
                select(Handoff).where(
                    Handoff.tenant_id == tenant_id,
                    Handoff.conversation_id == conversation_id,
                    Handoff.status.in_(["open", "acknowledged", "escalated"]),
                )
            )
            conversation = await session.get(Conversation, conversation_id)
            if existing is not None:
                upgraded = PRIORITY_ORDER[priority] < PRIORITY_ORDER[existing.priority]
                if upgraded:
                    existing.priority = priority
                    existing.sla_due_at = self.sla_due(profile, priority, now)
                    existing.reason = reason
                    existing.trigger = trigger
                    if priority == "P0":
                        # an emergency re-alerts even when someone already took the case
                        existing.next_alert_at = now
                        if existing.status == "acknowledged":
                            existing.status = "escalated"
                    if conversation is not None:
                        conversation.priority = priority
                    await session.commit()
                    await session.refresh(existing)
                if upgraded and priority == "P0":
                    for hook in self.on_created:
                        await hook(tenant_id, existing)
                return HandoffCreated(
                    existing.id, existing.priority, existing.sla_due_at, created=False
                )
            due = self.sla_due(profile, priority, now)
            handoff = Handoff(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                priority=priority,
                trigger=trigger,
                reason=reason[:300],
                summary=summary,
                sla_due_at=due,
                next_alert_at=now if priority == "P0" else None,
            )
            session.add(handoff)
            if conversation is not None:
                conversation.status = "needs_staff"
                conversation.priority = priority
            await session.commit()
            await session.refresh(handoff)
        for hook in self.on_created:
            await hook(tenant_id, handoff)
        return HandoffCreated(handoff.id, priority, due, created=True)

    async def inbox(
        self, tenant_id: str, *, statuses: tuple[str, ...] = ("open", "acknowledged", "escalated")
    ) -> list[Handoff]:
        async with self.db.tenant_scope(tenant_id) as session:
            rows = list(
                (
                    await session.execute(
                        select(Handoff).where(
                            Handoff.tenant_id == tenant_id, Handoff.status.in_(list(statuses))
                        )
                    )
                ).scalars()
            )
        rows.sort(key=lambda h: (PRIORITY_ORDER.get(h.priority, 9), h.sla_due_at or h.created_at))
        return rows

    async def get(self, tenant_id: str, handoff_id: str) -> Handoff | None:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(Handoff, handoff_id)
            return row if row is not None and row.tenant_id == tenant_id else None

    async def add_note(
        self, tenant_id: str, conversation_id: str, *, user_id: str, text: str
    ) -> ConversationNote:
        async with self.db.tenant_scope(tenant_id) as session:
            note = ConversationNote(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                user_id=user_id,
                text=text.strip()[:2000],
                created_at=self.clock.now(),
            )
            session.add(note)
            await session.commit()
            await session.refresh(note)
            return note

    async def notes(self, tenant_id: str, conversation_id: str) -> list[ConversationNote]:
        async with self.db.tenant_scope(tenant_id) as session:
            return list(
                (
                    await session.execute(
                        select(ConversationNote)
                        .where(
                            ConversationNote.tenant_id == tenant_id,
                            ConversationNote.conversation_id == conversation_id,
                        )
                        .order_by(ConversationNote.created_at)
                    )
                ).scalars()
            )

    async def open_for_conversation(self, tenant_id: str, conversation_id: str) -> Handoff | None:
        async with self.db.tenant_scope(tenant_id) as session:
            return await session.scalar(
                select(Handoff).where(
                    Handoff.tenant_id == tenant_id,
                    Handoff.conversation_id == conversation_id,
                    Handoff.status.in_(["open", "acknowledged", "escalated"]),
                )
            )

    async def acknowledge(self, tenant_id: str, handoff_id: str, *, user_id: str) -> Handoff:
        async with self.db.tenant_scope(tenant_id) as session:
            handoff = await session.get(Handoff, handoff_id)
            if handoff is None:
                raise LookupError("handoff not found")
            handoff.status = "acknowledged"
            handoff.acknowledged_by = user_id
            handoff.acknowledged_at = self.clock.now()
            handoff.next_alert_at = None
            conversation = await session.get(Conversation, handoff.conversation_id)
            if conversation is not None:
                conversation.status = "staff_active"
                conversation.assigned_user_id = user_id
            await session.commit()
            await session.refresh(handoff)
            return handoff

    async def resolve(
        self, tenant_id: str, handoff_id: str, *, user_id: str, return_to_bot: bool = True
    ) -> Handoff:
        async with self.db.tenant_scope(tenant_id) as session:
            handoff = await session.get(Handoff, handoff_id)
            if handoff is None:
                raise LookupError("handoff not found")
            handoff.status = "resolved"
            handoff.resolved_at = self.clock.now()
            handoff.next_alert_at = None
            conversation = await session.get(Conversation, handoff.conversation_id)
            if conversation is not None:
                conversation.status = "bot_handling" if return_to_bot else "resolved"
                conversation.priority = None
                conversation.unresolved_turns = 0
            await session.commit()
            await session.refresh(handoff)
            return handoff

    async def check_timers(self, tenant_id: str, profile: TenantProfile) -> list[Handoff]:
        """Mark breached timers and escalate; returns the handoffs that changed."""
        now = self.clock.now()
        changed: list[Handoff] = []
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(Handoff).where(
                        Handoff.tenant_id == tenant_id, Handoff.status.in_(["open", "escalated"])
                    )
                )
            ).scalars()
            for handoff in rows:
                if (
                    handoff.sla_due_at is not None
                    and handoff.sla_due_at <= now
                    and not handoff.sla_breached
                ):
                    handoff.sla_breached = True
                    handoff.status = "escalated"
                    handoff.escalation_level += 1
                    handoff.next_alert_at = now
                    changed.append(handoff)
                elif (
                    handoff.priority == "P0"
                    and handoff.next_alert_at is not None
                    and handoff.next_alert_at <= now
                ):
                    handoff.escalation_level += 1
                    handoff.next_alert_at = now + timedelta(
                        minutes=profile.handoff.sla.p0_repeat_min
                    )
                    changed.append(handoff)
            await session.commit()
            for handoff in changed:
                await session.refresh(handoff)
        return changed
