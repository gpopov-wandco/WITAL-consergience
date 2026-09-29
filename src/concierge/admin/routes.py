"""Hotel admin pages (F19) and the staff inbox (F17).

Implements: F19, F17, F14, F15, F16, F21, C10, C16, C17

Jinja2 pages with htmx for small mutations; no inline scripts (CSP ``script-src
'self'``); every mutation carries the CSRF token; German default, English available.
Wording never promises reply times the tenant's SLA configuration does not state.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select

from concierge.admin.auth import AuthError, AuthService
from concierge.admin.deps import AdminContext, AdminGuard, Forbidden
from concierge.admin.i18n import label, labels
from concierge.admin.templating import templates
from concierge.agent.responder import ResponderContext
from concierge.availability.manual import CsvReport
from concierge.channels.whatsapp.formatting import text_payload
from concierge.clock import format_local, local_to_utc, parse_range, to_local
from concierge.db.models import (
    Conversation,
    DeletionLog,
    Guest,
    Handoff,
    Job,
    Message,
    MessageText,
    Reservation,
    Suppression,
)
from concierge.gdpr.service import GdprError, Subject
from concierge.kb.questionnaire import (
    GROUPS,
    ITEMS,
    form_value,
    items_of,
    missing_required,
    validate_answers,
)
from concierge.legal.service import LegalError
from concierge.reports.qa import kb_key_for
from concierge.reports.redact import redact
from concierge.security.sessions import SESSION_COOKIE, set_session_cookie
from concierge.services import Services
from concierge.templates.service import TemplateRefused
from concierge.tenants.profile import TenantProfile

WINDOW_WARN = timedelta(hours=2)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",")[0].strip() or (request.client.host if request.client else ""))[:64]


def build_router(services: Services, auth: AuthService, guard: AdminGuard) -> APIRouter:
    api = APIRouter(prefix="/admin")
    secure = services.ctx.settings.public_base_url().startswith("https://")
    clock = services.ctx.clock

    def page(request: Request, name: str, ctx: AdminContext, **extra: Any) -> Response:
        return templates.TemplateResponse(request, f"admin/{name}.html", ctx.base(**extra))

    # ------------------------------------------------------------------ auth
    @api.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, error: str = "", next: str = "/admin/") -> Response:
        lang = (
            "en" if request.headers.get("accept-language", "de").lower().startswith("en") else "de"
        )
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {
                "lang": lang,
                "t": labels(lang),
                "error": label(error, lang) if error else "",
                "next": next,
            },
        )

    @api.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        email = str(form.get("email", ""))
        password = str(form.get("password", ""))
        target = str(form.get("next") or "/admin/")
        if not _SAFE_NEXT.match(target):
            target = "/admin/"
        try:
            token, principal = await auth.login(
                email,
                password,
                ip_ref=hashlib.sha256(_client_ip(request).encode()).hexdigest()[:12]
                if _client_ip(request)
                else "",
                user_agent=request.headers.get("user-agent", ""),
            )
        except AuthError as exc:
            return RedirectResponse(f"/admin/login?error={exc.code}", status_code=303)
        if principal.is_operator and principal.tenant is None:
            target = "/operator/"
        response = RedirectResponse(target, status_code=303)
        set_session_cookie(response, token, secure=secure)
        return response

    @api.post("/logout")
    async def logout(request: Request) -> Response:
        await auth.logout(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @api.get("/bootstrap/{token}", response_class=HTMLResponse)
    async def bootstrap_form(request: Request, token: str) -> Response:
        try:
            user = await auth.bootstrap_user(token)
        except AuthError:
            return templates.TemplateResponse(
                request, "admin/invalid.html", {"lang": "de", "t": labels("de")}, status_code=404
            )
        return templates.TemplateResponse(
            request,
            "admin/set_password.html",
            {
                "lang": "de",
                "t": labels("de"),
                "action": f"/admin/bootstrap/{token}",
                "email": user.email,
                "error": "",
            },
        )

    @api.post("/bootstrap/{token}")
    async def bootstrap(request: Request, token: str) -> Response:
        form = await request.form()
        try:
            await auth.bootstrap(token, str(form.get("password", "")))
        except AuthError as exc:
            status = 404 if exc.code == "invalid_link" else 400
            return templates.TemplateResponse(
                request,
                "admin/set_password.html",
                {
                    "lang": "de",
                    "t": labels("de"),
                    "action": f"/admin/bootstrap/{token}",
                    "email": "",
                    "error": label(exc.code, "de"),
                },
                status_code=status,
            )
        return RedirectResponse("/admin/login", status_code=303)

    @api.get("/invite/{token}", response_class=HTMLResponse)
    async def invite_form(request: Request, token: str) -> Response:
        try:
            user = await auth.invited_user(token)
        except AuthError:
            return templates.TemplateResponse(
                request, "admin/invalid.html", {"lang": "de", "t": labels("de")}, status_code=404
            )
        lang = user.locale if user.locale in {"de", "en"} else "de"
        return templates.TemplateResponse(
            request,
            "admin/set_password.html",
            {
                "lang": lang,
                "t": labels(lang),
                "action": f"/admin/invite/{token}",
                "email": user.email,
                "error": "",
            },
        )

    @api.post("/invite/{token}")
    async def invite_accept(request: Request, token: str) -> Response:
        form = await request.form()
        try:
            await auth.accept_invite(token, str(form.get("password", "")))
        except AuthError as exc:
            status = 404 if exc.code == "invalid_link" else 400
            return templates.TemplateResponse(
                request,
                "admin/set_password.html",
                {
                    "lang": "de",
                    "t": labels("de"),
                    "action": f"/admin/invite/{token}",
                    "email": "",
                    "error": label(exc.code, "de"),
                },
                status_code=status,
            )
        return RedirectResponse("/admin/login", status_code=303)

    # ------------------------------------------------------------- dashboard
    @api.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        tenant_id = ctx.tenant_id
        handoffs = await services.handoffs.inbox(tenant_id)
        by_priority = {
            p: sum(1 for h in handoffs if h.priority == p) for p in ("P0", "P1", "P2", "P3")
        }
        today_start = local_to_utc(
            to_local(clock.now(), ctx.tenant_profile.timezone).date(),
            time(0, 0),
            ctx.tenant_profile.timezone,
        )
        async with services.ctx.db.tenant_scope(tenant_id) as session:
            conversations_today = await session.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id, Conversation.last_inbound_at >= today_start
                )
            )
            pending_kb = len(await services.kb.entries(tenant_id, status="pending_approval"))
            dead_jobs = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.tenant_id == tenant_id, Job.status == "dead")
            )
        template_rows = await services.templates.rows(tenant_id)
        approved = sum(1 for r in template_rows if r.status == "APPROVED")
        overdue = await services.kb.overdue(tenant_id)
        return page(
            request,
            "dashboard",
            ctx,
            by_priority=by_priority,
            open_total=len(handoffs),
            conversations_today=conversations_today or 0,
            pending_kb=pending_kb,
            dead_jobs=dead_jobs or 0,
            templates_approved=approved,
            templates_total=len(template_rows),
            overdue=overdue[:10],
            integrations=await _integrations(services, ctx),
            now_local=format_local(clock.now(), ctx.tenant_profile.timezone, ctx.lang),
        )

    # ----------------------------------------------------------------- inbox
    @api.get("/inbox", response_class=HTMLResponse)
    async def inbox(request: Request, status: str = "open", priority: str = "") -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        statuses = ("open", "acknowledged", "escalated") if status == "open" else (status,)
        rows = await services.handoffs.inbox(ctx.tenant_id, statuses=statuses)
        if priority:
            rows = [h for h in rows if h.priority == priority]
        items = [await _inbox_item(services, ctx, h) for h in rows]
        return page(request, "inbox", ctx, items=items, status=status, priority=priority)

    @api.get("/conversations/{conversation_id}", response_class=HTMLResponse)
    async def conversation_page(
        request: Request, conversation_id: str, notice: str = ""
    ) -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        data = await _conversation_view(services, ctx, conversation_id)
        if data is None:
            return templates.TemplateResponse(
                request, "admin/invalid.html", ctx.base(), status_code=404
            )
        last_reply = next(
            (
                m
                for m in reversed(data["transcript"])
                if m["direction"] == "out" and m["kind"] == "text"
            ),
            None,
        )
        data["fix_kb_key"] = kb_key_for(last_reply["intent"]) if last_reply else ""
        data["last_reply_id"] = last_reply["id"] if last_reply else ""
        return page(
            request, "conversation", ctx, notice=label(notice, ctx.lang) if notice else "", **data
        )

    @api.post("/conversations/{conversation_id}/acknowledge")
    async def acknowledge(request: Request, conversation_id: str) -> Response:
        ctx = await guard.context(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        handoff = await services.handoffs.open_for_conversation(ctx.tenant_id, conversation_id)
        if handoff is not None:
            await services.handoffs.acknowledge(
                ctx.tenant_id, handoff.id, user_id=ctx.principal.user.id
            )
        else:
            await services.conversations.update(
                ctx.tenant_id,
                conversation_id,
                status="staff_active",
                assigned_user_id=ctx.principal.user.id,
            )
        return RedirectResponse(f"/admin/conversations/{conversation_id}", status_code=303)

    @api.post("/conversations/{conversation_id}/resolve")
    async def resolve(request: Request, conversation_id: str) -> Response:
        ctx = await guard.context(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        return_to_bot = str(form.get("return_to_bot", "1")) == "1"
        handoff = await services.handoffs.open_for_conversation(ctx.tenant_id, conversation_id)
        if handoff is not None:
            await services.handoffs.resolve(
                ctx.tenant_id,
                handoff.id,
                user_id=ctx.principal.user.id,
                return_to_bot=return_to_bot,
            )
        else:
            await services.conversations.update(
                ctx.tenant_id,
                conversation_id,
                status="bot_handling" if return_to_bot else "resolved",
            )
        return RedirectResponse(f"/admin/conversations/{conversation_id}", status_code=303)

    @api.post("/conversations/{conversation_id}/note")
    async def add_note(request: Request, conversation_id: str) -> Response:
        ctx = await guard.context(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        text = str(form.get("text", "")).strip()
        if text:
            await services.handoffs.add_note(
                ctx.tenant_id, conversation_id, user_id=ctx.principal.user.id, text=text
            )
        return RedirectResponse(f"/admin/conversations/{conversation_id}", status_code=303)

    @api.post("/conversations/{conversation_id}/reply")
    async def reply(request: Request, conversation_id: str) -> Response:
        ctx = await guard.context(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        mode = str(form.get("mode", "text"))
        text = str(form.get("text", "")).strip()
        template_name = str(form.get("template", "")).strip()
        outcome = await _staff_reply(
            services, ctx, conversation_id, mode=mode, text=text, template_name=template_name
        )
        notice = "quiet_hours_block" if outcome == "quiet_hours" else outcome
        return RedirectResponse(
            f"/admin/conversations/{conversation_id}?notice={notice}", status_code=303
        )

    # -------------------------------------------------------------------- kb
    @api.get("/kb", response_class=HTMLResponse)
    async def kb_page(request: Request, status: str = "", lang: str = "", q: str = "") -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        entries = await services.kb.entries(ctx.tenant_id, status=status or None, lang=lang or None)
        if q.strip():
            needle = q.strip().lower()
            entries = [e for e in entries if needle in e.key.lower()]
        gaps = await services.kb.gap_report(ctx.tenant_id)
        return page(
            request, "kb", ctx, entries=entries[:400], status=status, kb_lang=lang, gaps=gaps[:20]
        )

    @api.post("/kb/{entry_id}/status")
    async def kb_status(request: Request, entry_id: str) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        status = str(form.get("status", ""))
        approver = (
            str(form.get("approver", "")).strip()
            or ctx.principal.user.name
            or ctx.principal.user.email
        )
        try:
            await services.kb.set_status(
                ctx.tenant_id, entry_id, status, actor=ctx.principal.user.id, approver=approver
            )
        except Exception as exc:  # KbError or unknown id: show it, never crash the page
            return RedirectResponse(f"/admin/kb?error={type(exc).__name__}", status_code=303)
        return RedirectResponse("/admin/kb", status_code=303)

    # ------------------------------------------------------------ questionnaire
    def questionnaire_view(
        request: Request,
        ctx: AdminContext,
        values: dict[str, str],
        *,
        missing: list[str],
        errors: list[str],
        notice: str = "",
    ) -> Response:
        groups = [
            (letter, titles[0] if ctx.lang == "de" else titles[1], items_of(letter))
            for letter, titles in GROUPS.items()
        ]
        return page(
            request,
            "questionnaire",
            ctx,
            groups=groups,
            values=values,
            missing=missing,
            errors=errors,
            notice=label(notice, ctx.lang) if notice else "",
        )

    @api.get("/questionnaire", response_class=HTMLResponse)
    async def questionnaire_page(request: Request, notice: str = "") -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        answers = await services.questionnaire.answers(ctx.tenant_id)
        values = {
            item.key: form_value(item, answers[item.key]) for item in ITEMS if item.key in answers
        }
        missing = [item.key for item in missing_required(answers)]
        return questionnaire_view(request, ctx, values, missing=missing, errors=[], notice=notice)

    @api.post("/questionnaire")
    async def questionnaire_save(request: Request) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        raw = {item.key: str(form.get(item.key, "")) for item in ITEMS}
        answers, errors = validate_answers(raw)
        if errors:
            values = {key: value for key, value in raw.items() if value.strip()}
            missing = [item.key for item in missing_required(answers)]
            return questionnaire_view(request, ctx, values, missing=missing, errors=errors)
        await services.questionnaire.save(
            ctx.tenant_id, answers, actor=ctx.principal.user.id, replace=True
        )
        return RedirectResponse("/admin/questionnaire?notice=saved", status_code=303)

    @api.post("/questionnaire/apply")
    async def questionnaire_apply(request: Request) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        await services.questionnaire.apply(
            ctx.tenant_id,
            services.kb,
            lang=ctx.tenant_profile.default_language,
            actor=ctx.principal.user.id,
        )
        return RedirectResponse("/admin/questionnaire?notice=drafts_created", status_code=303)

    # ----------------------------------------------------------- availability
    @api.get("/availability", response_class=HTMLResponse)
    async def availability_page(request: Request, month: str = "", report: str = "") -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        today = to_local(clock.now(), ctx.tenant_profile.timezone).date()
        try:
            year, mon = (int(p) for p in month.split("-")) if month else (today.year, today.month)
        except ValueError:
            year, mon = today.year, today.month
        grid = await services.inventory.grid(ctx.tenant_id, year, mon)
        health: list[dict[str, Any]] = []
        room_types: list[Any] = []
        for source in ctx.tenant_profile.availability.chain:
            adapter = await services.adapters(source, ctx.tenant_row, ctx.tenant_profile)
            if adapter is None:
                health.append(
                    {"source": source, "ok": False, "detail": "not configured", "warnings": []}
                )
                continue
            try:
                doc = await adapter.doctor()
                health.append(
                    {"source": source, "ok": doc.ok, "detail": doc.detail, "warnings": doc.warnings}
                )
                if source == "manual":
                    room_types = await adapter.list_room_types()
            except Exception as exc:
                health.append(
                    {"source": source, "ok": False, "detail": type(exc).__name__, "warnings": []}
                )
        csv_report = json.loads(report) if report else None
        return page(
            request,
            "availability",
            ctx,
            grid=grid,
            month=f"{year:04d}-{mon:02d}",
            health=health,
            csv_report=csv_report,
            room_types=room_types,
        )

    @api.post("/availability/bulk")
    async def availability_bulk(request: Request) -> Response:
        ctx = await guard.context(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        try:
            start = date.fromisoformat(str(form.get("start", "")))
            end = date.fromisoformat(str(form.get("end", "")))
            price_raw = str(form.get("price_eur", "")).strip()
            await services.inventory.set_days(
                ctx.tenant_id,
                str(form.get("room_type", "")),
                start,
                end,
                units_available=_int_or_none(form.get("units_available")),
                min_stay=_int_or_none(form.get("min_stay")),
                closed_arrival=_bool_or_none(form.get("closed_arrival")),
                closed_departure=_bool_or_none(form.get("closed_departure")),
                price_cents=round(float(price_raw.replace(",", ".")) * 100) if price_raw else None,
                actor=ctx.principal.user.id,
            )
        except ValueError as exc:
            return RedirectResponse(
                f"/admin/availability?report={json.dumps({'errors': [str(exc)]})}", status_code=303
            )
        return RedirectResponse(f"/admin/availability?month={start:%Y-%m}", status_code=303)

    @api.post("/availability/csv")
    async def availability_csv(request: Request) -> Response:
        ctx = await guard.context(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        upload = form.get("file")
        dry_run = str(form.get("apply", "")) != "1"
        content = ""
        if upload is not None and hasattr(upload, "read"):
            content = (await upload.read()).decode("utf-8", errors="replace")
        report: CsvReport = await services.inventory.import_csv(
            ctx.tenant_id, content, dry_run=dry_run, actor=ctx.principal.user.id
        )
        summary = {
            "rows": len(report.rows),
            "errors": [f"line {line}: {message}" for line, message in report.errors],
            "applied": report.applied,
            "dry_run": report.dry_run,
        }
        return RedirectResponse(
            f"/admin/availability?report={json.dumps(summary)}", status_code=303
        )

    # ----------------------------------------------------------- reservations
    @api.get("/reservations", response_class=HTMLResponse)
    async def reservations_page(request: Request) -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        today = to_local(clock.now(), ctx.tenant_profile.timezone).date()
        rows = await services.reservations.between(
            ctx.tenant_id, today - timedelta(days=2), today + timedelta(days=60)
        )
        items = []
        for row in rows:
            personal = services.reservations.decrypt(row)
            items.append(
                {
                    "row": row,
                    "name": personal.get("name", ""),
                    "log": [e for e in row.meta.get("journey_log", []) if isinstance(e, dict)][-5:],
                    "pending": len(
                        [
                            k
                            for k in await _pending_journey_keys(
                                services, ctx.tenant_id, row.res_id
                            )
                        ]
                    ),
                }
            )
        return page(request, "reservations", ctx, items=items)

    @api.get("/reservations/{reservation_id}/journey", response_class=HTMLResponse)
    async def journey_preview(request: Request, reservation_id: str) -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        row = await services.reservations.get(ctx.tenant_id, reservation_id)
        if row is None:
            return templates.TemplateResponse(
                request, "admin/invalid.html", ctx.base(), status_code=404
            )
        lines = await services.journeys.preview(ctx.tenant_row, row)
        return page(request, "journey", ctx, reservation=row, lines=lines)

    @api.post("/reservations/{reservation_id}/pause")
    async def journey_pause(request: Request, reservation_id: str) -> Response:
        ctx = await guard.context(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        paused = str(form.get("paused", "1")) == "1"
        async with services.ctx.db.tenant_scope(ctx.tenant_id) as session:
            row = await session.get(Reservation, reservation_id)
            if row is not None and row.tenant_id == ctx.tenant_id:
                row.journey_paused = paused
                await session.commit()
                await session.refresh(row)
        if row is not None:
            if paused:
                await services.jobs.cancel(
                    dedupe_prefix=services.reservations.job_prefix(ctx.tenant_id, row.res_id)
                )
            else:
                await services.journeys.plan(ctx.tenant_row, row)
        return RedirectResponse("/admin/reservations", status_code=303)

    # -------------------------------------------------------------- templates
    @api.get("/templates", response_class=HTMLResponse)
    async def templates_page(request: Request, report: str = "") -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        rows = await services.templates.rows(ctx.tenant_id)
        return page(request, "templates", ctx, rows=rows, report=report)

    @api.post("/templates/sync")
    async def templates_sync(request: Request) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        await services.templates.install_standard(ctx.tenant_id, actor=ctx.principal.user.id)
        if not ctx.tenant_profile.whatsapp.waba_id:
            return RedirectResponse("/admin/templates?report=no_waba_id", status_code=303)
        result = await services.templates.sync(
            ctx.tenant_id, ctx.tenant_profile.whatsapp.waba_id, actor=ctx.principal.user.id
        )
        summary = f"created {len(result.created)}, existing {len(result.existing)}, budget {len(result.skipped_budget)}, errors {len(result.errors)}"
        return RedirectResponse(f"/admin/templates?report={summary}", status_code=303)

    # --------------------------------------------------------------- settings
    @api.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, error: str = "") -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        return page(
            request, "settings", ctx, integrations=await _integrations(services, ctx), error=error
        )

    @api.post("/settings/quiet-hours")
    async def settings_quiet_hours(request: Request) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        value = str(form.get("quiet_hours", "")).strip()
        try:
            parse_range(value)
            data = ctx.tenant_profile.model_dump(mode="json")
            data["journeys"]["quiet_hours"] = value
            await services.tenants.update_profile(
                ctx.tenant_row.slug, TenantProfile.model_validate(data), actor=ctx.principal.user.id
            )
        except ValueError as exc:
            return RedirectResponse(f"/admin/settings?error={type(exc).__name__}", status_code=303)
        return RedirectResponse("/admin/settings", status_code=303)

    # ------------------------------------------------------------------ legal
    @api.get("/legal", response_class=HTMLResponse)
    async def legal_page(request: Request, error: str = "") -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        await services.legal.install_drafts(
            ctx.tenant_id, ctx.tenant_profile, actor=ctx.principal.user.id
        )
        rows = await services.legal.rows(ctx.tenant_id)
        return page(request, "legal", ctx, rows=rows, error=error)

    @api.post("/legal/{legal_id}/approve")
    async def legal_approve(request: Request, legal_id: str) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        try:
            await services.legal.approve(
                ctx.tenant_id,
                legal_id,
                approver_name=str(form.get("approver", "")),
                actor=ctx.principal.user.id,
            )
        except LegalError as exc:
            return RedirectResponse(f"/admin/legal?error={exc}", status_code=303)
        return RedirectResponse("/admin/legal", status_code=303)

    @api.post("/legal/{legal_id}/text")
    async def legal_text(request: Request, legal_id: str) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        try:
            await services.legal.update_text(
                ctx.tenant_id, legal_id, str(form.get("text", "")), actor=ctx.principal.user.id
            )
        except LegalError as exc:
            return RedirectResponse(f"/admin/legal?error={exc}", status_code=303)
        return RedirectResponse("/admin/legal", status_code=303)

    # ------------------------------------------------------------------- team
    @api.get("/team", response_class=HTMLResponse)
    async def team_page(request: Request, link: str = "", error: str = "") -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        users = await auth.users(ctx.tenant_id)
        return page(request, "team", ctx, users=users, link=link, error=error)

    @api.post("/team/invite")
    async def team_invite(request: Request) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        role = str(form.get("role", "staff"))
        if role not in {"owner", "staff"}:
            role = "staff"
        try:
            _user, token = await auth.invite(
                tenant_id=ctx.tenant_id,
                email=str(form.get("email", "")),
                name=str(form.get("name", "")),
                role=role,
                actor=ctx.principal.user.id,
                locale=str(form.get("locale", ctx.lang)),
            )
        except AuthError as exc:
            return RedirectResponse(f"/admin/team?error={exc.code}", status_code=303)
        link = f"{services.ctx.settings.public_base_url()}/admin/invite/{token}"
        if services.email_outbox.configured:
            await services.email_outbox.enqueue(
                ctx.tenant_id,
                ctx.tenant_profile,
                to=str(form.get("email", "")),
                subject=f"{ctx.tenant_profile.name}: Guest Concierge",
                text=f"{label('invite', ctx.lang)}: {link}\n(15 min)",
                idempotency_key=f"invite:{token[:16]}",
            )
        return RedirectResponse(f"/admin/team?link={link}", status_code=303)

    @api.post("/team/{user_id}/active")
    async def team_active(request: Request, user_id: str) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        if user_id == ctx.principal.user.id:
            return RedirectResponse("/admin/team?error=self", status_code=303)
        users = {u.id for u in await auth.users(ctx.tenant_id)}
        if user_id not in users:
            raise Forbidden("user of another tenant")
        await auth.set_active(
            user_id, str(form.get("active", "0")) == "1", actor=ctx.principal.user.id
        )
        return RedirectResponse("/admin/team", status_code=303)

    # ---------------------------------------------------------- data requests
    @api.get("/data-requests", response_class=HTMLResponse)
    async def data_requests(
        request: Request, q: str = "", kind: str = "phone", notice: str = ""
    ) -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        rows = await services.gdpr.requests(ctx.tenant_id)
        subject = await _subject(services, ctx.tenant_id, kind, q) if q.strip() else None
        async with services.ctx.db.scope() as session:
            deletions = list(
                (
                    await session.execute(
                        select(DeletionLog)
                        .where(DeletionLog.tenant_id == ctx.tenant_id)
                        .order_by(DeletionLog.created_at.desc())
                        .limit(30)
                    )
                ).scalars()
            )
        return page(
            request,
            "data_requests",
            ctx,
            rows=rows,
            q=q,
            kind=kind,
            subject=subject,
            notice=notice,
            deletions=deletions,
        )

    @api.post("/data-requests")
    async def data_request_create(request: Request) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        kind, q = str(form.get("kind", "phone")), str(form.get("q", ""))
        subject = await _subject(services, ctx.tenant_id, kind, q)
        action = str(form.get("action", ""))
        if subject is None:
            return RedirectResponse("/admin/data-requests?notice=invalid", status_code=303)
        try:
            if action in {"export", "erasure", "restriction"}:
                await services.gdpr.create_request(
                    ctx.tenant_id, action, subject, actor=ctx.principal.user.id
                )
                if action == "restriction":
                    await services.gdpr.restrict(
                        ctx.tenant_id, subject, True, actor=ctx.principal.user.id
                    )
            elif action == "unrestrict":
                await services.gdpr.restrict(
                    ctx.tenant_id, subject, False, actor=ctx.principal.user.id
                )
            elif action == "erase":
                if str(form.get("confirm", "")).strip().upper() not in {"LÖSCHEN", "ERASE"}:
                    return RedirectResponse(
                        f"/admin/data-requests?kind={kind}&q={q}&notice=confirm", status_code=303
                    )
                request_id = str(form.get("request_id", "")) or None
                await services.gdpr.erase(
                    ctx.tenant_id, subject, actor=ctx.principal.user.id, request_id=request_id
                )
                return RedirectResponse("/admin/data-requests?notice=erased", status_code=303)
            elif action == "task":
                await services.gdpr.complete_task(
                    ctx.tenant_id,
                    str(form.get("request_id", "")),
                    str(form.get("task_id", "")),
                    actor=ctx.principal.user.id,
                )
        except GdprError as exc:
            return RedirectResponse(f"/admin/data-requests?notice={exc}", status_code=303)
        return RedirectResponse(
            f"/admin/data-requests?kind={kind}&q={q}&notice=saved", status_code=303
        )

    @api.get("/data-requests/export")
    async def data_request_export(
        request: Request, kind: str = "phone", q: str = "", fmt: str = "json"
    ) -> Response:
        ctx = await guard.context(request, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        subject = await _subject(services, ctx.tenant_id, kind, q)
        if subject is None or not subject.found:
            return Response(status_code=404)
        data = await services.gdpr.export(ctx.tenant_id, subject)
        async with services.ctx.db.scope() as session:
            from concierge import audit

            audit.record(
                session,
                actor=ctx.principal.user.id,
                action="gdpr.exported",
                target=subject.subject_bidx[:16],
                tenant_id=ctx.tenant_id,
                details={"format": fmt},
            )
            await session.commit()
        if fmt == "pdf":
            return Response(
                services.gdpr.export_pdf(data, hotel=ctx.tenant_profile.name),
                media_type="application/pdf",
                headers={"Content-Disposition": 'attachment; filename="data-export.pdf"'},
            )
        return JSONResponse(
            data, headers={"Content-Disposition": 'attachment; filename="data-export.json"'}
        )

    # ----------------------------------------------------------------- search
    @api.get("/search", response_class=HTMLResponse)
    async def search(request: Request, q: str = "") -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        hits: list[dict[str, Any]] = []
        needle = q.strip()
        if len(needle) >= 3:
            async with services.ctx.db.tenant_scope(ctx.tenant_id) as session:
                rows = (
                    await session.execute(
                        select(Message, MessageText)
                        .join(MessageText, MessageText.id == Message.id)
                        .where(
                            Message.tenant_id == ctx.tenant_id,
                            MessageText.text.ilike(f"%{needle}%"),
                        )
                        .order_by(Message.occurred_at.desc())
                        .limit(50)
                    )
                ).all()
            hits = [
                {
                    "conversation_id": m.conversation_id,
                    "direction": m.direction,
                    "at": format_local(m.occurred_at, ctx.tenant_profile.timezone, ctx.lang),
                    "text": t.text[:200],
                }
                for m, t in rows
            ]
        return page(request, "search", ctx, q=needle, hits=hits)

    @api.post("/conversations/{conversation_id}/flag")
    async def flag_reply(request: Request, conversation_id: str) -> Response:
        ctx = await guard.context(request, mutation=True)
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        message_id = str(form.get("message_id", "")) or None
        await services.qa.flag(
            ctx.tenant_id,
            conversation_id,
            message_id=message_id,
            user_id=ctx.principal.user.id,
            note=str(form.get("note", "")),
        )
        return RedirectResponse(
            f"/admin/conversations/{conversation_id}?notice=flagged", status_code=303
        )

    @api.get("/conversations/{conversation_id}/export")
    async def export_conversation(
        request: Request, conversation_id: str, redacted: str = "1"
    ) -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        async with services.ctx.db.tenant_scope(ctx.tenant_id) as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None or conversation.tenant_id != ctx.tenant_id:
                return Response(status_code=404)
            guest = await session.get(Guest, conversation.guest_id)
            rows = (
                await session.execute(
                    select(Message, MessageText)
                    .outerjoin(MessageText, MessageText.id == Message.id)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.occurred_at)
                )
            ).all()
        names: list[str] = []
        if guest is not None and guest.name_enc is not None:
            names.append(services.ctx.keyring.decrypt_str(guest.name_enc, ctx.tenant_id, "name"))
        do_redact = redacted != "0"
        payload = {
            "conversation": conversation_id,
            "channel": conversation.channel,
            "language": conversation.lang,
            "redacted": do_redact,
            "messages": [
                {
                    "at": m.occurred_at.isoformat(),
                    "direction": m.direction,
                    "kind": m.kind,
                    "text": (
                        redact(t.text, names=names)
                        if (t is not None and do_redact)
                        else (t.text if t else None)
                    ),
                }
                for m, t in rows
            ],
        }
        return JSONResponse(
            payload,
            headers={"Content-Disposition": f'attachment; filename="{conversation_id}.json"'},
        )

    # ---------------------------------------------------------------- reports
    @api.get("/reports", response_class=HTMLResponse)
    async def reports(request: Request, days: int = 30) -> Response:
        ctx = await guard.context(request)
        if isinstance(ctx, Response):
            return ctx
        days = max(1, min(days, 365))
        end = clock.now()
        start = end - timedelta(days=days)
        summary = await services.analytics.summary(ctx.tenant_id, ctx.tenant_profile, start, end)
        charts = services.analytics.charts(summary, ctx.lang)
        month = to_local(end, ctx.tenant_profile.timezone).date()
        usage = await services.metering.month_report(
            ctx.tenant_id, ctx.tenant_profile.name, ctx.tenant_profile, month.year, month.month
        )
        return page(
            request,
            "reports",
            ctx,
            days=days,
            summary=summary,
            charts=charts,
            usage=usage,
            month=usage.label,
            period=f"{format_local(start, ctx.tenant_profile.timezone, ctx.lang)} – {format_local(end, ctx.tenant_profile.timezone, ctx.lang)}",
        )

    @api.get("/reports/usage.{fmt}")
    async def usage_export(request: Request, fmt: str, month: str = "") -> Response:
        ctx = await guard.context(request, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        try:
            year, mon = (
                (int(p) for p in month.split("-"))
                if month
                else (
                    to_local(clock.now(), ctx.tenant_profile.timezone).year,
                    to_local(clock.now(), ctx.tenant_profile.timezone).month,
                )
            )
        except ValueError:
            return Response(status_code=400)
        report = await services.metering.month_report(
            ctx.tenant_id, ctx.tenant_profile.name, ctx.tenant_profile, year, mon
        )
        name = f"usage-{report.label}"
        if fmt == "csv":
            return Response(
                report.to_csv(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
            )
        if fmt == "pdf":
            return Response(
                report.to_pdf(),
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'},
            )
        return Response(
            report.to_json(),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
        )

    # ----------------------------------------------------------- registration
    @api.get("/registration", response_class=HTMLResponse)
    async def registration_page(request: Request) -> Response:
        ctx = await guard.context(request, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        today = to_local(clock.now(), ctx.tenant_profile.timezone).date()
        return page(request, "registration", ctx, start=today - timedelta(days=30), end=today)

    @api.post("/registration/export")
    async def registration_export(request: Request) -> Response:
        ctx = await guard.context(request, mutation=True, roles=("owner", "operator"))
        if isinstance(ctx, Response):
            return ctx
        form = await request.form()
        try:
            start = date.fromisoformat(str(form.get("start", "")))
            end = date.fromisoformat(str(form.get("end", "")))
        except ValueError:
            return RedirectResponse("/admin/registration", status_code=303)
        rows = await services.registration.export(ctx.tenant_id, start, end)
        async with services.ctx.db.scope() as session:
            from concierge import audit

            audit.record(
                session,
                actor=ctx.principal.user.id,
                action="meldeschein.export",
                tenant_id=ctx.tenant_id,
                details={"rows": len(rows), "start": start.isoformat(), "end": end.isoformat()},
            )
            await session.commit()
        return JSONResponse(
            rows,
            headers={
                "Content-Disposition": f'attachment; filename="meldeschein-{start}-{end}.json"'
            },
        )

    return api


# ------------------------------------------------------------------ helpers
async def _subject(services: Services, tenant_id: str, kind: str, q: str) -> Subject | None:
    value = q.strip()
    if not value:
        return None
    try:
        if kind == "email":
            return await services.gdpr.search(tenant_id, email=value)
        if kind == "res_id":
            return await services.gdpr.search(tenant_id, res_id=value)
        return await services.gdpr.search(tenant_id, phone=value)
    except GdprError:
        return None


async def _integrations(services: Services, ctx: AdminContext) -> list[dict[str, Any]]:
    names = set(await services.secrets.names(ctx.tenant_id))
    items = [
        {
            "name": "WhatsApp",
            "ok": bool(ctx.tenant_row.phone_number_id) and "wa_access_token" in names,
            "detail": ctx.tenant_row.phone_number_id or "-",
        },
        {
            "name": "E-mail",
            "ok": services.email_outbox.configured,
            "detail": services.ctx.settings.gc_email_provider,
        },
        {
            "name": "Availability",
            "ok": True,
            "detail": ", ".join(ctx.tenant_profile.availability.chain),
        },
        {
            "name": "Registration",
            "ok": ctx.tenant_profile.registration.enabled,
            "detail": ctx.tenant_profile.registration.psp,
        },
        {
            "name": "Voice",
            "ok": ctx.tenant_profile.voice.provider != "none",
            "detail": ctx.tenant_profile.voice.provider,
        },
        {
            "name": "LLM",
            "ok": services.llm is not None,
            "detail": "configured" if services.llm else "responder only",
        },
    ]
    return items


async def _inbox_item(services: Services, ctx: AdminContext, handoff: Handoff) -> dict[str, Any]:
    async with services.ctx.db.tenant_scope(ctx.tenant_id) as session:
        conversation = await session.get(Conversation, handoff.conversation_id)
        last = (
            await session.execute(
                select(MessageText.text)
                .join(Message, Message.id == MessageText.id)
                .where(
                    Message.conversation_id == handoff.conversation_id, Message.direction == "in"
                )
                .order_by(Message.occurred_at.desc())
                .limit(1)
            )
        ).scalar()
    now = services.ctx.clock.now()
    remaining = None
    if handoff.sla_due_at is not None:
        remaining = int((handoff.sla_due_at - now).total_seconds() // 60)
    return {
        "handoff": handoff,
        "conversation": conversation,
        "lang": conversation.lang if conversation else "",
        "snippet": (last or "")[:80],
        "sla_minutes": remaining,
        "window": _window_state(conversation, now),
        "due_local": format_local(handoff.sla_due_at, ctx.tenant_profile.timezone, ctx.lang)
        if handoff.sla_due_at
        else "",
    }


def _window_state(conversation: Conversation | None, now: datetime) -> str:
    if conversation is None or conversation.channel != "whatsapp":
        return "n/a"
    if conversation.window_expires_at is None or conversation.window_expires_at <= now:
        return "closed"
    if conversation.window_expires_at - now <= WINDOW_WARN:
        return "warn"
    return "open"


async def _conversation_view(
    services: Services, ctx: AdminContext, conversation_id: str
) -> dict[str, Any] | None:
    tenant_id = ctx.tenant_id
    async with services.ctx.db.tenant_scope(tenant_id) as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.tenant_id != tenant_id:
            return None
        guest = await session.get(Guest, conversation.guest_id)
        rows = (
            await session.execute(
                select(Message, MessageText)
                .join(MessageText, MessageText.id == Message.id)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.occurred_at, Message.created_at)
            )
        ).all()
    now = services.ctx.clock.now()
    transcript = [
        {
            "id": m.id,
            "direction": m.direction,
            "text": t.text,
            "status": m.status,
            "kind": m.kind,
            "intent": m.intent or "",
            "lang": m.lang or "",
            "at": format_local(m.occurred_at, ctx.tenant_profile.timezone, ctx.lang),
            "translation": (m.meta or {}).get("translation", "")
            if isinstance(m.meta, dict)
            else "",
        }
        for m, t in rows[-80:]
    ]
    handoff = await services.handoffs.open_for_conversation(tenant_id, conversation_id)
    notes = await services.handoffs.notes(tenant_id, conversation_id)
    last_guest = next((t.text for m, t in reversed(rows) if m.direction == "in"), "")
    suggested = ""
    if last_guest:
        try:
            reply, _views, _slots = await services.brain.responder.respond(
                last_guest,
                ResponderContext(
                    tenant_id=tenant_id,
                    lang=conversation.lang,
                    channel=conversation.channel,
                    verified=conversation.verified_reservation_id is not None,
                    stay_date=None,
                    hotel_phone=ctx.tenant_profile.contacts.reception_phone,
                ),
            )
            suggested = reply.reply_text if reply.intent_code in {"A", "P"} else ""
        except Exception:
            suggested = ""
    template_rows = [
        r
        for r in await services.templates.rows(tenant_id)
        if r.status == "APPROVED" and r.lang == conversation.lang
    ]
    return {
        "conversation": conversation,
        "guest_ref": (guest.channel_key[-4:] if guest else "").rjust(4, "*"),
        "guest_lang": conversation.lang,
        "transcript": transcript,
        "handoff": handoff,
        "notes": notes,
        "suggested": suggested,
        "window": _window_state(conversation, now),
        "window_until": format_local(
            conversation.window_expires_at, ctx.tenant_profile.timezone, ctx.lang
        )
        if conversation.window_expires_at
        else "",
        "template_rows": template_rows,
        "sla_text": ctx.tenant_profile.handoff.response_window_text.get(conversation.lang, ""),
        "email_possible": services.email_outbox.configured and bool(guest and guest.email_enc),
    }


_SAFE_NEXT = re.compile(r"^/(?![/\\])[A-Za-z0-9_./?=&%+-]*$")


async def _suppressed(services: Services, tenant_id: str, to: str) -> bool:
    subject = services.ctx.keyring.blind_index("+" + to.lstrip("+"), tenant_id, "phone")
    async with services.ctx.db.tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(Suppression).where(
                Suppression.tenant_id == tenant_id,
                Suppression.channel == "whatsapp",
                Suppression.subject_bidx == subject,
            )
        )
    return row is not None


async def _staff_reply(
    services: Services,
    ctx: AdminContext,
    conversation_id: str,
    *,
    mode: str,
    text: str,
    template_name: str,
) -> str:
    tenant_id = ctx.tenant_id
    async with services.ctx.db.tenant_scope(tenant_id) as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.tenant_id != tenant_id:
            return "unknown"
        guest = await session.get(Guest, conversation.guest_id)
    if guest is None:
        return "unknown"
    now = services.ctx.clock.now()
    lang = conversation.lang
    retention = ctx.tenant_profile.retention.chat_days
    if conversation.channel == "whatsapp":
        to = guest.channel_key
        if mode == "template":
            # a template is a business-initiated send: STOP and quiet hours apply (C9)
            if conversation.stopped_at is not None or await _suppressed(services, tenant_id, to):
                return "suppressed"
            local_now = to_local(now, ctx.tenant_profile.timezone).time()
            if ctx.tenant_profile.journeys.is_quiet(local_now):
                open_handoff = await services.handoffs.open_for_conversation(
                    tenant_id, conversation_id
                )
                if open_handoff is None or open_handoff.priority != "P0":
                    return "quiet_hours"
            values = {
                "hotel": ctx.tenant_profile.name,
                "guest_name": "",
                "phone": ctx.tenant_profile.contacts.reception_phone or "-",
            }
            try:
                payload, rendered = await services.templates.payload(
                    tenant_id, to, template_name, lang, values
                )
            except TemplateRefused:
                return "template_refused"
            message = await services.conversations.record_outbound(
                tenant_id,
                conversation_id,
                rendered,
                lang=lang,
                kind="template",
                intent="S:template",
                retention_days=retention,
            )
            await services.outbox.enqueue(
                tenant_id,
                payload=payload,
                idempotency_key=f"staff:{message.id}",
                conversation_id=conversation_id,
                category="utility",
                message_id=message.id,
            )
            return "sent_template"
        if mode == "email":
            if not services.email_outbox.configured or guest.email_enc is None:
                return "email_unavailable"
            address = services.ctx.keyring.decrypt_str(guest.email_enc, tenant_id, "email")
            message = await services.conversations.record_outbound(
                tenant_id,
                conversation_id,
                text,
                lang=lang,
                kind="email",
                intent="S:email",
                retention_days=retention,
            )
            await services.email_outbox.enqueue(
                tenant_id,
                ctx.tenant_profile,
                to=address,
                subject=ctx.tenant_profile.name,
                text=text,
                idempotency_key=f"staff-email:{message.id}",
                conversation_id=conversation_id,
            )
            return "sent_email"
        if not text:
            return "empty"
        if conversation.window_expires_at is None or conversation.window_expires_at <= now:
            return "window_closed"
        message = await services.conversations.record_outbound(
            tenant_id,
            conversation_id,
            text,
            lang=lang,
            kind="text",
            intent="S:reply",
            retention_days=retention,
        )
        await services.outbox.enqueue(
            tenant_id,
            payload=text_payload(to, text),
            idempotency_key=f"staff:{message.id}",
            conversation_id=conversation_id,
            category="service",
            message_id=message.id,
        )
    else:
        if not text:
            return "empty"
        await services.conversations.record_outbound(
            tenant_id,
            conversation_id,
            text,
            lang=lang,
            kind="text",
            intent="S:reply",
            retention_days=retention,
            status="sent",
        )
    if conversation.status != "staff_active":
        await services.conversations.update(
            tenant_id,
            conversation_id,
            status="staff_active",
            assigned_user_id=ctx.principal.user.id,
        )
    return "sent"


async def _pending_journey_keys(services: Services, tenant_id: str, res_id: str) -> list[str]:
    prefix = services.reservations.job_prefix(tenant_id, res_id)
    jobs = await services.jobs.pending(tenant_id=tenant_id, kind="journey_step")
    return [j.dedupe_key or "" for j in jobs if (j.dedupe_key or "").startswith(prefix)]


def _int_or_none(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text else None


def _bool_or_none(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text == "":
        return None
    return text in {"1", "true", "on", "ja", "yes"}
