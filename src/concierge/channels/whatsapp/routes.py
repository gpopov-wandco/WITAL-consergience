"""WhatsApp webhook endpoints (6.6 inbound flow).

Implements: 6.6, F31, C15

``GET /wa/webhook`` answers the verification challenge as plain text when
``hub.verify_token`` matches a tenant's verify token. ``POST /wa/webhook`` reads
the raw bytes, verifies ``X-Hub-Signature-256`` against every known app secret with a
constant-time compare before parsing, resolves the tenant by
``metadata.phone_number_id`` and checks that the matching secret belongs to that
tenant, stores one ``webhook_event`` per message or status (unique per tenant and
key), queues the processing job and returns 200 at once.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from concierge.channels.whatsapp.webhook import parse_webhook
from concierge.clock import Clock
from concierge.db.models import Job, Tenant, WebhookEvent
from concierge.db.session import Database
from concierge.ids import new_id
from concierge.jobs.service import JobService
from concierge.log import get_logger
from concierge.security.signatures import meta_signature_ok
from concierge.tenants.secrets import SecretStore

log = get_logger(__name__)
router = APIRouter()
MAX_BODY = 3 * 1024 * 1024


@dataclass
class TenantCredentials:
    tenant_id: str
    phone_number_id: str | None
    app_secret: str
    verify_token: str


@dataclass
class SecretCache:
    """Per-tenant WhatsApp secrets, refreshed every minute or on invalidation."""

    db: Database
    secrets: SecretStore
    clock: Clock
    ttl: timedelta = timedelta(seconds=60)
    _rows: list[TenantCredentials] = field(default_factory=list)
    _loaded_at: datetime | None = None

    def invalidate(self) -> None:
        self._loaded_at = None

    async def rows(self) -> list[TenantCredentials]:
        now = self.clock.now()
        if self._loaded_at is not None and now - self._loaded_at < self.ttl:
            return self._rows
        rows: list[TenantCredentials] = []
        async with self.db.scope() as session:
            tenants = list((await session.execute(select(Tenant))).scalars())
        for tenant in tenants:
            values = await self.secrets.get_many(tenant.id, ["wa_app_secret", "wa_verify_token"])
            if values.get("wa_app_secret") or values.get("wa_verify_token"):
                rows.append(
                    TenantCredentials(
                        tenant.id,
                        tenant.phone_number_id,
                        values.get("wa_app_secret", ""),
                        values.get("wa_verify_token", ""),
                    )
                )
        self._rows = rows
        self._loaded_at = now
        return rows


class WebhookIngress:
    def __init__(self, db: Database, clock: Clock, jobs: JobService, cache: SecretCache) -> None:
        self.db = db
        self.clock = clock
        self.jobs = jobs
        self.cache = cache
        self._write_lock = asyncio.Lock()

    async def verify(self, mode: str | None, token: str | None, challenge: str | None) -> Response:
        if mode != "subscribe" or not token or challenge is None:
            return PlainTextResponse("forbidden", status_code=403)
        for row in await self.cache.rows():
            if row.verify_token and _safe_equal(row.verify_token, token):
                return PlainTextResponse(challenge, status_code=200)
        return PlainTextResponse("forbidden", status_code=403)

    async def ingest(self, raw: bytes, signature: str | None) -> Response:
        if len(raw) > MAX_BODY:
            return PlainTextResponse("too large", status_code=413)
        rows = await self.cache.rows()
        secrets = [r.app_secret for r in rows]
        index = meta_signature_ok(raw, signature, secrets)
        if index is None:
            log.warning("whatsapp.webhook_signature_rejected")
            return PlainTextResponse("invalid signature", status_code=403)
        matched = rows[index]
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return PlainTextResponse("bad json", status_code=400)
        changes = parse_webhook(payload)
        stored = 0
        for change in changes:
            tenant_id = _tenant_for(rows, change.phone_number_id)
            if tenant_id is None:
                log.warning("whatsapp.webhook_unknown_number")
                continue
            if tenant_id != matched.tenant_id:
                log.warning("whatsapp.webhook_secret_mismatch")
                continue
            for message in change.messages:
                stored += await self._store(
                    tenant_id,
                    f"msg:{message.wamid}",
                    _single(payload, change, message=message.wamid),
                )
            for status in change.statuses:
                stored += await self._store(
                    tenant_id,
                    f"status:{status.wamid}:{status.status}",
                    _single(payload, change, status=status.wamid),
                )
            for update in change.template_updates:
                key = f"tpl:{update.name}:{update.language}:{update.event}:{update.category or ''}"
                stored += await self._store(tenant_id, key, _single(payload, change))
        return PlainTextResponse("ok", status_code=200, headers={"x-events": str(stored)})

    async def _store(self, tenant_id: str, key: str, payload: dict[str, Any]) -> int:
        """The event row and its job commit together in one transaction.

        Writers are serialised in the process: SQLite lets one writer through at a time
        and its busy handler sleeps between retries, so an in-process lock keeps a burst
        of webhooks moving at the speed of the inserts themselves.
        """
        event_id = new_id("evt")
        now = self.clock.now()
        event_row = {
            "id": event_id,
            "tenant_id": tenant_id,
            "source": "meta",
            "event_key": key[:160],
            "received_at": now,
            "status": "received",
            "payload": payload,
        }
        job_row = {
            "id": new_id("job"),
            "tenant_id": tenant_id,
            "kind": "wa_inbound",
            "run_at": now,
            "status": "pending",
            "attempts": 0,
            "max_attempts": 5,
            "dedupe_key": f"wa_inbound:{event_id}",
            "payload": {"event_id": event_id, "tenant_id": tenant_id},
            "created_at": now,
            "updated_at": now,
        }
        # Core inserts: no unit-of-work bookkeeping on the hot path
        async with self._write_lock, self.db.scope() as session:
            try:
                await session.execute(insert(WebhookEvent).values(**event_row))
                await session.execute(insert(Job).values(**job_row))
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return 0  # a Meta retry of an event already stored
        return 1


def _tenant_for(rows: list[TenantCredentials], phone_number_id: str) -> str | None:
    if not phone_number_id:
        return None
    for row in rows:
        if row.phone_number_id == phone_number_id:
            return row.tenant_id
    return None


def _safe_equal(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _single(
    payload: dict[str, Any], change: Any, *, message: str | None = None, status: str | None = None
) -> dict[str, Any]:
    """Reduce a webhook payload to one message or status so events process independently."""
    for entry in payload.get("entry") or []:
        for item in entry.get("changes") or []:
            value = item.get("value") or {}
            metadata = value.get("metadata") or {}
            if metadata.get("phone_number_id") != change.phone_number_id:
                continue
            single_value: dict[str, Any] = {"messaging_product": "whatsapp", "metadata": metadata}
            if message is not None:
                single_value["contacts"] = value.get("contacts") or []
                single_value["messages"] = [
                    m for m in value.get("messages") or [] if m.get("id") == message
                ]
            elif status is not None:
                single_value["statuses"] = [
                    s for s in value.get("statuses") or [] if s.get("id") == status
                ]
            else:
                single_value = dict(value)
            return {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "id": entry.get("id", ""),
                        "changes": [
                            {"field": item.get("field", "messages"), "value": single_value}
                        ],
                    }
                ],
            }
    return payload


def build_router(ingress: WebhookIngress) -> APIRouter:
    api = APIRouter()

    @api.get("/wa/webhook")
    async def verify(request: Request) -> Response:
        params = request.query_params
        return await ingress.verify(
            params.get("hub.mode"), params.get("hub.verify_token"), params.get("hub.challenge")
        )

    @api.post("/wa/webhook")
    async def receive(request: Request) -> Response:
        raw = await request.body()
        return await ingress.ingest(raw, request.headers.get("x-hub-signature-256"))

    return api
