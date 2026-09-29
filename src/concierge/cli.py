"""The ``concierge`` command line: every operational task is a subcommand.

Implements: F28, F33, F21, C1, C14

The command tree grows with the modules: ``db``, ``check``, ``setup``, ``tenant``,
``secrets``, ``doctor``, ``demo``, ``serve``, ``wa-sim``, ``pack``, ``kb``,
``whatsapp``, ``voice``, ``usage``, ``backup``, ``restore-test``, ``golden``,
``eval``, ``handout``, ``compliance``. Standard streams are switched to UTF-8 so that
Cyrillic output never fails under a legacy Windows code page.
"""

from __future__ import annotations

import asyncio
import secrets as _pysecrets
import sys
from collections.abc import Coroutine
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import typer
import yaml

from concierge import __version__

if TYPE_CHECKING:
    from concierge.context import AppContext
    from concierge.services import Services

app = typer.Typer(no_args_is_help=True, add_completion=False, help="WITALCO Guest Concierge")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations")
tenant_app = typer.Typer(no_args_is_help=True, help="Tenant lifecycle")
secrets_app = typer.Typer(no_args_is_help=True, help="Per-tenant secrets (encrypted at rest)")
app.add_typer(db_app, name="db")
app.add_typer(tenant_app, name="tenant")
app.add_typer(secrets_app, name="secrets")
whatsapp_app = typer.Typer(no_args_is_help=True, help="WhatsApp: templates")
templates_app = typer.Typer(no_args_is_help=True, help="Template registry: lint, list, sync")
whatsapp_app.add_typer(templates_app, name="templates")
journeys_app = typer.Typer(no_args_is_help=True, help="Guest journeys: preview, test-send")
registration_app = typer.Typer(no_args_is_help=True, help="Registration (Meldeschein): export")
demo_app = typer.Typer(
    invoke_without_command=True,
    help="Seed the fictional Hotel Beispiel and serve; `advance` warps the demo clock",
)
app.add_typer(whatsapp_app, name="whatsapp")
app.add_typer(journeys_app, name="journeys")
app.add_typer(registration_app, name="registration")
app.add_typer(demo_app, name="demo")
pack_app = typer.Typer(no_args_is_help=True, help="Client packs: export, validate, diff, import")
app.add_typer(pack_app, name="pack")
voice_app = typer.Typer(no_args_is_help=True, help="Voice fallback: provision the provider")
app.add_typer(voice_app, name="voice")
usage_app = typer.Typer(no_args_is_help=True, help="Metering: monthly usage export")
app.add_typer(usage_app, name="usage")
gdpr_app = typer.Typer(no_args_is_help=True, help="GDPR: search, export, erase, retention")
app.add_typer(gdpr_app, name="gdpr")
wa_sim_app = typer.Typer(no_args_is_help=True, help="WhatsApp simulator against a running server")
app.add_typer(wa_sim_app, name="wa-sim")
handout_app = typer.Typer(no_args_is_help=True, help="The PDF handout (section 13)")
app.add_typer(handout_app, name="handout")
compliance_app = typer.Typer(no_args_is_help=True, help="Compliance pack per tenant (drafts)")
app.add_typer(compliance_app, name="compliance")
golden_app = typer.Typer(no_args_is_help=True, help="Golden evaluation with the scripted model")
app.add_typer(golden_app, name="golden")
eval_app = typer.Typer(no_args_is_help=True, help="Live evaluation against the real model")
app.add_typer(eval_app, name="eval")


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from a CLI command (no event loop policy changes, 6.8)."""
    return asyncio.run(coro)


def _utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=code)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines)


# --------------------------------------------------------------------- basics
@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(f"concierge {__version__}")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Migrate the configured database to the latest revision."""
    from concierge.db.migrate import upgrade
    from concierge.db.session import Database
    from concierge.settings import get_settings

    async def _go() -> None:
        database = Database(get_settings().database_url())
        try:
            await upgrade(database.engine)
        finally:
            await database.dispose()

    run(_go())
    typer.echo("database is at head")


@db_app.command("current")
def db_current() -> None:
    """Show the database revision and the script head."""
    from concierge.db.migrate import head_state
    from concierge.db.session import Database
    from concierge.settings import get_settings

    async def _go() -> tuple[str | None, str | None]:
        database = Database(get_settings().database_url())
        try:
            return await head_state(database.engine)
        finally:
            await database.dispose()

    current, head = run(_go())
    typer.echo(f"database: {current or 'empty'}  head: {head}")


@app.command()
def check(
    skip_audit: bool = typer.Option(False, "--skip-audit", help="skip pip-audit (offline)"),
) -> None:
    """Run every static gate and print one table."""
    from concierge.tools.check import main as check_main

    raise typer.Exit(code=check_main(["--skip-audit"] if skip_audit else []))


@app.command()
def setup(
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="take every value from the environment or flags"
    ),
    env_path: Path = typer.Option(
        Path(".env"), "--env", help="where to write the environment file"
    ),
    database_url: str | None = typer.Option(None, "--database-url"),
    public_base_url: str | None = typer.Option(None, "--public-base-url"),
    operator_email: str | None = typer.Option(None, "--operator-email"),
    tenant_profile: Path | None = typer.Option(
        None, "--tenant-profile", help="YAML profile of the first tenant"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="regenerate keys even if .env exists (existing data becomes unreadable)",
    ),
) -> None:
    """First-run wizard: keys, .env, migration, operator account, one-time admin link."""
    from concierge.tools.setup import run_setup

    if (
        force
        and not non_interactive
        and not typer.confirm("Regenerate all keys? Existing encrypted data becomes unreadable.")
    ):
        raise typer.Exit(code=1)

    def _prompt(question: str, default: str) -> str:
        return str(typer.prompt(question, default=default, show_default=bool(default)))

    result = run(
        run_setup(
            env_path=env_path,
            non_interactive=non_interactive,
            database_url=database_url,
            public_base_url=public_base_url,
            operator_email=operator_email,
            tenant_profile=tenant_profile,
            force=force,
            echo=typer.echo,
            prompt=None if non_interactive else _prompt,
        )
    )
    typer.echo(f"environment written to {result.env_path}")
    typer.echo(f"database: {result.database_url}")
    if result.admin_link:
        typer.echo("one-time admin link (valid 24 hours, single use):")
        typer.echo(f"  {result.admin_link}")
    if result.tenant_slug:
        typer.echo(f"tenant: {result.tenant_slug}")
    typer.echo("next: `concierge doctor`, then `concierge demo` or `concierge serve`")


# -------------------------------------------------------------------- tenants
def _context() -> AppContext:
    from concierge.context import AppContext

    return AppContext.build()


@tenant_app.command("add")
def tenant_add(
    profile: Path | None = typer.Option(None, "--profile", help="YAML tenant profile (Appendix C)"),
    slug: str | None = typer.Option(None, "--slug"),
    name: str | None = typer.Option(None, "--name"),
    timezone: str = typer.Option("Europe/Berlin", "--timezone"),
) -> None:
    """Create a tenant from a profile file or from slug and name."""
    from concierge.tenants.profile import TenantProfile, validation_errors
    from concierge.tenants.service import TenantError, TenantService

    if profile is not None:
        data = yaml.safe_load(profile.read_text(encoding="utf-8"))
        errors = validation_errors(data)
        if errors:
            _fail("invalid profile:\n  " + "\n  ".join(errors))
        parsed = TenantProfile.model_validate(data)
    elif slug and name:
        parsed = TenantProfile(slug=slug, name=name, timezone=timezone, status="pilot")
    else:
        _fail("give --profile or both --slug and --name")

    async def _go() -> None:
        ctx = _context()
        try:
            await TenantService(ctx.db, ctx.clock).create(parsed, actor="cli")
        finally:
            await ctx.close()

    try:
        run(_go())
    except TenantError as exc:
        _fail(str(exc))
    typer.echo(f"tenant {parsed.slug} created")


