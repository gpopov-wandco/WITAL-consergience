"""Scheduled per-tenant flows through the job table (F25, F27, F24, F32).

Implements: F25, F27, F24, F32, C16

Every flow is one job per tenant and local day (or ISO week), deduplicated by key, so a
restart never doubles a run and a crashed run is retried. Each handler reschedules the
next occurrence after it ran.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta

from concierge.clock import Clock, local_to_utc, to_local
from concierge.db.models import Job, Tenant
from concierge.jobs.service import JobService
from concierge.log import get_logger
from concierge.tenants.service import TenantService

log = get_logger(__name__)

RETENTION_KIND = "retention"
RECONCILE_KIND = "metering_reconcile"
DIGEST_KIND = "weekly_digest"
RETENTION_HOUR = 3
RECONCILE_HOUR = 2
DIGEST_HOUR = 8
DIGEST_WEEKDAY = 0  # Monday

TenantHandler = Callable[[Tenant], Awaitable[None]]


def next_daily(now: datetime, tz: str, hour: int) -> datetime:
    local = to_local(now, tz)
    day = local.date() if local.hour < hour else local.date() + timedelta(days=1)
    return local_to_utc(day, time(hour, 0), tz)


def next_weekly(now: datetime, tz: str, weekday: int, hour: int) -> datetime:
    local = to_local(now, tz)
    days_ahead = (weekday - local.weekday()) % 7
    candidate = local.date() + timedelta(days=days_ahead)
    if days_ahead == 0 and local.hour >= hour:
        candidate += timedelta(days=7)
    return local_to_utc(candidate, time(hour, 0), tz)


class MaintenanceScheduler:
    def __init__(self, jobs: JobService, clock: Clock, tenants: TenantService) -> None:
        self.jobs = jobs
        self.clock = clock
        self.tenants = tenants
        self.handlers: dict[str, TenantHandler] = {}

    def register(self, kind: str, handler: TenantHandler) -> None:
        self.handlers[kind] = handler

    async def schedule_all(self) -> int:
        count = 0
        for tenant in await self.tenants.list():
            for kind in (RETENTION_KIND, RECONCILE_KIND, DIGEST_KIND):
                await self.schedule(tenant, kind)
                count += 1
        return count

    async def schedule(self, tenant: Tenant, kind: str) -> datetime:
        now = self.clock.now()
        tz = tenant.timezone
        if kind == DIGEST_KIND:
            run_at = next_weekly(now, tz, DIGEST_WEEKDAY, DIGEST_HOUR)
            stamp = f"{run_at.isocalendar().year}-W{run_at.isocalendar().week:02d}"
        else:
            hour = RETENTION_HOUR if kind == RETENTION_KIND else RECONCILE_HOUR
            run_at = next_daily(now, tz, hour)
            stamp = to_local(run_at, tz).date().isoformat()
        await self.jobs.enqueue(
            kind,
            {"tenant_id": tenant.id},
            run_at=run_at,
            tenant_id=tenant.id,
            dedupe_key=f"maintenance:{kind}:{tenant.id}:{stamp}",
            max_attempts=3,
        )
        return run_at

    async def run(self, job: Job) -> None:
        tenant = await self.tenants.get_by_id(str(job.payload.get("tenant_id", "")))
        if tenant is None:
            return
        handler = self.handlers.get(job.kind)
        if handler is not None:
            await handler(tenant)
            log.info("maintenance.ran", kind=job.kind, tenant=tenant.id)
        await self.schedule(tenant, job.kind)
