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
import sys
from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from a CLI command (no event loop policy changes, 6.8)."""
    return asyncio.run(coro)


def _utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _fail(message: str, code: int = 1) -> None:
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
        return

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
        return
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
        return
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
        return
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
        return
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
        return
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
        return
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
        return
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    if out is not None:
        out.write_text(payload, encoding="utf-8")
        typer.echo(f"{len(rows)} rows written to {out}")
    else:
        typer.echo(payload)


# --------------------------------------------------------------------- serving
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
        return
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


def main() -> None:
    _utf8_streams()
    app()


if __name__ == "__main__":
    main()
