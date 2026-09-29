"""Hosted pre-check-in form (F15): ``/r/{token}`` in the booking language.

Implements: F15, C8, C10, F31

The signed link identifies tenant and reservation; the form collects only the closed
field set, saves it encrypted, and on the arrival day starts the electronic
confirmation through the payment-proof port. The fake provider completes through its
callback (demo, tests); Stripe completes through the signed webhook.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from concierge.admin.templating import templates
from concierge.clock import Clock, format_date
from concierge.registration.proof import FakeProof
from concierge.registration.service import FIELDS, RegistrationError, RegistrationService
from concierge.registration.stripe_proof import StripeProof
from concierge.security.signatures import stripe_signature_ok
from concierge.tenants.service import TenantService


def build_router(
    registration: RegistrationService,
    tenants: TenantService,
    clock: Clock,
    *,
    stripe_webhook_secret: str = "",
) -> APIRouter:
    api = APIRouter(prefix="/r")

    async def _page(request: Request, token: str, *, error: str = "", notice: str = "") -> Response:
        try:
            tenant, reservation = await registration.resolve(token)
        except RegistrationError as exc:
            return templates.TemplateResponse(
                request,
                "registration/invalid.html",
                {"lang": "en", "reason": exc.code},
                status_code=404 if exc.code == "invalid_link" else 410,
            )
        profile = tenants.profile_of(tenant)
        lang = (
            reservation.lang if reservation.lang in profile.languages else profile.default_language
        )
        texts = registration.texts.lang(lang)
        row = await registration.get(tenant.id, reservation.id)
        data: dict[str, Any] = registration.decrypt(row) if row is not None else {}
        if not data:
            data = {
                "arrival": reservation.arrival.isoformat(),
                "departure": reservation.departure.isoformat(),
            }
        if isinstance(data.get("nationalities"), list):
            data["nationalities"] = ", ".join(data["nationalities"])
        return templates.TemplateResponse(
            request,
            "registration/form.html",
            {
                "lang": lang,
                "t": texts,
                "fields": FIELDS,
                "data": data,
                "hotel": profile.name,
                "token": token,
                "saved": row is not None,
                "confirmed": row is not None and row.confirmed_at is not None,
                "arrival_day": row is not None and registration.arrival_day_ok(profile, row),
                "arrival_text": format_date(reservation.arrival, lang),
                "enabled": profile.registration.enabled,
                "error": error,
                "notice": notice,
                "status": registration.texts.status,
                "privacy_url": profile.contacts.privacy_url,
            },
        )

    @api.get("/{token}", response_class=HTMLResponse)
    async def form(request: Request, token: str) -> Response:
        return await _page(request, token)

    @api.post("/{token}", response_class=HTMLResponse)
    async def save(request: Request, token: str) -> Response:
        try:
            tenant, reservation = await registration.resolve(token)
        except RegistrationError:
            return await _page(request, token)
        profile = tenants.profile_of(tenant)
        form_data = await request.form()
        raw = {k: str(v) for k, v in form_data.items() if k in FIELDS and str(v).strip() != ""}
        raw.setdefault("companions_count", "0")
        try:
            data = registration.parse(raw)
            await registration.save(tenant, profile, reservation, data, actor="guest")
        except RegistrationError as exc:
            return await _page(request, token, error=str(exc))
        return RedirectResponse(f"/r/{token}", status_code=303)

    @api.post("/{token}/confirm")
    async def confirm(request: Request, token: str) -> Response:
        try:
            tenant, reservation = await registration.resolve(token)
        except RegistrationError:
            return await _page(request, token)
        profile = tenants.profile_of(tenant)
        row = await registration.get(tenant.id, reservation.id)
        if row is None:
            return RedirectResponse(f"/r/{token}", status_code=303)
        base = registration.public_base_url
        try:
            session = await registration.start_confirmation(
                tenant,
                profile,
                row,
                success_url=f"{base}/r/{token}/done",
                cancel_url=f"{base}/r/{token}",
            )
        except RegistrationError as exc:
            return await _page(request, token, error=str(exc))
        return RedirectResponse(session.redirect_url, status_code=303)

    @api.get("/{token}/done", response_class=HTMLResponse)
    async def done(request: Request, token: str) -> Response:
        return await _page(request, token, notice="done")

    @api.get("/proof/fake/{proof_id}")
    async def fake_callback(proof_id: str) -> Response:
        proof = registration.proof
        if not isinstance(proof, FakeProof) or proof_id not in proof.sessions:
            return JSONResponse({"error": "not found"}, status_code=404)
        row = await registration.complete(proof.completion(proof_id))
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"confirmed": True, "meldeschein_id": row.id})

    @api.post("/psp/webhook")
    async def stripe_webhook(request: Request) -> Response:
        raw = await request.body()
        header = request.headers.get("stripe-signature")
        if not stripe_webhook_secret or not stripe_signature_ok(
            raw, header, stripe_webhook_secret.encode("utf-8"), int(clock.now().timestamp())
        ):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        completion = StripeProof.parse_event(raw)
        if completion is None:
            return JSONResponse({"received": True, "handled": False})
        row = await registration.complete(completion)
        return JSONResponse({"received": True, "handled": row is not None})

    return api
