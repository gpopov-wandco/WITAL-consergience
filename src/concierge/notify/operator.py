"""Operator alert e-mails (F32): one deduplicated digest of open alerts.

Implements: F32, F18, F24

Alert rows (``operator_alert``) are deduplicated by kind and key when recorded; this
notifier mails the operator once per new alert and again only when an alert keeps
firing after ``resend_after``. It also raises the stale-price-table alert when the
newest dated price row is older than ``stale_after_days``. Delivery goes straight to
the e-mail port: operator mail belongs to no tenant.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from concierge.clock import Clock
from concierge.db.models import OperatorAlert, Tenant
from concierge.db.session import Database
from concierge.log import get_logger
from concierge.notify.port import EmailMessage, EmailPort

log = get_logger(__name__)

AlertSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
STALE_PRICE_DAYS = 180
# nosec B105 below: alert titles shown to the operator, not credentials
ALERT_TITLES = {
    "token_invalid": "WhatsApp access token invalid",  # nosec B105
    "spend_cap": "Model spend cap reached",
    "llm_degraded": "Model degraded (circuit open or failing)",
    "dead_job": "Job failed permanently",
    "template_paused": "WhatsApp template paused",
    "template_rejected": "WhatsApp template rejected",
    "template_disabled": "WhatsApp template disabled",
    "template_category_changed": "WhatsApp template category changed by Meta",
    "backup_failed": "Backup failed",
    "price_table_stale": "Price table older than 180 days",
    "journey_step_skipped": "Journey step skipped",
    "availability_breaker_open": "Availability source circuit open",
}


class OperatorNotifier:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        port: EmailPort | None,
        *,
        sender: str,
        operator_email: str,
        public_base_url: str,
        alert_sink: AlertSink,
        resend_after: timedelta = timedelta(hours=24),
        interval: timedelta = timedelta(minutes=15),
    ) -> None:
        self.db = db
        self.clock = clock
        self.port = port
        self.sender = sender
        self.operator_email = operator_email
        self.public_base_url = public_base_url
        self.alert_sink = alert_sink
        self.resend_after = resend_after
        self.interval = interval
        self.last_run: Any = None
        self.sent: list[EmailMessage] = []

    @property
    def configured(self) -> bool:
        return self.port is not None and bool(self.sender) and bool(self.operator_email)

    async def check_price_tables(self, newest: tuple[date | None, date | None]) -> None:
        today = self.clock.now().date()
        message_date, model_date = newest
        for label, value in (("whatsapp", message_date), ("model", model_date)):
            if value is not None and (today - value).days > STALE_PRICE_DAYS:
                await self.alert_sink(
                    "price_table_stale", "", {"kind": label, "newest": value.isoformat()}
                )

    async def due(self) -> bool:
        now = self.clock.now()
        if self.last_run is not None and now - self.last_run < self.interval:
            return False
        self.last_run = now
        return True

    async def run(self, *, force: bool = False) -> int:
        """Mail the pending alerts; returns how many alert rows the mail covered."""
        if not force and not await self.due():
            return 0
        now = self.clock.now()
        async with self.db.scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(OperatorAlert)
                        .where(OperatorAlert.resolved_at.is_(None))
                        .order_by(OperatorAlert.first_seen_at)
                    )
                ).scalars()
            )
            pending = [
                r
                for r in rows
                if r.last_sent_at is None or (now - r.last_sent_at) >= self.resend_after
            ]
            if not pending:
                return 0
            slugs = dict((await session.execute(select(Tenant.id, Tenant.slug))).all())
            if not self.configured:
                log.warning(
                    "operator_alerts.unsent", count=len(pending), reason="email not configured"
                )
                return 0
            lines = [
                f"{len(pending)} open operator alert(s) at {now.isoformat(timespec='minutes')}:",
                "",
            ]
            for row in pending:
                title = ALERT_TITLES.get(row.kind, row.kind)
                tenant = slugs.get(row.tenant_id or "", "-")
                lines.append(
                    f"- {title} [{row.kind}] tenant {tenant}, seen {row.count}x since "
                    f"{row.first_seen_at.isoformat(timespec='minutes')}"
                )
            lines += ["", f"Operator console: {self.public_base_url}/operator/"]
            message = EmailMessage(
                to=self.operator_email,
                subject=f"[Guest Concierge] {len(pending)} operator alert(s)",
                text="\n".join(lines),
                idempotency_key=f"opalerts:{now.strftime('%Y%m%d%H%M')}",
            )
            assert self.port is not None
            result = await self.port.send(message, sender=self.sender)
            if not result.ok:
                log.warning("operator_alerts.send_failed", error=result.error[:120])
                return 0
            self.sent.append(message)
            for row in pending:
                row.last_sent_at = now
            await session.commit()
        return len(pending)

    async def resolve(self, kind: str, alert_key: str) -> bool:
        async with self.db.scope() as session:
            row = await session.scalar(
                select(OperatorAlert).where(
                    OperatorAlert.kind == kind, OperatorAlert.alert_key == alert_key
                )
            )
            if row is None or row.resolved_at is not None:
                return False
            row.resolved_at = self.clock.now()
            await session.commit()
            return True
