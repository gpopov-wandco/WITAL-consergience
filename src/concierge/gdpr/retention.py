"""Retention (F25): per-tenant deletion by policy with a content-free deletion log.

Implements: F25, F26, F24, C16

Defaults come from the tenant profile (lawyer-review proposals): chat texts expire
``chat_days`` after the message, special-category texts after the stay (or seven
days), booking-linked personal data ``booking_days_after_departure`` after departure,
voice transcripts ``voice_transcript_days``, webhook payloads ``webhook_days``, usage
events ``metering_months``, withdrawn consents ``consent_years`` after withdrawal.
Suppression rows survive everything. Every pass logs table and count, never content.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import exists, select

from concierge.clock import Clock
from concierge.db.models import (
    CallbackTask,
    Consent,
    Conversation,
    ConversationNote,
    DeletionLog,
    Guest,
    Message,
    MessageText,
    Reservation,
    Tenant,
    UsageEvent,
    VoiceCall,
    WebhookEvent,
)
from concierge.db.session import ALLOW_APPEND_ONLY_DELETE, Database
from concierge.log import get_logger
from concierge.tenants.profile import TenantProfile

log = get_logger(__name__)

SPECIAL_GRACE_DAYS = 7


class RetentionService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def run(self, tenant: Tenant, profile: TenantProfile) -> dict[str, int]:
        now = self.clock.now()
        today = now.date()
        counts: dict[str, int] = {}
        retention = profile.retention
        async with self.db.tenant_scope(tenant.id) as session:
            session.info[ALLOW_APPEND_ONLY_DELETE] = True

            def log_deletion(table: str, count: int, reason: str = "retention") -> None:
                if count:
                    counts[table] = counts.get(table, 0) + count
                    session.add(
                        DeletionLog(
                            tenant_id=tenant.id, table_name=table, row_count=count, reason=reason
                        )
                    )

            # 1. chat texts past their expiry
            expired = list(
                (
                    await session.execute(
                        select(MessageText).where(
                            MessageText.tenant_id == tenant.id, MessageText.expires_at <= now
                        )
                    )
                ).scalars()
            )
            for text in expired:
                await session.delete(text)
            log_deletion("message_text", len(expired))

            # 2. special-category texts after the stay or the grace period
            special = (
                await session.execute(
                    select(Message, MessageText)
                    .join(MessageText, MessageText.id == Message.id)
                    .where(Message.tenant_id == tenant.id)
                )
            ).all()
            scrubbed = 0
            for message, text in special:
                if not (isinstance(message.meta, dict) and message.meta.get("special_category")):
                    continue
                if message.occurred_at + timedelta(days=SPECIAL_GRACE_DAYS) > now and not (
                    await self._stay_over(session, message.conversation_id, today)
                ):
                    continue
                await session.delete(text)
                scrubbed += 1
            log_deletion("message_text", scrubbed, reason="special_category")

            # 3. voice transcripts
            calls = list(
                (
                    await session.execute(
                        select(VoiceCall).where(
                            VoiceCall.tenant_id == tenant.id,
                            VoiceCall.transcript_expires_at.is_not(None),
                            VoiceCall.transcript_expires_at <= now,
                        )
                    )
                ).scalars()
            )
            for call in calls:
                call.transcript = None
                call.transcript_expires_at = None
                call.summary_de = ""
            log_deletion("voice_call.transcript", len(calls))

            # 4. booking-linked personal data after departure plus the retention window
            cutoff = today - timedelta(days=retention.booking_days_after_departure)
            reservations = list(
                (
                    await session.execute(
                        select(Reservation).where(
                            Reservation.tenant_id == tenant.id,
                            Reservation.departure <= cutoff,
                            Reservation.phone_bidx.is_not(None),
                        )
                    )
                ).scalars()
            )
            for reservation in reservations:
                reservation.name_enc = None
                reservation.name_bidx = None
                reservation.phone_enc = None
                reservation.phone_bidx = None
                reservation.email_enc = None
                reservation.email_bidx = None
                reservation.meta = {
                    k: v
                    for k, v in reservation.meta.items()
                    if k in {"journey_log", "late_arrival"}
                }
            log_deletion("reservation.personal", len(reservations))

            # 5. guests without live texts after the chat retention window
            stale_guests = list(
                (
                    await session.execute(
                        select(Guest).where(
                            Guest.tenant_id == tenant.id,
                            Guest.last_seen_at <= now - timedelta(days=retention.chat_days),
                            Guest.phone_enc.is_not(None) | Guest.name_enc.is_not(None),
                        )
                    )
                ).scalars()
            )
            scrubbed_guests = 0
            for guest in stale_guests:
                live = await session.scalar(
                    select(
                        exists().where(
                            Message.tenant_id == tenant.id,
                            Message.conversation_id.in_(
                                select(Conversation.id).where(Conversation.guest_id == guest.id)
                            ),
                            MessageText.id == Message.id,
                        )
                    )
                )
                if live:
                    continue
                guest.name_enc = None
                guest.name_bidx = None
                guest.phone_enc = None
                guest.phone_bidx = None
                guest.email_enc = None
                guest.email_bidx = None
                scrubbed_guests += 1
            log_deletion("guest.personal", scrubbed_guests)

            # 6. notes and callbacks of conversations whose texts are gone and are old
            old_notes = list(
                (
                    await session.execute(
                        select(ConversationNote).where(
                            ConversationNote.tenant_id == tenant.id,
                            ConversationNote.created_at
                            <= now - timedelta(days=retention.chat_days),
                        )
                    )
                ).scalars()
            )
            for note in old_notes:
                await session.delete(note)
            log_deletion("conversation_note", len(old_notes))
            done_callbacks = list(
                (
                    await session.execute(
                        select(CallbackTask).where(
                            CallbackTask.tenant_id == tenant.id,
                            CallbackTask.status != "open",
                            CallbackTask.updated_at <= now - timedelta(days=retention.chat_days),
                        )
                    )
                ).scalars()
            )
            for task in done_callbacks:
                await session.delete(task)
            log_deletion("callback_task", len(done_callbacks))

            # 7. webhook payloads
            hooks = list(
                (
                    await session.execute(
                        select(WebhookEvent).where(
                            WebhookEvent.tenant_id == tenant.id,
                            WebhookEvent.received_at
                            <= now - timedelta(days=retention.webhook_days),
                        )
                    )
                ).scalars()
            )
            for hook in hooks:
                await session.delete(hook)
            log_deletion("webhook_event", len(hooks))

            # 8. metering rows older than the metering window
            usage_cutoff = now - timedelta(days=30 * retention.metering_months)
            usage = list(
                (
                    await session.execute(
                        select(UsageEvent).where(
                            UsageEvent.tenant_id == tenant.id,
                            UsageEvent.occurred_at <= usage_cutoff,
                        )
                    )
                ).scalars()
            )
            for event in usage:
                await session.delete(event)
            log_deletion("usage_event", len(usage))

            # 9. withdrawn consents after the limitation period
            consent_cutoff = now - timedelta(days=365 * retention.consent_years)
            consents = list(
                (
                    await session.execute(
                        select(Consent).where(
                            Consent.tenant_id == tenant.id,
                            Consent.withdrawn_at.is_not(None),
                            Consent.withdrawn_at <= consent_cutoff,
                        )
                    )
                ).scalars()
            )
            for consent in consents:
                await session.delete(consent)
            log_deletion("consent", len(consents))
            await session.commit()
        if counts:
            log.info(
                "retention.run",
                tenant=tenant.id,
                **{k.replace(".", "_"): v for k, v in counts.items()},
            )
        return counts

    @staticmethod
    async def _stay_over(session: Any, conversation_id: str, today: date) -> bool:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.verified_reservation_id is None:
            return False
        reservation = await session.get(Reservation, conversation.verified_reservation_id)
        return reservation is not None and reservation.departure < today


def next_run_utc(now: datetime, tz: str, at_hour: int = 3) -> datetime:
    """The next local ``at_hour`` o'clock after ``now`` (daily jobs)."""
    from datetime import time

    from concierge.clock import local_to_utc, to_local

    local = to_local(now, tz)
    day = local.date() if local.hour < at_hour else local.date() + timedelta(days=1)
    return local_to_utc(day, time(at_hour, 0), tz)
