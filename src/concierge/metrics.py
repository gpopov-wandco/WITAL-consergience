"""Bearer-protected Prometheus metrics (F32).

Implements: F32, F31

Gauges are computed at scrape time from the job table, open handoffs, the worker
heartbeat, the model breaker and the usage rows of the last 24 hours. No guest data
and no free text leave this endpoint; tenant labels carry the slug only.
"""

from __future__ import annotations

import hmac
from datetime import timedelta
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from sqlalchemy import func, select

from concierge import __version__
from concierge.db.models import Handoff, Job, Tenant, UsageEvent

if TYPE_CHECKING:
    from concierge.services import Services


def authorised(header: str | None, token: str) -> bool:
    if not token or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer ") :].strip(), token)


async def render(services: Services) -> bytes:
    registry = CollectorRegistry()
    Gauge("gc_build_info", "Build information", ["version"], registry=registry).labels(
        version=__version__
    ).set(1)
    jobs = Gauge("gc_jobs", "Jobs by status", ["status"], registry=registry)
    handoffs = Gauge("gc_handoffs_open", "Open handoffs", ["tenant", "priority"], registry=registry)
    usage = Gauge(
        "gc_usage_cost_micro_eur_24h",
        "Usage cost of the last 24 hours in micro EUR",
        ["tenant", "kind"],
        registry=registry,
    )
    heartbeat = Gauge(
        "gc_worker_heartbeat_age_seconds", "Seconds since the worker heartbeat", registry=registry
    )
    breaker = Gauge("gc_llm_breaker_open", "1 when the model circuit is open", registry=registry)
    calls = Gauge("gc_voice_calls_active", "Live voice calls", registry=registry)
    now = services.ctx.clock.now()
    async with services.ctx.db.scope() as session:
        slugs = dict((await session.execute(select(Tenant.id, Tenant.slug))).all())
        for status, count in (
            await session.execute(select(Job.status, func.count()).group_by(Job.status))
        ).all():
            jobs.labels(status=status).set(count)
        for tenant_id, priority, count in (
            await session.execute(
                select(Handoff.tenant_id, Handoff.priority, func.count())
                .where(Handoff.status.in_(["open", "acknowledged", "escalated"]))
                .group_by(Handoff.tenant_id, Handoff.priority)
            )
        ).all():
            handoffs.labels(tenant=slugs.get(tenant_id, "?"), priority=priority).set(count)
        for tenant_id, kind, cost in (
            await session.execute(
                select(UsageEvent.tenant_id, UsageEvent.kind, func.sum(UsageEvent.cost_micro_eur))
                .where(UsageEvent.occurred_at >= now - timedelta(hours=24))
                .group_by(UsageEvent.tenant_id, UsageEvent.kind)
            )
        ).all():
            usage.labels(tenant=slugs.get(tenant_id, "?"), kind=kind).set(int(cost or 0))
    age = services.worker.heartbeat_age_s()
    heartbeat.set(age if age is not None else -1)
    breaker.set(1 if services.brain.breaker.is_open else 0)
    calls.set(len(services.voice.calls))
    return generate_latest(registry)
