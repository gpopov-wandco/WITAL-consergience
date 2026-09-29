"""Signed reservation push API (F12): ``POST /api/v1/tenants/{public_key}/reservations``.

Implements: F12, C3, C15, F31

HMAC-SHA256 over the raw body with the tenant's ``reservation_api_secret`` in the
``X-Signature`` header (``t=<unix seconds>,v1=<hex>``), a five-minute window, idempotent
by ``res_id``. Failures never say which part of the signature was wrong.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from concierge.clock import Clock
from concierge.reservations.service import ReservationInput, ReservationService
from concierge.security.signatures import timestamped_signature_ok
from concierge.tenants.secrets import SecretStore
from concierge.tenants.service import TenantService

MAX_BODY = 64 * 1024
SIGNATURE_HEADER = "x-signature"
WINDOW_S = 300


def build_router(
    tenants: TenantService,
    secrets: SecretStore,
    reservations: ReservationService,
    clock: Clock,
) -> APIRouter:
    api = APIRouter(prefix="/api/v1")

    @api.post("/tenants/{public_key}/reservations")
    async def push_reservation(public_key: str, request: Request) -> Response:
        tenant = await tenants.by_public_key(public_key)
        if tenant is None:
            return JSONResponse({"error": "unknown tenant"}, status_code=404)
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse({"error": "body too large"}, status_code=413)
        secret = await secrets.get(tenant.id, "reservation_api_secret")
        if not secret:
            return JSONResponse({"error": "reservation api not configured"}, status_code=503)
        header = request.headers.get(SIGNATURE_HEADER)
        now_s = int(clock.now().timestamp())
        if not timestamped_signature_ok(raw, header, secret.encode("utf-8"), now_s, WINDOW_S):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            data = ReservationInput.model_validate_json(raw)
        except ValidationError as exc:
            return JSONResponse(
                {"error": "invalid reservation", "fields": _field_errors(exc)}, status_code=422
            )
        profile = tenants.profile_of(tenant)
        result = await reservations.upsert(
            tenant.id, data, actor=f"api:{public_key}", phone_region=profile.contacts.phone_region
        )
        body: dict[str, Any] = {
            "id": result.reservation.id,
            "res_id": result.reservation.res_id,
            "status": result.outcome,
            "changed": result.changed,
            "cancelled_jobs": result.cancelled_jobs,
            "whatsapp_optin": result.reservation.whatsapp_optin,
        }
        return JSONResponse(body, status_code=201 if result.created else 200)

    return api


def _field_errors(exc: ValidationError) -> list[str]:
    out: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "body"
        out.append(f"{location}: {error.get('msg', 'invalid')}")
    return out[:20]