@tenant_app.command("list")
def tenant_list() -> None:
    """List tenants with status and channel mapping."""
    from concierge.tenants.service import TenantService

    async def _go() -> list[list[str]]:
        ctx = _context()
        try:
            rows = []
            for t in await TenantService(ctx.db, ctx.clock).list():
                rows.append(
                    [
                        t.slug,
                        t.status
                        + (" (paused)" if t.paused else "")
                        + (" KILL" if t.kill_switch else ""),
                        t.ai_mode,
                        t.timezone,
                        t.phone_number_id or "-",
                        t.public_key,
                    ]
                )
            return rows
        finally:
            await ctx.close()

    rows = run(_go())
    typer.echo(
        _table(["slug", "status", "ai_mode", "timezone", "phone_number_id", "public_key"], rows)
    )


@tenant_app.command("pause")
def tenant_pause(slug: str) -> None:
    """Pause a tenant: no automated messages, guests get the degraded reply."""
    _set_paused(slug, True)


@tenant_app.command("unpause")
def tenant_unpause(slug: str) -> None:
    """Resume a paused tenant."""
    _set_paused(slug, False)


def _set_paused(slug: str, paused: bool) -> None:
    from concierge.tenants.service import TenantError, TenantService

    async def _go() -> None:
        ctx = _context()
        try:
            await TenantService(ctx.db, ctx.clock).set_paused(slug, paused, actor="cli")
        finally:
            await ctx.close()

    try:
        run(_go())
    except TenantError as exc:
        _fail(str(exc))
    typer.echo(f"tenant {slug} {'paused' if paused else 'resumed'}")


@tenant_app.command("kill-switch")
def tenant_kill_switch(slug: str, on: bool = typer.Option(True, "--on/--off")) -> None:
    """Per-tenant kill switch: guests get the degraded message, staff are alerted."""
    from concierge.tenants.service import TenantError, TenantService

    async def _go() -> None:
        ctx = _context()
        try:
            await TenantService(ctx.db, ctx.clock).set_kill_switch(slug, on, actor="cli")
        finally:
            await ctx.close()

    try:
        run(_go())
    except TenantError as exc:
        _fail(str(exc))
    typer.echo(f"kill switch for {slug}: {'on' if on else 'off'}")


@tenant_app.command("record-dpa")
def tenant_record_dpa(
    slug: str,
    signed_at: datetime | None = typer.Option(
        None, "--signed-at", help="date the processor agreement was signed (YYYY-MM-DD)"
    ),
    acknowledged_at: datetime | None = typer.Option(
        None,
        "--acknowledged-at",
        help="date the sub-processor list incl. US transfers was acknowledged",
    ),
) -> None:
    """Record the signed processor agreement and the sub-processor acknowledgement."""
    from datetime import UTC

    from concierge.tenants.service import TenantError, TenantService

    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def _go() -> None:
        ctx = _context()
        try:
            await TenantService(ctx.db, ctx.clock).record_dpa(
                slug,
                signed_at=_aware(signed_at),
                acknowledged_at=_aware(acknowledged_at),
                actor="cli",
            )
        finally:
            await ctx.close()

    try:
        run(_go())
    except TenantError as exc:
        _fail(str(exc))
    typer.echo("recorded")


@tenant_app.command("activate")
def tenant_activate(slug: str) -> None:
    """Activate a tenant; refuses while any F21 gate fails (runs doctor --live first)."""
    from concierge.tenants.service import TenantError, TenantService

    async def _go() -> list[str]:
        ctx = _context()
        try:
            from concierge.tools.doctor import live_ok_for_tenant

            doctor_ok = await live_ok_for_tenant(ctx, slug)
            report = await TenantService(ctx.db, ctx.clock).activate(
                slug, actor="cli", doctor_live_ok=doctor_ok
            )
            return report.failures
        finally:
            await ctx.close()

    try:
        failures = run(_go())
    except TenantError as exc:
        _fail(str(exc))
    if failures:
        typer.echo("activation refused:")
        for item in failures:
            typer.echo(f"  - {item}")
        raise typer.Exit(code=2)
    typer.echo(f"tenant {slug} activated")


# -------------------------------------------------------------------- secrets
async def _tenant_id(ctx: Any, slug: str) -> str:
    from concierge.tenants.service import TenantService

    tenant = await TenantService(ctx.db, ctx.clock).require(slug)
    return str(tenant.id)


@secrets_app.command("set")
def secrets_set(
    tenant: str,
    name: str,
    from_stdin: bool = typer.Option(False, "--stdin", help="read the value from standard input"),
) -> None:
    """Store a secret for a tenant (hidden prompt; never pass secrets as arguments)."""
    from concierge.tenants.secrets import SecretStore
    from concierge.tenants.service import TenantError

    value = (
        sys.stdin.readline().rstrip("\r\n")
        if from_stdin
        else typer.prompt("Value", hide_input=True)
    )

    async def _go() -> None:
        ctx = _context()
        try:
            tenant_id = await _tenant_id(ctx, tenant)
            await SecretStore(ctx.db, ctx.keyring).set(tenant_id, name, value, actor="cli")
        finally:
            await ctx.close()

    try:
        run(_go())
    except (TenantError, ValueError) as exc:
        _fail(str(exc))
    typer.echo(f"secret {name} stored for {tenant}")


@secrets_app.command("list")
def secrets_list(tenant: str) -> None:
    """List the names of a tenant's secrets (never the values)."""
    from concierge.tenants.secrets import SecretStore
    from concierge.tenants.service import TenantError

    async def _go() -> list[str]:
        ctx = _context()
        try:
            return await SecretStore(ctx.db, ctx.keyring).names(await _tenant_id(ctx, tenant))
        finally:
            await ctx.close()

    try:
        names = run(_go())
    except TenantError as exc:
        _fail(str(exc))
    typer.echo("\n".join(names) if names else "(none)")


@secrets_app.command("delete")
def secrets_delete(tenant: str, name: str) -> None:
    """Delete a tenant secret."""
    from concierge.tenants.secrets import SecretStore
    from concierge.tenants.service import TenantError

    async def _go() -> bool:
        ctx = _context()
        try:
            return await SecretStore(ctx.db, ctx.keyring).delete(
                await _tenant_id(ctx, tenant), name, actor="cli"
            )
        finally:
            await ctx.close()

    try:
        deleted = run(_go())
    except TenantError as exc:
        _fail(str(exc))
    typer.echo("deleted" if deleted else "no such secret")


@secrets_app.command("rotate")
def secrets_rotate() -> None:
    """Re-encrypt every secret and personal-data column under the active master key."""
    from concierge.tenants.secrets import SecretStore

    async def _go() -> int:
        ctx = _context()
        try:
            return await SecretStore(ctx.db, ctx.keyring).rotate_all(actor="cli")
        finally:
            await ctx.close()

    touched = run(_go())
    typer.echo(f"re-encrypted {touched} rows under the active key")


# ----------------------------------------------------------- templates and journeys
def _services(ctx: AppContext) -> Services:
    from concierge.channels.whatsapp.graph import GraphClient
    from concierge.services import build_services

    return build_services(
        ctx,
        llm=None,
        wa_port=GraphClient(version=ctx.settings.gc_wa_graph_version),
    )


@templates_app.command("lint")
def templates_lint() -> None:
    """Lint the standard template set (names, limits, variables, cues, disclosure, STOP)."""
    from concierge.templates.service import lint_all, load_standard

    definitions = load_standard()
    issues = lint_all(definitions)
    if issues:
        typer.echo(
            _table(
                ["template", "lang", "rule", "detail"],
                [[i.name, i.lang, i.rule, i.detail[:60]] for i in issues],
            )
        )
        _fail(f"{len(issues)} lint issues")
    typer.echo(f"{len(definitions)} templates lint clean")


