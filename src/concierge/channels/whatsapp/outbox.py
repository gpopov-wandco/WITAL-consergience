"""Outbox: idempotent sends with states, retries and Meta error actions (6.6).

Implements: 6.6, F18, F24, F32, C9

States: ``queued`` → ``sending`` → ``sent`` | ``failed`` | ``unknown`` (timeout,
reconciled by the status webhook, never blindly resent) | ``dead`` (retries
exhausted). A worker killed between ``sending`` and ``sent`` leaves the row in
``sending``; the retry sees that and marks it ``unknown`` instead of resending.
Retries cover 429, 5xx and network errors with jittered backoff, at most five
attempts, then ``dead`` and an alert.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from concierge.channels.whatsapp.errors import ALERT_ACTIONS, RETRYABLE_ACTIONS, action_for
from concierge.channels.whatsapp.port import WhatsAppPort
from concierge.clock import Clock
from concierge.db.models import Message, Outbox, Suppression, Tenant
from concierge.db.session import Database
from concierge.jobs.service import JobService
from concierge.log import get_logger
from concierge.security.crypto import Keyring
from concierge.tenants.secrets import SecretStore

log = get_logger(__name__)

AlertSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
MAX_ATTEMPTS = 5
UNKNOWN_RECONCILE_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True)
class SendOutcome:
    state: str
    action: str = ""
    retry_after_s: float | None = None
    error_code: str | None = None


class RetryableSend(Exception):
    def __init__(self, delay_s: float | None) -> None:
        super().__init__("retryable send failure")
        self.delay_s = delay_s


class OutboxService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        port: WhatsAppPort,
        secrets: SecretStore,
        jobs: JobService,
        keyring: Keyring,
        *,
        alert_sink: AlertSink | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.port = port
        self.secrets = secrets
        self.jobs = jobs
        self.keyring = keyring
        self.alert_sink = alert_sink
        self._token_invalid: set[str] = set()

    # ------------------------------------------------------------ enqueue
    async def enqueue(
        self,
        tenant_id: str,
        *,
        payload: dict[str, Any],
        idempotency_key: str,
        conversation_id: str | None = None,
        category: str = "service",
        message_id: str | None = None,
        run_at: Any = None,
        channel: str = "whatsapp",
    ) -> Outbox:
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
                channel=channel,
                idempotency_key=idempotency_key,
                payload=payload,
                category=category,
                message_id=message_id,
                next_attempt_at=run_at or self.clock.now(),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        await self.jobs.enqueue(
            "outbox_send",
            {"outbox_id": row.id, "tenant_id": tenant_id},
            run_at=run_at,
            tenant_id=tenant_id,
            dedupe_key=f"outbox:{row.id}",
            max_attempts=MAX_ATTEMPTS,
        )
        return row

    # --------------------------------------------------------------- send
    async def send(self, tenant_id: str, outbox_id: str) -> SendOutcome:
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(Outbox, outbox_id)
            if row is None:
                return SendOutcome("missing")
            if row.state in {"sent", "failed", "dead", "unknown"}:
                return SendOutcome(row.state)
            if row.state == "sending":
                # a previous worker died between `sending` and `sent`: never resend blindly
                row.state = "unknown"
                row.updated_at = now
                row.error_message = "send interrupted; awaiting status webhook"
                await session.commit()
                return SendOutcome("unknown", action="interrupted")
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None or not tenant.phone_number_id:
                row.state = "failed"
                row.error_message = "tenant has no WhatsApp number mapped"
                await session.commit()
                return SendOutcome("failed", action="not_configured")
            if tenant_id in self._token_invalid:
                row.state = "failed"
                row.error_code = "190"
                row.error_message = "access token marked invalid; run `concierge doctor --live`"
                await session.commit()
                return SendOutcome("failed", action="token_invalid", error_code="190")
            row.state = "sending"
            row.attempts += 1
            await session.commit()
            phone_number_id = tenant.phone_number_id
            payload = dict(row.payload)
            message_id = row.message_id
        token = await self.secrets.get(tenant_id, "wa_access_token") or ""
        result = await self.port.send_message(phone_number_id, payload, access_token=token)
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(Outbox, outbox_id)
            assert row is not None
            if result.ok:
                row.state = "sent"
                row.sent_at = now
                row.provider_message_id = result.wamid
                row.error_code = None
                row.error_message = None
                if message_id:
                    message = await session.get(Message, message_id)
                    if message is not None:
                        message.wamid = result.wamid
                        message.status = "sent"
                        message.status_rank = 3
                await session.commit()
                return SendOutcome("sent")
            if result.unknown:
                row.state = "unknown"
                row.updated_at = now
                row.error_message = "timeout; awaiting status webhook"
                await session.commit()
                return SendOutcome("unknown", action="timeout")
            error = result.error
            assert error is not None
            action = action_for(error.code, error.status)
            row.error_code = str(error.code)
            row.error_message = error.message[:300]
            if action in RETRYABLE_ACTIONS and row.attempts < MAX_ATTEMPTS:
                row.state = "queued"
                delay = error.retry_after if error.retry_after else None
                await session.commit()
                raise RetryableSend(delay)
            row.state = "dead" if action in RETRYABLE_ACTIONS else "failed"
            if message_id:
                message = await session.get(Message, message_id)
                if message is not None:
                    message.status = "failed"
                    message.status_rank = 9
            if action == "opted_out":
                await self._record_opt_out(session, tenant_id, str(payload.get("to", "")))
            await session.commit()
        if action == "token_invalid":
            self._token_invalid.add(tenant_id)
        if action in ALERT_ACTIONS or row.state == "dead":
            await self._alert(
                action if action in ALERT_ACTIONS else "outbox_dead",
                tenant_id,
                {"outbox_id": outbox_id, "code": str(error.code)},
            )
        return SendOutcome(row.state, action=action, error_code=str(error.code))

    async def _record_opt_out(self, session: Any, tenant_id: str, to: str) -> None:
        if not to:
            return
        subject = self.keyring.blind_index("+" + to.lstrip("+"), tenant_id, "phone")
        existing = await session.scalar(
            select(Suppression).where(
                Suppression.tenant_id == tenant_id,
                Suppression.channel == "whatsapp",
                Suppression.subject_bidx == subject,
            )
        )
        if existing is None:
            session.add(
                Suppression(
                    tenant_id=tenant_id,
                    channel="whatsapp",
                    subject_bidx=subject,
                    reason="meta_opt_out",
                )
            )

    async def _alert(self, kind: str, tenant_id: str, details: dict[str, Any]) -> None:
        log.warning("outbox.alert", kind=kind, tenant=tenant_id, **details)
        if self.alert_sink is not None:
            await self.alert_sink(kind, tenant_id, details)

    def clear_token_invalid(self, tenant_id: str) -> None:
        self._token_invalid.discard(tenant_id)

    def token_invalid_tenants(self) -> set[str]:
        return set(self._token_invalid)

    # ------------------------------------------------------- reconciling
    async def reconcile_unknown(
        self, tenant_id: str, wamid: str, recipient: str, status: str
    ) -> bool:
        """Attach a status for an unknown wamid to an ``unknown`` outbox row sent recently."""
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(Outbox)
                    .where(
                        Outbox.tenant_id == tenant_id,
                        Outbox.state == "unknown",
                        Outbox.updated_at >= now - UNKNOWN_RECONCILE_WINDOW,
                    )
                    .order_by(Outbox.updated_at)
                )
            ).scalars()
            for row in rows:
                if str(row.payload.get("to", "")) == recipient:
                    row.state = "sent" if status != "failed" else "failed"
                    row.provider_message_id = wamid
                    row.sent_at = now
                    if row.message_id:
                        message = await session.get(Message, row.message_id)
                        if message is not None:
                            message.wamid = wamid
                    await session.commit()
                    return True
        return False

    async def counts(self, tenant_id: str | None = None) -> dict[str, int]:
        async with self.db.scope() as session:
            stmt = select(Outbox.state)
            if tenant_id:
                stmt = stmt.where(Outbox.tenant_id == tenant_id)
            rows = (await session.execute(stmt)).all()
        counts: dict[str, int] = {}
        for (state,) in rows:
            counts[state] = counts.get(state, 0) + 1
        return counts
