"""Append-only audit trail for operator and owner actions.

Implements: F20, F21, F31, C16
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from concierge.db.models import AuditLog
from concierge.log import redact


def record(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target: str = "",
    tenant_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an audit row to the session (committed with the surrounding unit of work)."""
    row = AuditLog(
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        target=target,
        details=redact(details or {}),
    )
    session.add(row)
    return row
