"""Declarative base and mixins.

Implements: F31, C15, C16

- :class:`TenantScoped` marks every business table; the session layer adds the
  tenant criterion to every ORM statement and refuses flushes for other tenants.
- :class:`AppendOnly` marks logs that are never updated or deleted through the ORM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from concierge.db.types import UTCDateTime
from concierge.ids import new_id


def utcnow() -> datetime:
    """Default column value; business logic uses the injected clock instead."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: UTCDateTime,
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


def id_column(prefix: str) -> Mapped[str]:
    return mapped_column(String(40), primary_key=True, default=lambda: new_id(prefix))


class TenantScoped:
    """Rows that belong to exactly one tenant."""

    @declared_attr
    @classmethod
    def tenant_id(cls) -> Mapped[str]:
        return mapped_column(
            String(40), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False, index=True
        )


class AppendOnly:
    """Rows that are inserted, never updated or deleted, except by the retention job."""


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