@templates_app.command("list")
def templates_list(tenant: str) -> None:
    """List a tenant's templates with their Meta status."""

    async def _go() -> list[list[str]]:
        ctx = _context()
        try:
            services = _services(ctx)
            rows = await services.templates.rows(await _tenant_id(ctx, tenant))
            return [
                [
                    r.name,
                    r.lang,
                    r.category,
                    r.status,
                    r.meta_template_id or "",
                    r.status_reason or "",
                ]
                for r in rows
            ]
        finally:
            await ctx.close()

    rows = run(_go())
    typer.echo(
        _table(["name", "lang", "category", "status", "meta id", "reason"], rows)
        if rows
        else "(no templates; run `concierge whatsapp templates sync`)"
    )


@templates_app.command("sync")
def templates_sync(tenant: str) -> None:
    """Install the standard set and create the missing templates at Meta."""
    from concierge.channels.whatsapp.port import GraphError
    from concierge.tenants.service import TenantError, TenantService

    async def _go() -> tuple[int, Any]:
        ctx = _context()
        try:
            services = _services(ctx)
            row = await TenantService(ctx.db, ctx.clock).require(tenant)
            profile = TenantService.profile_of(row)
            if not profile.whatsapp.waba_id:
                raise TenantError("the tenant profile has no whatsapp.waba_id")
            created = await services.templates.install_standard(row.id, actor="cli")
            report = await services.templates.sync(row.id, profile.whatsapp.waba_id, actor="cli")
            return created, report
        finally:
            await ctx.close()

    try:
        created, report = run(_go())
    except TenantError as exc:
        _fail(str(exc))
    except GraphError as exc:
        _fail(
            f"Meta Graph API: {exc}; check the network, wa_access_token and whatsapp.waba_id "
            "(docs/INTEGRATIONS.md)"
        )
    typer.echo(
        f"installed {created} new registry rows; created at Meta: {len(report.created)}, "
        f"existing: {len(report.existing)}, skipped (budget): {len(report.skipped_budget)}"
    )
    for error in report.errors:
        typer.echo(f"error: {error}")
    if report.errors:
        _fail("sync finished with errors")


@journeys_app.command("preview")
def journeys_preview(tenant: str, res_id: str) -> None:
    """Show the planned journey of a reservation with rendered texts."""
    from concierge.tenants.service import TenantError, TenantService

    async def _go() -> list[list[str]]:
        ctx = _context()
        try:
            services = _services(ctx)
            row = await TenantService(ctx.db, ctx.clock).require(tenant)
            reservation = await services.reservations.by_res_id(row.id, res_id)
            if reservation is None:
                raise TenantError(f"reservation {res_id} not found")
            lines = await services.journeys.preview(row, reservation)
            return [
                [line.step, line.local_at.strftime("%Y-%m-%d %H:%M"), line.template, line.text[:70]]
                for line in lines
            ]
        finally:
            await ctx.close()

    try:
        rows = run(_go())
    except TenantError as exc:
        _fail(str(exc))
    typer.echo(_table(["step", "local time", "template", "text"], rows) if rows else "(no steps)")


@journeys_app.command("test-send")
def journeys_test_send(
    tenant: str,
    step: str,
    to: str = typer.Option(..., "--to", help="staff phone in E.164"),
    lang: str = typer.Option("de", "--lang"),
) -> None:
    """Send one journey step with sample values to a staff phone (needs an APPROVED template)."""
    from concierge.templates.service import TemplateRefused
    from concierge.tenants.service import TenantError, TenantService

    async def _go() -> tuple[str, str, int]:
        ctx = _context()
        try:
            services = _services(ctx)
            row = await TenantService(ctx.db, ctx.clock).require(tenant)
            outbox_id, text = await services.journeys.test_send(row, to, step, lang)
            processed = await services.worker.drain()
            return outbox_id, text, processed
        finally:
            await ctx.close()

    try:
        outbox_id, text, processed = run(_go())
    except (TenantError, TemplateRefused, ValueError) as exc:
        _fail(str(exc))
    typer.echo(f"queued {outbox_id} ({processed} jobs processed)\n{text}")


@registration_app.command("export")
def registration_export(
    tenant: str,
    start: datetime = typer.Option(..., "--from", formats=["%Y-%m-%d"]),
    end: datetime = typer.Option(..., "--to", formats=["%Y-%m-%d"]),
    out: Path | None = typer.Option(None, "--out", help="write JSON to this file"),
) -> None:
    """Machine-readable Meldeschein export for the authorities (decrypted; handle with care)."""
    import json

    from concierge.tenants.service import TenantError

    async def _go() -> list[dict[str, Any]]:
        ctx = _context()
        try:
            services = _services(ctx)
            return await services.registration.export(
                await _tenant_id(ctx, tenant), start.date(), end.date()
            )
        finally:
            await ctx.close()

    try:
        rows = run(_go())
    except TenantError as exc:
        _fail(str(exc))
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    if out is not None:
        out.write_text(payload, encoding="utf-8")
        typer.echo(f"{len(rows)} rows written to {out}")
    else:
        typer.echo(payload)


# --------------------------------------------------------------------- serving
# -------------------------------------------------------------------- metering
@usage_app.command("export")
def usage_export(
    tenant: str = typer.Option(..., "--tenant"),
    month: str = typer.Option(..., "--month", help="YYYY-MM"),
    fmt: str = typer.Option("json", "--format", help="json, csv or pdf"),
    out: Path | None = typer.Option(None, "--out", help="file to write (default: stdout)"),
) -> None:
    """Write the tenant's usage report for one month (F24); no charge is made."""
    from concierge.tenants.service import TenantError

    try:
        year, mon = (int(p) for p in month.split("-"))
    except ValueError:
        _fail("--month must look like 2026-09")
    if fmt not in {"json", "csv", "pdf"}:
        _fail("--format must be json, csv or pdf")
    ctx = _context()
    services = _services(ctx)

    async def go() -> Any:
        row = await services.tenants.require(tenant)
        return await services.metering.month_report(
            row.id, row.name, services.tenants.profile_of(row), year, mon
        )

    try:
        report = run(go())
    except TenantError as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    if fmt == "pdf":
        target = out or Path(f"usage-{report.label}.pdf")
        target.write_bytes(report.to_pdf())
        typer.echo(f"written to {target}")
        return
    text = report.to_csv() if fmt == "csv" else report.to_json()
    if out is not None:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"written to {out}")
    else:
        typer.echo(text)


# ------------------------------------------------------------------------ GDPR
def _subject_lookup(services: Any, tenant_id: str, phone: str, email: str, res_id: str) -> Any:
    if phone:
        return services.gdpr.search(tenant_id, phone=phone)
    if email:
        return services.gdpr.search(tenant_id, email=email)
    return services.gdpr.search(tenant_id, res_id=res_id)


@gdpr_app.command("search")
def gdpr_search(
    tenant: str = typer.Option(..., "--tenant"),
    phone: str = typer.Option("", "--phone"),
    email: str = typer.Option("", "--email"),
    res_id: str = typer.Option("", "--res-id"),
) -> None:
    """Find a data subject by phone, e-mail or booking reference (blind index)."""
    if not (phone or email or res_id):
        _fail("give --phone, --email or --res-id")
    ctx = _context()
    services = _services(ctx)

    async def go() -> Any:
        row = await services.tenants.require(tenant)
        return await _subject_lookup(services, row.id, phone, email, res_id)

    try:
        subject = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    typer.echo(
        f"guests {len(subject.guest_ids)}, reservations {len(subject.reservation_ids)}, "
        f"conversations {len(subject.conversation_ids)}"
    )
    if not subject.found:
        raise typer.Exit(code=1)


