"""E-mail outbox (F18): idempotent rows, retries with backoff, sender identity footer.

Implements: F18, C16, UWG § 7 Abs. 2 Nr. 3 (sender identity and a stop address)

Rows share the ``outbox`` table with WhatsApp (``channel = email``). Every
business-initiated e-mail ends with the hotel's identity and an address to stop
messages; transactional replies to a guest's own request carry the identity only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from concierge.clock import Clock
from concierge.db.models import Outbox
from concierge.db.session import Database
from concierge.jobs.service import JobService, backoff_seconds
from concierge.log import get_logger
from concierge.notify.port import EmailMessage, EmailPort
from concierge.tenants.profile import TenantProfile

log = get_logger(__name__)

JOB_KIND = "email_send"
MAX_ATTEMPTS = 5


class RetryableEmail(Exception):
    pass


@dataclass
class EmailOutcome:
    state: str
    error: str = ""


def footer(profile: TenantProfile, *, business_initiated: bool) -> str:
    lines = ["", "--", profile.name]
    if profile.contacts.email:
        lines.append(profile.contacts.email)
    if profile.contacts.reception_phone:
        lines.append(profile.contacts.reception_phone)
    if profile.contacts.imprint_url:
        lines.append(profile.contacts.imprint_url)
    if business_initiated:
        stop = profile.contacts.email or profile.contacts.reception_phone or "-"
        lines.append(f"Keine weiteren Nachrichten / no more messages: {stop}")
    return "\n".join(lines)


class EmailOutbox:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        jobs: JobService,
        port: EmailPort | None,
        *,
        sender: str,
    ) -> None:
        self.db = db
        self.clock = clock
        self.jobs = jobs
        self.port = port
        self.sender = sender

    @property
    def configured(self) -> bool:
        return self.port is not None and bool(self.sender)

    async def enqueue(
        self,
        tenant_id: str,
        profile: TenantProfile,
        *,
        to: str,
        subject: str,
        text: str,
        idempotency_key: str,
        reply_to: str | None = None,
        business_initiated: bool = False,
        conversation_id: str | None = None,
        category: str = "email",
    ) -> Outbox:
        payload: dict[str, Any] = {
            "to": to,
            "subject": subject[:200],
            "text": text + footer(profile, business_initiated=business_initiated),
            "reply_to": reply_to or profile.contacts.email or None,
        }
        async with self.db.tenant_scope(tenant_id) as session:
            existing = await session.scalar(
                select(Outbox).where(
                    Outbox.tenant_id == tenant_id, Outbox.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return existing
            row = Outbox(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel="email",
                idempotency_key=idempotency_key,
                payload=payload,
                category=category,
                next_attempt_at=self.clock.now(),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        await self.jobs.enqueue(
            JOB_KIND,
            {"outbox_id": row.id, "tenant_id": tenant_id},
            tenant_id=tenant_id,
            dedupe_key=f"email:{row.id}",
            max_attempts=MAX_ATTEMPTS,
        )
        return row

    async def send(self, tenant_id: str, outbox_id: str) -> EmailOutcome:
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(Outbox, outbox_id)
            if row is None or row.channel != "email":
                return EmailOutcome("missing")
            if row.state in {"sent", "failed", "dead"}:
                return EmailOutcome(row.state)
            if self.port is None or not self.sender:
                row.state = "failed"
                row.error_message = "e-mail is not configured (GC_EMAIL_PROVIDER, GC_EMAIL_FROM)"
                await session.commit()
                return EmailOutcome("failed", row.error_message)
            row.state = "sending"
            row.attempts += 1
            await session.commit()
            payload = dict(row.payload)
            attempts = row.attempts
        message = EmailMessage(
            to=str(payload["to"]),
            subject=str(payload["subject"]),
            text=str(payload["text"]),
            reply_to=payload.get("reply_to") or None,
            idempotency_key=f"{tenant_id}:{outbox_id}",
            tenant_id=tenant_id,
        )
        result = await self.port.send(message, sender=self.sender)
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(Outbox, outbox_id)
            assert row is not None
            if result.ok:
                row.state = "sent"
                row.sent_at = self.clock.now()
                row.provider_message_id = result.provider_id[:128] or None
                await session.commit()
                return EmailOutcome("sent")
            row.error_message = result.error[:300]
            if result.retryable and attempts < MAX_ATTEMPTS:
                row.state = "queued"
                delay = backoff_seconds(attempts)
                row.next_attempt_at = now
                await session.commit()
                raise RetryableEmail(f"retry in {delay:.0f}s: {result.error}")
            row.state = "dead" if result.retryable else "failed"
            await session.commit()
            log.warning(
                "email.send_failed",
                tenant=tenant_id,
                outbox=outbox_id,
                state=row.state,
                error=result.error[:120],
            )
            return EmailOutcome(row.state, result.error)

    async def rows(self, tenant_id: str, *, limit: int = 50) -> list[Outbox]:
        async with self.db.tenant_scope(tenant_id) as session:
            return list(
                (
                    await session.execute(
                        select(Outbox)
                        .where(Outbox.tenant_id == tenant_id, Outbox.channel == "email")
                        .order_by(Outbox.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )
