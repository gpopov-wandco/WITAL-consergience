"""SLA timers (F17): a job per tenant that escalates missed timers and repeats P0 alerts.

Implements: F17, F18, C16

Runs every minute through the job table (dedupe key per tenant), so a restart never
loses a timer and a crashed run is retried. Escalations set ``sla_breached`` and alert
the contacts again; an unacknowledged P0 is repeated to the next contact.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

from concierge.clock import Clock
from concierge.db.models import Job, Tenant
from concierge.handoff.service import HandoffService
from concierge.jobs.service import JobService
from concierge.log import get_logger
from concierge.notify.alerts import StaffAlerts
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

log = get_logger(__name__)

JOB_KIND = "handoff_timers"
INTERVAL = timedelta(seconds=60)


class HandoffTimers:
    def __init__(
        self,
        clock: Clock,
        jobs: JobService,
        tenants: TenantService,
        handoffs: HandoffService,
        alerts: StaffAlerts,
    ) -> None:
        self.clock = clock
        self.jobs = jobs
        self.tenants = tenants
        self.handoffs = handoffs
        self.alerts = alerts
        self.extra: list[Callable[[Tenant], Awaitable[None]]] = []
        """Other per-tenant minute maintenance (voice reconciliation registers here)."""

    async def schedule_all(self) -> int:
        count = 0
        for tenant in await self.tenants.list():
            await self.schedule(tenant.id)
            count += 1
        return count

    async def schedule(self, tenant_id: str, *, delay: timedelta = timedelta(0)) -> None:
        """One job per tenant and minute: a finished dedupe key never runs again (jobs
        table semantics), so the key carries the minute the run is due."""
        run_at = self.clock.now() + delay
        await self.jobs.enqueue(
            JOB_KIND,
            {"tenant_id": tenant_id},
            run_at=run_at,
            tenant_id=tenant_id,
            dedupe_key=f"maintenance:handoff_timers:{tenant_id}:{run_at:%Y%m%d%H%M}",
            max_attempts=3,
        )

    async def run(self, job: Job) -> None:
        tenant_id = str(job.payload["tenant_id"])
        tenant = await self.tenants.get_by_id(tenant_id)
        if tenant is None:
            return
        profile = self.tenants.profile_of(tenant)
        try:
            await self._tick(tenant, tenant_id, profile)
        finally:
            await self.schedule(tenant_id, delay=INTERVAL)  # a failure never stops the timers

    async def _tick(self, tenant: Tenant, tenant_id: str, profile: TenantProfile) -> None:
        changed = await self.handoffs.check_timers(tenant_id, profile)
        for handoff in changed:
            await self.alerts.on_handoff(tenant, profile, handoff, level=handoff.escalation_level)
            log.info(
                "handoff.escalated",
                tenant=tenant_id,
                handoff=handoff.id,
                priority=handoff.priority,
                level=handoff.escalation_level,
                breached=handoff.sla_breached,
            )
        for hook in self.extra:
            try:
                await hook(tenant)
            except Exception:  # one failing hook never stops the timers
                log.exception("handoff_timers.hook_failed", tenant=tenant_id)