@gdpr_app.command("export")
def gdpr_export(
    tenant: str = typer.Option(..., "--tenant"),
    out: Path = typer.Option(..., "--out", help=".json or .pdf file"),
    phone: str = typer.Option("", "--phone"),
    email: str = typer.Option("", "--email"),
    res_id: str = typer.Option("", "--res-id"),
) -> None:
    """Export everything stored about one data subject (Art. 15)."""
    import json

    if not (phone or email or res_id):
        _fail("give --phone, --email or --res-id")
    ctx = _context()
    services = _services(ctx)

    async def go() -> Any:
        row = await services.tenants.require(tenant)
        subject = await _subject_lookup(services, row.id, phone, email, res_id)
        if not subject.found:
            _fail("no data for this subject")
        data = await services.gdpr.export(row.id, subject)
        return data, row.name

    try:
        data, hotel = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    if out.suffix.lower() == ".pdf":
        out.write_bytes(services.gdpr.export_pdf(data, hotel=hotel))
    else:
        out.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    typer.echo(f"written to {out}")


@gdpr_app.command("erase")
def gdpr_erase(
    tenant: str = typer.Option(..., "--tenant"),
    phone: str = typer.Option("", "--phone"),
    email: str = typer.Option("", "--email"),
    res_id: str = typer.Option("", "--res-id"),
    yes: bool = typer.Option(False, "--yes", help="skip the confirmation prompt"),
) -> None:
    """Erase one data subject (Art. 17); suppression rows stay, the deletion log records counts."""
    if not (phone or email or res_id):
        _fail("give --phone, --email or --res-id")
    if not yes and not typer.confirm("Erase every row of this subject? This cannot be undone."):
        raise typer.Exit(code=1)
    ctx = _context()
    services = _services(ctx)

    async def go() -> dict[str, int]:
        row = await services.tenants.require(tenant)
        subject = await _subject_lookup(services, row.id, phone, email, res_id)
        if not subject.found:
            _fail("no data for this subject")
        return await services.gdpr.erase(row.id, subject, actor="cli:gdpr-erase")

    try:
        counts = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    for table, count in sorted(counts.items()):
        typer.echo(f"{table}: {count}")
    typer.echo("erased")


@gdpr_app.command("retention")
def gdpr_retention(
    tenant: str = typer.Option("", "--tenant", help="one tenant, default all"),
) -> None:
    """Run the retention pass now (the daily job does the same at 03:00 local)."""
    ctx = _context()
    services = _services(ctx)

    async def go() -> list[tuple[str, dict[str, int]]]:
        rows = [await services.tenants.require(tenant)] if tenant else await services.tenants.list()
        results = []
        for row in rows:
            results.append(
                (row.slug, await services.retention.run(row, services.tenants.profile_of(row)))
            )
        return results

    try:
        results = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    for slug, counts in results:
        summary = ", ".join(f"{k} {v}" for k, v in counts.items()) or "nothing to delete"
        typer.echo(f"{slug}: {summary}")


# ---------------------------------------------------------------------- backup
@app.command()
def backup(
    out: Path | None = typer.Option(None, "--out", help="directory (default: data dir/backups)"),
    keep_days: int = typer.Option(30, "--keep-days", min=1),
) -> None:
    """Encrypted database backup (F30), pruned by age; failures raise an operator alert."""
    from concierge.settings import default_data_dir, get_settings
    from concierge.tools import backup as backup_tool

    settings = get_settings()
    directory = out or (default_data_dir() / "backups")
    now = _utcnow()
    try:
        key = settings.backup_key()
        path = backup_tool.create_backup(settings.database_url(), directory, key, now=now)
        removed = backup_tool.prune(directory, keep_days=keep_days, now=now)
    except Exception as exc:
        ctx = _context()
        try:
            run(_record_backup_failure(ctx, str(exc)))
        finally:
            run(ctx.db.dispose())
        _fail(f"backup failed: {exc}")
    typer.echo(f"backup written to {path} ({path.stat().st_size} bytes)")
    if removed:
        typer.echo(f"pruned {len(removed)} old backup(s)")


@app.command("restore-test")
def restore_test(
    file: Path | None = typer.Option(None, "--file", help="backup file (default: newest)"),
    out: Path | None = typer.Option(None, "--out", help="backup directory when --file is absent"),
) -> None:
    """Restore a backup into a scratch database and check head and row counts."""
    from concierge.settings import default_data_dir, get_settings
    from concierge.tools import backup as backup_tool

    settings = get_settings()
    directory = out or (default_data_dir() / "backups")
    target = file or backup_tool.latest_backup(directory)
    if target is None:
        _fail("no backup found")
    try:
        report = run(backup_tool.restore_test(target, settings.backup_key()))
    except backup_tool.BackupError as exc:
        _fail(str(exc), code=2)
    typer.echo(f"restored {target} -> {report.path}")
    typer.echo(f"migration head: {report.head} (expected {report.expected_head})")
    expected = report.expected_tables or {}
    rows = [
        [name, str(expected.get(name, "-")), str(count)]
        for name, count in sorted(report.tables.items())
        if count or expected.get(name)
    ]
    typer.echo(_table(["table", "backup", "restored"], rows))
    if report.expected_tables is None:
        typer.echo("no manifest in this file (older format): row counts not compared")
    if not report.head_ok:
        _fail("restore test failed: migration head differs", code=2)
    if report.mismatches:
        _fail(
            "restore test failed: row counts differ from the backup manifest: "
            + ", ".join(f"{n} {e} -> {r}" for n, (e, r) in report.mismatches.items()),
            code=2,
        )
    typer.echo(
        f"restore test ok: {sum(report.tables.values())} rows in {len(report.tables)} tables"
    )


def _utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


async def _record_backup_failure(ctx: Any, error: str) -> None:
    from concierge.services import record_alert

    await record_alert(ctx, "backup_failed", "", {"kind": "backup", "code": error[:80]})


