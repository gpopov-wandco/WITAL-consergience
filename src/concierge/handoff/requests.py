"""Guest requests for staff (F11, F17): early check-in, invoice, ETA, callbacks.

Implements: F11, F17, F02

A request is recorded, never confirmed; staff close it from the inbox. Callback tasks
store the guest's number encrypted with a blind index.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from sqlalchemy import select

from concierge.clock import Clock
from concierge.db.models import CallbackTask, GuestRequest
from concierge.db.session import Database
from concierge.security.crypto import Keyring

REQUEST_KINDS: frozenset[str] = frozenset(
    {
        "early_checkin",
        "late_checkout",
        "invoice",
        "parking",
        "cot",
        "extra_bed",
        "group_inquiry",
        "allergy",
        "booking_change",
        "eta",
        "booking_inquiry",
        "upsell",
        "other",
    }
)


class RequestService:
    def __init__(self, db: Database, clock: Clock, keyring: Keyring) -> None:
        self.db = db
        self.clock = clock
        self.keyring = keyring

    async def create(
        self,
        tenant_id: str,
        *,
        conversation_id: str | None,
        kind: str,
        details: dict[str, Any],
        reservation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> GuestRequest:
        if kind not in REQUEST_KINDS:
            raise ValueError(f"unknown request kind {kind!r}")
        async with self.db.tenant_scope(tenant_id) as session:
            if idempotency_key:
                existing = await session.scalar(
                    select(GuestRequest).where(
                        GuestRequest.tenant_id == tenant_id,
                        GuestRequest.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return existing
            row = GuestRequest(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                reservation_id=reservation_id,
                kind=kind,
                details={**details, "recorded_at": self.clock.now().isoformat()},
                idempotency_key=idempotency_key,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def list_open(self, tenant_id: str, *, kind: str | None = None) -> list[GuestRequest]:
        async with self.db.tenant_scope(tenant_id) as session:
            query = select(GuestRequest).where(
                GuestRequest.tenant_id == tenant_id, GuestRequest.status == "open"
            )
            if kind:
                query = query.where(GuestRequest.kind == kind)
            return list((await session.execute(query.order_by(GuestRequest.created_at))).scalars())

    async def close(self, tenant_id: str, request_id: str, *, status: str = "done") -> GuestRequest:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(GuestRequest, request_id)
            if row is None or row.tenant_id != tenant_id:
                raise ValueError("unknown request")
            row.status = status
            await session.commit()
            await session.refresh(row)
            return row

    async def record_eta(
        self,
        tenant_id: str,
        *,
        conversation_id: str,
        reservation_id: str | None,
        eta: time,
        stay_date: datetime | None = None,
    ) -> GuestRequest:
        return await self.create(
            tenant_id,
            conversation_id=conversation_id,
            kind="eta",
            details={
                "eta": eta.strftime("%H:%M"),
                "stay_date": stay_date.isoformat() if stay_date else "",
            },
            reservation_id=reservation_id,
            idempotency_key=f"eta:{conversation_id}:{eta.strftime('%H:%M')}",
        )

    async def create_callback(
        self,
        tenant_id: str,
        *,
        conversation_id: str | None,
        phone: str | None,
        reason: str,
        lang: str,
        voice_call_id: str | None = None,
    ) -> CallbackTask:
        async with self.db.tenant_scope(tenant_id) as session:
            row = CallbackTask(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                voice_call_id=voice_call_id,
                reason=reason[:300],
                lang=lang,
            )
            if phone:
                row.phone_enc = self.keyring.encrypt_str(phone, tenant_id, "phone")
                row.phone_bidx = self.keyring.blind_index(phone, tenant_id, "phone")
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def open_callbacks(self, tenant_id: str) -> list[CallbackTask]:
        async with self.db.tenant_scope(tenant_id) as session:
            return list(
                (
                    await session.execute(
                        select(CallbackTask)
                        .where(CallbackTask.tenant_id == tenant_id, CallbackTask.status == "open")
                        .order_by(CallbackTask.created_at)
                    )
                ).scalars()
            )
