"""Operator console (F20): cross-tenant health, kill switch, prices, activation.

Implements: F20, F21, F24, F32, C16

Role ``operator`` only. A tenant switch and every kill-switch change are audited.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from concierge.admin.auth import AuthError, AuthService
from concierge.admin.deps import AdminGuard, Forbidden
from concierge.admin.templating import templates
from concierge.db.models import Job, OperatorAlert, PriceTable, Tenant, WebhookEvent
from concierge.services import Services
from concierge.tenants.service import TenantError

SUBPROCESSORS: list[dict[str, str]] = [
    {
        "name": "Meta Platforms Ireland",
        "purpose": "WhatsApp Business Platform",
        "region": "EU/US",
        "transfer": "SCC, VERIFY",
    },
    {
        "name": "Anthropic",
        "purpose": "language model",
        "region": "US",
        "transfer": "SCC, no training on data; VERIFY DPA",
    },
    {"name": "Hetzner Online", "purpose": "hosting", "region": "EU (DE)", "transfer": "none"},
    {"name": "Resend", "purpose": "e-mail delivery", "region": "VERIFY", "transfer": "VERIFY"},
    {
        "name": "Retell AI",
        "purpose": "voice (optional)",
        "region": "US",
        "transfer": "SCC, VERIFY; EU-only routing per tenant flag",
    },
    {
        "name": "Stripe",
        "purpose": "payment proof for registration (optional)",
        "region": "EU/US",
        "transfer": "SCC, VERIFY account owner",
    },
]


def build_router(services: Services, auth: AuthService, guard: AdminGuard) -> APIRouter:
    api = APIRouter(prefix="/operator")
    roles = ("operator",)

    async def _ctx(request: Request, *, mutation: bool = False) -> Any:
        return await guard.context(request, tenant_required=False, roles=roles, mutation=mutation)

    @api.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        ctx = await _ctx(request)
        if isinstance(ctx, Response):
            return ctx
        tenants = await services.tenants.list()
        rows = [await _health(services, tenant) for tenant in tenants]
        async with services.ctx.db.scope() as session:
            alerts = list(
                (
                    await session.execute(
                        select(OperatorAlert)
                        .where(OperatorAlert.resolved_at.is_(None))
                        .order_by(OperatorAlert.first_seen_at.desc())
                        .limit(50)
                    )
                ).scalars()
            )
        return templates.TemplateResponse(
            request,
            "operator/index.html",
            ctx.base(
                rows=rows,
                alerts=alerts,
                subprocessors=SUBPROCESSORS,
                email_configured=services.email_outbox.configured,
            ),
        )

    @api.post("/switch/{slug}")
    async def switch(request: Request, slug: str) -> Response:
        ctx = await _ctx(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        tenant = await services.tenants.get(slug)
        try:
            await auth.switch_tenant(ctx.principal, tenant.id if tenant else None)
        except AuthError as exc:
            raise Forbidden(exc.code) from exc
        return RedirectResponse("/admin/" if tenant else "/operator/", status_code=303)

    @api.post("/tenants/{slug}/kill-switch")
    async def kill_switch(request: Request, slug: str) -> Response:
        ctx = await _ctx(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        on = str(form.get("on", "1")) == "1"
        try:
            await services.tenants.set_kill_switch(slug, on, actor=ctx.principal.user.id)
        except TenantError:
            return RedirectResponse("/operator/?error=unknown_tenant", status_code=303)
        return RedirectResponse("/operator/", status_code=303)

    @api.get("/tenants/{slug}/activation", response_class=HTMLResponse)
    async def activation(request: Request, slug: str) -> Response:
        ctx = await _ctx(request)
        if isinstance(ctx, Response):
            return ctx
        try:
            report = await services.tenants.activation_report(slug, doctor_live_ok=None)
        except TenantError:
            return RedirectResponse("/operator/?error=unknown_tenant", status_code=303)
        return templates.TemplateResponse(
            request, "operator/activation.html", ctx.base(slug=slug, report=report)
        )

    @api.get("/prices", response_class=HTMLResponse)
    async def prices(request: Request, error: str = "") -> Response:
        ctx = await _ctx(request)
        if isinstance(ctx, Response):
            return ctx
        async with services.ctx.db.scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(PriceTable).order_by(
                            PriceTable.region, PriceTable.category, PriceTable.effective_from.desc()
                        )
                    )
                ).scalars()
            )
        return templates.TemplateResponse(
            request, "operator/prices.html", ctx.base(rows=rows, error=error)
        )

    @api.post("/prices")
    async def prices_add(request: Request) -> Response:
        ctx = await _ctx(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        try:
            row = PriceTable(
                tenant_id=None,
                region=str(form.get("region", "DE")).upper()[:8],
                category=str(form.get("category", "")).lower()[:16],
                effective_from=date.fromisoformat(str(form.get("effective_from", ""))),
                price_micro_eur=round(
                    float(str(form.get("price_eur", "0")).replace(",", ".")) * 1_000_000
                ),
                free_allowance_per_month=int(str(form.get("free_allowance", "0") or 0)),
                source_url=str(form.get("source_url", ""))[:300],
                updated_by=ctx.principal.user.id,
            )
            if row.category not in {"utility", "marketing", "authentication", "service"}:
                raise ValueError("category")
        except ValueError:
            return RedirectResponse("/operator/prices?error=invalid", status_code=303)
        async with services.ctx.db.scope() as session:
            from concierge import audit

            session.add(row)
            audit.record(
                session,
                actor=ctx.principal.user.id,
                action="prices.add",
                target=f"{row.region}/{row.category}",
                details={
                    "price_micro_eur": row.price_micro_eur,
                    "effective_from": row.effective_from.isoformat(),
                },
            )
            await session.commit()
        return RedirectResponse("/operator/prices", status_code=303)

    return api


async def _health(services: Services, tenant: Tenant) -> dict[str, Any]:
    profile = services.tenants.profile_of(tenant)
    async with services.ctx.db.scope() as session:
        last_webhook = await session.scalar(
            select(func.max(WebhookEvent.received_at)).where(WebhookEvent.tenant_id == tenant.id)
        )
        dead_jobs = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.tenant_id == tenant.id, Job.status == "dead")
        )
    template_rows = await services.templates.rows(tenant.id)
    approved = sum(1 for r in template_rows if r.status == "APPROVED")
    budget_spent = services.brain.budget.spent(tenant.id, profile.timezone)
    return {
        "tenant": tenant,
        "status": tenant.status,
        "paused": tenant.paused,
        "kill_switch": tenant.kill_switch,
        "last_webhook": last_webhook,
        "token_invalid": tenant.id in services.outbox.token_invalid_tenants(),
        "templates": f"{approved}/{len(template_rows)}",
        "dead_jobs": dead_jobs or 0,
        "budget": f"{budget_spent:.2f} / {profile.limits.daily_llm_budget_usd:.2f} USD",
        "breaker_open": services.brain.breaker.is_open,
        "chain": ", ".join(profile.availability.chain),
        "voice": profile.voice.provider,
    }