# ---------------------------------------------------------------------- doctor
@app.command()
def doctor(
    live: bool = typer.Option(False, "--live", help="also probe every configured integration"),
    tenant: str = typer.Option("", "--tenant", help="one tenant slug (default: all)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Offline configuration checks; --live runs the section 7 probes and records the result."""
    import json

    from concierge.tools import doctor as doctor_tool

    ctx = _context()
    services = _services(ctx)

    async def go() -> doctor_tool.DoctorReport:
        if live:
            from concierge.tools.livechecks import LiveProbes

            return await doctor_tool.run_live(
                ctx, services, tenant_slug=tenant or None, probes=LiveProbes()
            )
        return await doctor_tool.run_offline(ctx, services, tenant_slug=tenant or None)

    try:
        report = run(go())
    finally:
        run(ctx.db.dispose())
    typer.echo(json.dumps(report.to_dict(), indent=2) if as_json else report.render())
    if report.exit_code:
        raise typer.Exit(code=report.exit_code)


@wa_sim_app.command("send")
def wa_sim_send(
    text: str = typer.Option(..., "--text", help="the guest message"),
    tenant: str = typer.Option("hotel-beispiel", "--tenant"),
    base_url: str = typer.Option("http://127.0.0.1:8000", "--base-url"),
    sender: str = typer.Option("4915112345678", "--from", help="guest number without +"),
) -> None:
    """Post one signed WhatsApp text webhook to the running server (demo or test WABA)."""
    from concierge.demo.wa_sim import PayloadFactory, WaSimClient

    ctx = _context()
    services = _services(ctx)

    async def go() -> tuple[int, str]:
        try:
            row = await services.tenants.require(tenant)
            secret = await services.secrets.get(row.id, "wa_app_secret")
            phone_number_id = row.phone_number_id or ""
        finally:
            await ctx.db.dispose()
        if not secret or not phone_number_id:
            _fail(f"tenant {tenant} has no wa_app_secret or phone_number_id")
        client = WaSimClient(base_url, secret)
        try:
            response = await client.post(PayloadFactory(phone_number_id).text(sender, text))
        finally:
            await client.aclose()
        return response.status_code, response.headers.get("x-events", "?")

    try:
        status, events = run(go())
    except Exception as exc:
        _fail(f"simulator failed: {exc}")
    typer.echo(f"webhook answered {status}; events stored: {events}")
    typer.echo(f"replies appear on {base_url}/demo/wa-sim and in the inbox {base_url}/admin/inbox")
    if status != 200:
        raise typer.Exit(code=1)


# ------------------------------------------------------------ handout and pack
@handout_app.command("build")
def handout_build(
    out: Path = typer.Option(Path("docs/handout.pdf"), "--out", help="where to write the PDF"),
) -> None:
    """Build the six-page handout: how to test, hand over and customise."""
    from concierge.tools.handout import MAX_PAGES, build_handout

    path = build_handout(out)
    from pypdf import PdfReader

    pages = len(PdfReader(str(path)).pages)
    typer.echo(f"handout written to {path} ({pages} pages, limit {MAX_PAGES})")
    if pages > MAX_PAGES:
        _fail(f"the handout has {pages} pages; the limit is {MAX_PAGES}")


@compliance_app.command("pack")
def compliance_pack(
    tenant: str = typer.Option(..., "--tenant"),
    out: Path | None = typer.Option(None, "--out", help="directory (default: compliance-<slug>)"),
) -> None:
    """Sub-processors, retention, TOM checklist, disclosures and a record-of-processing draft."""
    from datetime import UTC, datetime

    from concierge.tools.compliance import build_pack_files, pack_pdf, write_pack

    ctx = _context()
    services = _services(ctx)

    async def go() -> tuple[Any, list[Any]]:
        row = await services.tenants.require(tenant)
        return services.tenants.profile_of(row), await services.legal.rows(row.id)

    try:
        profile, rows = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    today = datetime.now(UTC).date()
    files = build_pack_files(profile, rows, today=today)
    written = write_pack(
        out or Path(f"compliance-{tenant}"), files, pack_pdf(files, profile, today=today), tenant
    )
    for path in written:
        typer.echo(f"written {path}")
    typer.echo("every page is stamped 'Draft for lawyer review'; nothing here certifies compliance")


# ------------------------------------------------------------------ evaluation
@golden_app.command("run")
def golden_run(
    case: str = typer.Option("", "--case", help="substring of a case id, e.g. e01 or :ru"),
    lang: str = typer.Option("", "--lang", help="one language code"),
    as_json: bool = typer.Option(False, "--json", help="machine-readable report"),
) -> None:
    """Run the golden cases (Appendix E in six languages plus the cross-cutting set) offline."""
    import json
    import tempfile

    from concierge.eval.golden import load_cases, run_cases, summary

    cases = [
        c for c in load_cases() if (not case or case in c.id) and (not lang or c.language == lang)
    ]
    if not cases:
        _fail("no golden case matches")
    with tempfile.TemporaryDirectory(prefix="gc-golden-") as tmp:
        reports = run(run_cases(cases, Path(tmp)))
    totals = summary(reports)
    if as_json:
        typer.echo(
            json.dumps({"summary": totals, "cases": [r.to_dict() for r in reports]}, indent=2)
        )
    else:
        rows = [
            [
                r.case_id,
                r.feature,
                "pass" if r.ok else "FAIL",
                f"{r.seconds:.1f}s",
                "; ".join(r.problems)[:100],
            ]
            for r in reports
        ]
        typer.echo(_table(["case", "feature", "result", "time", "detail"], rows))
        for group, counts in sorted(totals["groups"].items()):
            typer.echo(f"{group}: {counts['passed']}/{counts['total']} passed")
        typer.echo(f"total: {totals['passed']}/{totals['total']} passed")
    if totals["failed"]:
        raise typer.Exit(code=1)


@eval_app.command("live")
def eval_live(
    case: str = typer.Option("", "--case", help="substring of a case id"),
    repeats: int = typer.Option(3, "--repeats", min=1, max=10),
    budget: float | None = typer.Option(
        None, "--budget", help="USD cap; default GC_EVAL_BUDGET_USD"
    ),
    judge: bool = typer.Option(False, "--judge", help="ask an advisory model judge per reply"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Golden scenarios against the real model (needs ANTHROPIC_API_KEY); never a release gate."""
    import json
    import tempfile

    from concierge.eval.golden import load_cases
    from concierge.eval.live import LiveEvalError, require_key, run_live
    from concierge.settings import get_settings

    settings = get_settings()
    try:
        require_key(settings)
    except LiveEvalError as exc:
        _fail(str(exc), code=2)
    cases = [c for c in load_cases() if not case or case in c.id]
    if not cases:
        _fail("no golden case matches")
    with tempfile.TemporaryDirectory(prefix="gc-live-") as tmp:
        try:
            report = run(
                run_live(
                    settings,
                    cases=cases,
                    work_dir=Path(tmp),
                    repeats=repeats,
                    budget_usd=budget,
                    judge=judge,
                )
            )
        except LiveEvalError as exc:
            _fail(str(exc), code=2)
    if as_json:
        typer.echo(json.dumps(report.to_dict(), indent=2))
    else:
        rows = [
            [
                r.case_id,
                r.effort,
                str(r.repeat),
                "pass" if r.ok else "FAIL",
                f"{r.cost_usd:.4f}",
                "; ".join(r.problems)[:80],
            ]
            for r in report.runs
        ]
        typer.echo(_table(["case", "effort", "run", "result", "usd", "detail"], rows))
        for key, values in report.percentiles().items():
            typer.echo(f"{key}: median {values['median_ms']} ms, p95 {values['p95_ms']} ms")
        typer.echo(
            f"spent {report.total_usd:.4f} USD of {report.budget_usd} USD"
            + (" (stopped by budget)" if report.stopped_by_budget else "")
        )
        typer.echo(
            f"cache read on repeat: {report.cache_read_ok}; three-turn history: {report.history_ok}"
        )
    if not report.ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------- voice
@voice_app.command("provision")
def voice_provision(
    tenant: str,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the provider payloads without calling the API"
    ),
) -> None:
    """Create the voice agent and import the number at the tenant's provider (F26)."""
    import json

    from concierge.channels.voice import retell, twilio_cr
    from concierge.channels.voice.port import ProvisionResult
    from concierge.tenants.profile import TenantProfile
    from concierge.tenants.service import TenantError

    ctx = _context()
    services = _services(ctx)
    settings = ctx.settings
    public = settings.public_base_url()

    async def go() -> ProvisionResult:
        row = await services.tenants.require(tenant)
        profile = services.tenants.profile_of(row)
        provider = profile.voice.provider
        if provider == "retell":
            if not profile.voice.number or not profile.voice.voice_id:
                _fail("voice.number and voice.voice_id are required in the tenant profile")
            ws_base = public.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
            socket_secret = await _ensure_voice_socket_secret(services, row.id, dry_run=dry_run)
            result = await retell.provision(
                profile,
                api_key=settings.retell_api_key.get_secret_value(),
                websocket_url=f"{ws_base}/voice/retell/llm/{row.public_key}/{socket_secret}/{{call_id}}",
                webhook_url=f"{public}/voice/retell/webhook",
                sip_user=await services.secrets.get(row.id, "voice_sip_user") or "",
                sip_password=await services.secrets.get(row.id, "voice_sip_password") or "",
                dry_run=dry_run,
            )
        elif provider == "twilio_cr":
            if not profile.voice.number:
                _fail("voice.number is required in the tenant profile")
            await _ensure_voice_socket_secret(services, row.id, dry_run=dry_run)
            result = await twilio_cr.provision(
                profile,
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token.get_secret_value(),
                voice_url=f"{public}/voice/twilio/incoming",
                status_url=f"{public}/voice/twilio/status",
                dry_run=dry_run,
            )
        else:
            _fail(f"voice.provider is {provider!r}; nothing to provision")
        if result.applied and result.agent_id:
            data = profile.model_dump(mode="json")
            data["voice"]["agent_id"] = result.agent_id
            await services.tenants.update_profile(
                row.slug, TenantProfile.model_validate(data), actor="cli:voice-provision"
            )
        return result

    try:
        result = run(go())
    except TenantError as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    for payload in result.payloads:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    typer.echo("applied" if result.applied else "dry run: nothing sent to the provider")
    if result.agent_id:
        typer.echo(f"agent id: {result.agent_id}")
    typer.echo("human steps:")
    for step in result.human_steps:
        typer.echo(f"  - {step}")


# ---------------------------------------------------------------------- packs
@pack_app.command("export")
def pack_export(
    tenant: str,
    out: Path = typer.Option(..., "--out", help="directory (or .zip path) to write"),
) -> None:
    """Write a tenant's configuration pack (never secrets)."""
    import tempfile

    from concierge.tenants.packs import pack_to_zip

    ctx = _context()
    services = _services(ctx)

    async def go(directory: Path) -> list[str]:
        row = await services.tenants.require(tenant)
        return await services.packs.export(row, directory)

    try:
        if out.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory(prefix="concierge-pack-") as tmp:
                written = run(go(Path(tmp)))
                out.write_bytes(pack_to_zip(Path(tmp)))
        else:
            written = run(go(out))
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    for rel in written:
        typer.echo(rel)
    typer.echo(f"pack written to {out}")


@pack_app.command("validate")
def pack_validate(source: Path) -> None:
    """Validate a pack directory or zip; errors name the field path."""
    from concierge.tenants.packs import open_pack, validate_pack

    try:
        errors = validate_pack(open_pack(source))
    except ValueError as exc:
        _fail(str(exc))
    for error in errors:
        typer.echo(error)
    if errors:
        raise typer.Exit(code=1)
    typer.echo("pack ok")


@pack_app.command("diff")
def pack_diff(source: Path) -> None:
    """Show what an import of the pack would change."""
    from concierge.tenants.packs import open_pack

    ctx = _context()
    services = _services(ctx)
    try:
        report = run(
            services.packs.import_pack(open_pack(source), dry_run=True, actor="cli:pack-diff")
        )
    except ValueError as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    for error in report.errors:
        typer.echo(f"error: {error}")
    for change in report.changes:
        typer.echo(change)
    if report.errors:
        raise typer.Exit(code=1)
    if not report.changes:
        typer.echo("no changes")


@pack_app.command("import")
def pack_import(
    source: Path,
    apply: bool = typer.Option(False, "--apply", help="write the changes (default: dry run)"),
) -> None:
    """Import a pack: dry run by default, `--apply` writes and audits."""
    from concierge.tenants.packs import open_pack

    ctx = _context()
    services = _services(ctx)
    try:
        report = run(
            services.packs.import_pack(
                open_pack(source), dry_run=not apply, actor="cli:pack-import"
            )
        )
    except ValueError as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    for error in report.errors:
        typer.echo(f"error: {error}")
    for change in report.changes:
        typer.echo(change)
    if report.errors:
        raise typer.Exit(code=1)
    typer.echo("applied" if report.applied else "dry run: nothing written (use --apply)")


async def _ensure_voice_socket_secret(services: Any, tenant_id: str, *, dry_run: bool) -> str:
    """The per-tenant secret in the provider's WebSocket path; created once at provisioning."""
    from concierge.channels.voice.port import VOICE_SOCKET_SECRET

    existing = await services.secrets.get(tenant_id, VOICE_SOCKET_SECRET)
    if existing:
        return str(existing)
    value = _pysecrets.token_urlsafe(24)
    if dry_run:
        return "<socket-secret-created-at-provisioning>"
    await services.secrets.set(tenant_id, VOICE_SOCKET_SECRET, value, actor="cli:voice-provision")
    return value


def _reachable_host(host: str) -> str:
    """The host the process can reach itself on (an unspecified bind maps to loopback)."""
    import ipaddress

    try:
        if ipaddress.ip_address(host).is_unspecified:
            return "127.0.0.1"
    except ValueError:
        pass
    return host


def _serve(*, demo: bool, host: str, port: int) -> None:
    import uvicorn

    from concierge.db.migrate import upgrade
    from concierge.db.session import Database
    from concierge.demo.seed import DEMO_USERS
    from concierge.runtime import build_runtime
    from concierge.settings import get_settings

    settings = get_settings()
    if demo and settings.is_production:
        _fail("the demo is refused when GC_ENV=production")

    async def _migrate() -> None:
        database = Database(settings.database_url())
        try:
            await upgrade(database.engine)
        finally:
            await database.dispose()

    run(_migrate())
    bind_url = f"http://{_reachable_host(host)}:{port}"
    runtime = build_runtime(settings, demo=demo, bind_url=bind_url)
    typer.echo(f"WITALCO Guest Concierge {__version__} on {bind_url}")
    if demo:
        typer.echo(
            "demo mode: Hotel Beispiel is seeded on startup; the mock Graph server answers sends"
        )
        typer.echo(f"  widget test page: {bind_url}/demo/widget")
        typer.echo(f"  WhatsApp simulator: {bind_url}/demo/wa-sim")
        typer.echo(f"  hotel admin: {bind_url}/admin/login")
        typer.echo(f"  operator console: {bind_url}/operator/")
        typer.echo("  fixture accounts (demo only, not credentials):")
        for email, _role, password in DEMO_USERS:
            typer.echo(f"    {email} / {password}")
        if not runtime.llm_configured:
            typer.echo(
                "  no ANTHROPIC_API_KEY: the scripted responder answers, not the language model"
            )
    uvicorn.run(
        runtime.app, host=host, port=port, proxy_headers=True, access_log=False, log_level="warning"
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Start the service (migrates first, one process with the job worker)."""
    _serve(demo=False, host=host, port=port)


@demo_app.callback(invoke_without_command=True)
def demo(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Seed the fictional Hotel Beispiel and start the service in demo mode."""
    if ctx.invoked_subcommand is not None:
        return
    _serve(demo=True, host=host, port=port)


@demo_app.command("voice-sim")
def demo_voice_sim(
    say: list[str] = typer.Option(..., "--say", help="caller utterances, in order"),
    tenant: str = typer.Option("hotel-beispiel", "--tenant"),
    base_url: str = typer.Option("http://127.0.0.1:8000", "--base-url"),
    from_number: str = typer.Option("+4915120000009", "--from"),
) -> None:
    """Simulate a Retell call against the running server and print the spoken lines."""
    from concierge.channels.voice.port import VOICE_SOCKET_SECRET
    from concierge.demo.voice_sim import live_call

    ctx = _context()

    async def go() -> list[tuple[str, str]]:
        try:
            services = _services(ctx)
            row = await services.tenants.require(tenant)
            public_key = row.public_key
            socket_secret = await services.secrets.get(row.id, VOICE_SOCKET_SECRET) or ""
        finally:
            await ctx.db.dispose()
        if not socket_secret:
            _fail("no voice socket secret for the tenant: run `concierge voice provision`")
        return await live_call(
            base_url, public_key, socket_secret, list(say), from_number=from_number
        )

    try:
        lines = run(go())
    except Exception as exc:
        _fail(f"voice simulation failed: {exc}")
    for role, text in lines:
        typer.echo(f"{'caller' if role == 'user' else 'agent '}: {text}")


@demo_app.command("advance")
def demo_advance(
    days: int = typer.Option(..., "--days", min=0, max=60, help="days to warp forward"),
    at: str = typer.Option("10:00", "--at", help="local wall time of the warped clock (HH:MM)"),
) -> None:
    """Run every journey step due by the warped clock against the running demo server."""
    from datetime import timedelta

    from concierge.channels.whatsapp.graph import GraphClient
    from concierge.clock import FakeClock, SystemClock, local_date, local_to_utc, parse_hhmm
    from concierge.context import AppContext
    from concierge.db.session import Database
    from concierge.security.crypto import Keyring
    from concierge.services import build_services
    from concierge.settings import get_settings

    settings = get_settings()
    if settings.is_production:
        _fail("the demo clock is refused when GC_ENV=production")
    try:
        wall = parse_hhmm(at)
    except ValueError as exc:
        _fail(str(exc))
    tz = settings.gc_tz_default
    day = local_date(SystemClock().now(), tz) + timedelta(days=days)
    target = local_to_utc(day, wall, tz)
    base = settings.public_base_url()

    async def _go() -> tuple[int, list[list[str]]]:
        ctx = AppContext(
            settings=settings,
            db=Database(settings.database_url()),
            keyring=Keyring.from_settings(settings),
            clock=FakeClock(target),
        )
        services = build_services(
            ctx,
            llm=None,
            wa_port=GraphClient(
                version=settings.gc_wa_graph_version, base_url=f"{base}/mock-graph"
            ),
            demo=True,
        )
        try:
            processed = await services.worker.drain()
            rows = [[o.step, o.outcome, o.detail[:60]] for o in services.journeys.outcomes]
            counts = await services.outbox.counts()
            rows.append(["outbox", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), ""])
            return processed, rows
        finally:
            await services.close()

    processed, rows = run(_go())
    typer.echo(
        f"demo clock warped to {day} {at} {tz} ({target:%Y-%m-%d %H:%M} UTC); "
        f"{processed} jobs processed"
    )
    typer.echo(_table(["step", "outcome", "detail"], rows) if rows else "(nothing was due)")
    typer.echo(f"sends are listed on {base}/demo/wa-sim while `concierge demo` is running")


# ------------------------------------------------------------------ prices
prices_app = typer.Typer(no_args_is_help=True, help="Price tables (F24)")
app.add_typer(prices_app, name="prices")
model_prices_app = typer.Typer(
    no_args_is_help=True, help="Model prices per million tokens (USD), dated rows"
)
prices_app.add_typer(model_prices_app, name="model")


@model_prices_app.command("set")
def prices_model_set(
    model: str = typer.Option(..., "--model", help="model id, for example claude-sonnet-5-5"),
    effective_from: str = typer.Option(..., "--from", help="first day the row applies, YYYY-MM-DD"),
    input_usd: float = typer.Option(..., "--input", help="USD per million input tokens"),
    output_usd: float = typer.Option(..., "--output", help="USD per million output tokens"),
    cache_write_usd: float | None = typer.Option(
        None, "--cache-write", help="USD per million cache-write tokens (default 1.25 x input)"
    ),
    cache_read_usd: float | None = typer.Option(
        None, "--cache-read", help="USD per million cache-read tokens (default 0.1 x input)"
    ),
    source: str = typer.Option("", "--source", help="where the figures come from (URL)"),
) -> None:
    """Insert or replace a dated model price row; usage from that day is priced with it."""
    from concierge.metering.service import MeteringService

    try:
        day = date.fromisoformat(effective_from)
    except ValueError:
        _fail("--from expects YYYY-MM-DD")
    if min(input_usd, output_usd) < 0:
        _fail("prices are USD amounts of zero or more")
    ctx = _context()
    try:
        row = run(
            MeteringService(ctx.db, ctx.clock).set_model_price(
                model,
                day,
                input_usd=input_usd,
                output_usd=output_usd,
                cache_write_usd=cache_write_usd,
                cache_read_usd=cache_read_usd,
                source_url=source,
                actor="cli:prices",
            )
        )
    finally:
        run(ctx.db.dispose())
    typer.echo(f"model price stored: {row.model} from {row.effective_from.isoformat()}")


@model_prices_app.command("list")
def prices_model_list() -> None:
    """The configured model price rows and the built-in defaults they override."""
    from concierge.db.models import ModelPrice
    from concierge.metering.prices import DEFAULT_MODEL_PRICES

    ctx = _context()

    async def go() -> list[list[str]]:
        from sqlalchemy import select

        async with ctx.db.scope() as session:
            rows = (
                await session.execute(
                    select(ModelPrice).order_by(ModelPrice.model, ModelPrice.effective_from)
                )
            ).scalars()
            return [
                [
                    r.model,
                    r.effective_from.isoformat(),
                    f"{r.input_micro_usd_per_mtok / 1_000_000:.2f}",
                    f"{r.output_micro_usd_per_mtok / 1_000_000:.2f}",
                    "configured",
                ]
                for r in rows
            ]

    try:
        rows = run(go())
    finally:
        run(ctx.db.dispose())
    rows += [
        [
            d.model,
            d.effective_from.isoformat(),
            f"{d.input_usd_per_mtok:.2f}",
            f"{d.output_usd_per_mtok:.2f}",
            "default",
        ]
        for d in DEFAULT_MODEL_PRICES
    ]
    typer.echo(_table(["model", "from", "input USD/M", "output USD/M", "source"], rows))


# ------------------------------------------------------------------ kb intake
kb_app = typer.Typer(
    no_args_is_help=True, help="Knowledge-base intake: pages, files, translations, coverage"
)
app.add_typer(kb_app, name="kb")
questionnaire_app = typer.Typer(
    no_args_is_help=True, help="The 85-item questionnaire: items, export, import, apply"
)
kb_app.add_typer(questionnaire_app, name="questionnaire")


def _echo_proposals(proposals: list[Any]) -> None:
    for proposal in proposals:
        flag = " (truncated)" if proposal.truncated else ""
        typer.echo(f"{proposal.key}/{proposal.lang}: {len(proposal.text)} characters{flag}")


@kb_app.command("import-url")
def kb_import_url(
    url: str,
    tenant: str = typer.Option(..., "--tenant"),
    lang: str = typer.Option(
        "", "--lang", help="language of the pages (default: the tenant's default language)"
    ),
    max_pages: int = typer.Option(
        5, "--max-pages", min=1, max=5, help="pages to fetch, same site only"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the proposals without writing drafts"
    ),
) -> None:
    """Propose draft entries from the hotel's public pages (SSRF guard, five pages of 1 MB)."""
    from concierge.kb.intake import MAX_PAGE_BYTES, import_url, store_proposals
    from concierge.security.ssrf import SafeFetcher, SSRFError

    ctx = _context()
    services = _services(ctx)

    async def go() -> tuple[Any, int]:
        row = await services.tenants.require(tenant)
        language = lang or services.tenants.profile_of(row).default_language
        report = await import_url(
            SafeFetcher(max_bytes=MAX_PAGE_BYTES), url, lang=language, max_pages=max_pages
        )
        written = 0
        if not dry_run:
            written = await store_proposals(
                services.kb, row.id, report.proposals, actor="cli:kb-import-url"
            )
        return report, written

    try:
        report, written = run(go())
    except SSRFError as exc:
        _fail(f"refused: {exc}")
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    for page in report.pages:
        detail = f", {page.sections} sections" if page.status == "ok" else ""
        typer.echo(f"{page.url}: {page.status}{detail}")
    _echo_proposals(report.proposals)
    outcome = "dry run: nothing written" if dry_run else f"{written} drafts written"
    typer.echo(f"{len(report.proposals)} proposals; {outcome}")
    if not any(page.status == "ok" for page in report.pages):
        raise typer.Exit(code=1)


@kb_app.command("import-file")
def kb_import_file(
    path: Path,
    tenant: str = typer.Option(..., "--tenant"),
    lang: str = typer.Option("", "--lang", help="language of the text (default: tenant default)"),
    key: str = typer.Option("", "--key", help="store the whole file as one draft under this key"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Draft entries from a text, Markdown or PDF file; the text is data, never instructions."""
    from concierge.kb.intake import import_file, store_proposals

    if not path.is_file():
        _fail(f"{path} is not a file")
    ctx = _context()
    services = _services(ctx)

    async def go() -> tuple[list[Any], int]:
        row = await services.tenants.require(tenant)
        language = lang or services.tenants.profile_of(row).default_language
        proposals = import_file(path, lang=language, key=key or None)
        written = 0
        if not dry_run:
            written = await store_proposals(
                services.kb, row.id, proposals, actor="cli:kb-import-file"
            )
        return proposals, written

    try:
        proposals, written = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    _echo_proposals(proposals)
    outcome = "dry run: nothing written" if dry_run else f"{written} drafts written"
    typer.echo(f"{len(proposals)} proposals; {outcome}")


@kb_app.command("translate")
def kb_translate(
    tenant: str = typer.Option(..., "--tenant"),
    to: str = typer.Option(..., "--to", help="target languages, comma-separated"),
    source_lang: str = typer.Option("", "--from", help="source language (default: tenant default)"),
    key: str = typer.Option("", "--key", help="only this key or block"),
    dry_run: bool = typer.Option(False, "--dry-run", help="list the entries; no model call"),
) -> None:
    """Machine-translation drafts (draft_mt) of the approved entries through the model."""
    from concierge.kb.intake import LANGUAGE_NAMES, translate_entries
    from concierge.runtime import build_llm

    targets = [t.strip() for t in to.split(",") if t.strip()]
    if not targets or any(t not in LANGUAGE_NAMES for t in targets):
        _fail("--to expects languages among " + ", ".join(LANGUAGE_NAMES))
    ctx = _context()
    llm = None if dry_run else build_llm(ctx.settings)
    if llm is None and not dry_run:
        run(ctx.db.dispose())
        _fail("no language model configured: set GC_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY")
    services = _services(ctx)
    model = ctx.settings.gc_llm_model_cheap or ctx.settings.gc_llm_model_primary

    async def go() -> tuple[list[Any], Any]:
        row = await services.tenants.require(tenant)
        profile = services.tenants.profile_of(row)
        disabled = [t for t in targets if t not in profile.languages]
        if disabled:
            raise ValueError(f"language {disabled[0]} is not enabled for {tenant}")
        source = source_lang or profile.default_language
        seen: set[str] = set()
        entries = []
        for entry in await services.kb.entries(row.id, status="approved", lang=source):
            if entry.key in seen or (
                key and entry.key != key and not entry.key.startswith(key + ".")
            ):
                continue
            seen.add(entry.key)
            entries.append(entry)
        if llm is None:
            return entries, None
        report = await translate_entries(llm, model, entries, targets)
        written = 0
        if report.rows:
            written = await services.kb.bulk_upsert(row.id, report.rows, actor="cli:kb-translate")
        return entries, (report, written)

    try:
        entries, result = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    if result is None:
        for entry in entries:
            typer.echo(f"{entry.key}/{entry.lang} -> {', '.join(targets)}")
        typer.echo(f"dry run: {len(entries)} entries x {len(targets)} languages, nothing sent")
        return
    report, written = result
    for failure in report.failures:
        typer.echo(f"failed: {failure}")
    typer.echo(
        f"{written} draft_mt rows written from {len(entries)} entries; approve them in the admin"
    )
    if report.failures:
        raise typer.Exit(code=1)


@kb_app.command("coverage")
def kb_coverage(tenant: str = typer.Option(..., "--tenant")) -> None:
    """Coverage of the go-live keys per language; exit 1 while a key lacks approval."""
    from concierge.kb.intake import coverage_rows

    ctx = _context()
    services = _services(ctx)

    async def go() -> tuple[list[str], dict[str, dict[str, str]]]:
        row = await services.tenants.require(tenant)
        languages = [str(code) for code in services.tenants.profile_of(row).languages]
        return languages, await services.kb.coverage(row.id, languages)

    try:
        languages, coverage = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    rows, complete = coverage_rows(coverage, languages)
    typer.echo(_table(["key", *languages], rows))
    typer.echo(f"{complete} of {len(rows)} required keys approved in every language")
    if complete < len(rows):
        raise typer.Exit(code=1)


@questionnaire_app.command("items")
def questionnaire_items(lang: str = typer.Option("en", "--lang", help="de or en")) -> None:
    """The 85 items with kind, target and required flag."""
    from concierge.kb.questionnaire import ITEMS

    rows = [
        [
            item.key,
            item.kind,
            item.target or f"tenant.yaml {item.field}",
            "yes" if item.required else "",
            item.label(lang),
        ]
        for item in ITEMS
    ]
    typer.echo(_table(["key", "kind", "target", "required", "label"], rows))


@questionnaire_app.command("export")
def questionnaire_export(
    tenant: str = typer.Option(..., "--tenant"),
    out: Path | None = typer.Option(None, "--out", help="write the YAML here (default: print)"),
) -> None:
    """The stored answers as questionnaire_answers.yaml."""
    from concierge.kb.questionnaire import dump_answers

    ctx = _context()
    services = _services(ctx)

    async def go() -> tuple[str, int]:
        row = await services.tenants.require(tenant)
        answers = await services.questionnaire.answers(row.id)
        return dump_answers(row.slug, answers), len(answers)

    try:
        text, count = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    if out is None:
        typer.echo(text)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {out} ({count} answers)")


@questionnaire_app.command("import")
def questionnaire_import(
    path: Path,
    tenant: str = typer.Option(..., "--tenant"),
    replace: bool = typer.Option(False, "--replace", help="remove stored answers not in the file"),
) -> None:
    """Store answers from a YAML file; errors name the item."""
    from concierge.kb.questionnaire import load_answers, missing_required

    answers, errors = load_answers(path)
    if errors:
        for error in errors:
            typer.echo(f"error: {path.name}: {error}")
        raise typer.Exit(code=1)
    ctx = _context()
    services = _services(ctx)

    async def go() -> tuple[int, int]:
        row = await services.tenants.require(tenant)
        saved = await services.questionnaire.save(
            row.id, answers, actor="cli:questionnaire-import", replace=replace
        )
        stored = await services.questionnaire.answers(row.id)
        return saved, len(missing_required(stored))

    try:
        saved, open_items = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    typer.echo(f"{saved} answers stored; {open_items} required items still open")


@questionnaire_app.command("apply")
def questionnaire_apply(
    tenant: str = typer.Option(..., "--tenant"),
    lang: str = typer.Option(
        "", "--lang", help="language of the answers (default: tenant default)"
    ),
) -> None:
    """Write one draft KB entry per answered target key."""
    ctx = _context()
    services = _services(ctx)

    async def go() -> int:
        row = await services.tenants.require(tenant)
        language = lang or services.tenants.profile_of(row).default_language
        return await services.questionnaire.apply(
            row.id, services.kb, lang=language, actor="cli:questionnaire-apply"
        )

    try:
        count = run(go())
    except Exception as exc:
        _fail(str(exc))
    finally:
        run(ctx.db.dispose())
    typer.echo(f"{count} draft entries written; approve them in the admin (Knowledge base)")


def main() -> None:
    _utf8_streams()
    app()


if __name__ == "__main__":
    main()
