"""Doctor records (F28): the outcome of the last live check per tenant.

Implements: F28, F21

``concierge doctor --live`` records one audit row per tenant (action ``doctor.live``)
with ``ok`` in its details; the activation gate reads the latest one here so a tenant
never goes live without a recorded, passing live check.
"""

from __future__ import annotations

from sqlalchemy import select

from concierge.context import AppContext
from concierge.db.models import AuditLog, Tenant

LIVE_ACTION = "doctor.live"


async def live_ok_for_tenant(ctx: AppContext, slug: str) -> bool | None:
    """True/False from the latest recorded live run; None when none was recorded."""
    async with ctx.db.scope() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is None:
            return None
        row = await session.scalar(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant.id, AuditLog.action == LIVE_ACTION)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    if row is None:
        return None
    return bool(row.details.get("ok"))
