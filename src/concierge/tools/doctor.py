"""Doctor (F28): offline configuration checks and the live integration run.

Implements: F28, F21, F24, F32

Every check ends as pass, warn or fail with the exact variable or profile field, the
fix and the documentation section; the exit code is 0, 1 or 2. Secrets never appear
in the output: the report names variables and statuses only. ``--live`` runs the
probes of ``tools/livechecks.py`` (section 7) and records one audit row per tenant
(action ``doctor.live``) that the activation gate reads through
``live_ok_for_tenant``, so a tenant never goes live without a recorded, passing run.
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from concierge import audit
from concierge.context import AppContext
from concierge.db.migrate import head_state
from concierge.db.models import AuditLog, Tenant
from concierge.metering.prices import META_PRICING_URL
from concierge.reports.pdf import FONTS_DIR
from concierge.settings import Settings, SettingsError, default_data_dir
from concierge.tenants.profile import TenantProfile
from concierge.tools.backup import latest_backup, sqlite_path

LIVE_ACTION = "doctor.live"
Status = Literal["pass", "warn", "fail"]
PROMPT_MIN_TOKENS = 512
"""Below this the prompt cache does nothing (8.2)."""
STALE_PRICE_DAYS = 180
BACKUP_MAX_AGE = timedelta(days=2)
DISK_FAIL_BYTES = 200 * 1024 * 1024
DISK_WARN_BYTES = 1024 * 1024 * 1024
FONT_FILES = ("Inter-Regular.ttf", "Inter-Bold.ttf", "Syne-Regular.ttf", "Syne-Bold.ttf")
SYNCED_FOLDER_MARKERS = ("onedrive", "dropbox", "icloud", "google drive", "googledrive")
CHANNELS = ("whatsapp", "widget", "voice")
DOC_OPERATIONS = "docs/OPERATIONS.md"
DOC_INTEGRATIONS = "docs/INTEGRATIONS.md"
DOC_CUSTOMISATION = "docs/CUSTOMISATION.md"
DOC_SECURITY = "docs/SECURITY.md"


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""
    doc: str = ""
    tenant: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
            "doc": self.doc,
            "tenant": self.tenant,
        }


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    live: bool = False
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def exit_code(self) -> int:
        if self.failed:
            return 2
        return 1 if self.warned else 0

    def render(self) -> str:
        width = max([len(c.name) for c in self.checks] + [5])
        lines = [f"{'check':<{width}}  status  detail"]
        for c in self.checks:
            where = f"[{c.tenant}] " if c.tenant else ""
            lines.append(f"{c.name:<{width}}  {c.status:<6}  {where}{c.detail}")
            if c.fix and c.status != "pass":
                lines.append(
                    f"{'':<{width}}          fix: {c.fix}" + (f" ({c.doc})" if c.doc else "")
                )
        passed = sum(1 for c in self.checks if c.status == "pass")
        lines.append(
            f"{passed} pass, {len(self.warned)} warn, {len(self.failed)} fail"
            + (" (live)" if self.live else " (offline)")
        )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "exit_code": self.exit_code,
            "checks": [c.to_dict() for c in self.checks],
        }


def estimate_tokens(text: str) -> int:
    """A rough count (about four characters per token) used when no model is configured."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------- offline checks
def check_environment(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    checks.append(
        Check("config.environment", "pass", f"GC_ENV={settings.gc_env}", doc=DOC_OPERATIONS)
    )
    try:
        url = settings.public_base_url()
    except SettingsError as exc:
        checks.append(
            Check(
                "config.public_url",
                "fail",
                str(exc),
                fix="set GC_PUBLIC_BASE_URL to the public https origin (webhooks, widget, links)",
                doc=DOC_OPERATIONS,
            )
        )
    else:
        local = url.startswith(("http://127.0.0.1", "http://localhost"))
        if settings.is_production and (not url.startswith("https://") or local):
            checks.append(
                Check(
                    "config.public_url",
                    "fail",
                    "GC_PUBLIC_BASE_URL is not a public https origin",
                    fix="set GC_PUBLIC_BASE_URL=https://<your host> behind Caddy or another TLS proxy",
                    doc=DOC_OPERATIONS,
                )
            )
        else:
            note = " (local; webhooks need a public URL or a tunnel)" if local else ""
            checks.append(Check("config.public_url", "pass", f"GC_PUBLIC_BASE_URL={url}{note}"))
    database_url = settings.database_url()
    path = sqlite_path(database_url)
    scheme = database_url.split(":", 1)[0]
    if path is not None and any(
        marker in path.as_posix().lower() for marker in SYNCED_FOLDER_MARKERS
    ):
        checks.append(
            Check(
                "config.database",
                "warn",
                "the SQLite file lies in a synced folder (OneDrive, Dropbox, iCloud, Google Drive)",
                fix="set GC_DATABASE_URL to a path outside synced folders; syncing corrupts SQLite",
                doc=DOC_OPERATIONS,
            )
        )
    else:
        checks.append(Check("config.database", "pass", f"GC_DATABASE_URL scheme {scheme}"))
    if not settings.gc_operator_email:
        checks.append(
            Check(
                "config.operator_email",
                "warn",
                "GC_OPERATOR_EMAIL is empty: operator alerts and test mails have no recipient",
                fix="set GC_OPERATOR_EMAIL",
                doc=DOC_OPERATIONS,
            )
        )
    else:
        checks.append(Check("config.operator_email", "pass", "GC_OPERATOR_EMAIL set"))
    if not settings.gc_metrics_token.get_secret_value():
        checks.append(
            Check(
                "config.metrics_token",
                "warn",
                "GC_METRICS_TOKEN is empty: /metrics answers 503",
                fix="set GC_METRICS_TOKEN (concierge setup generates one)",
                doc=DOC_OPERATIONS,
            )
        )
    else:
        checks.append(Check("config.metrics_token", "pass", "GC_METRICS_TOKEN set"))
    checks.append(check_email_config(settings))
    checks.append(check_llm_config(settings))
    return checks


def check_email_config(settings: Settings) -> Check:
    provider = settings.gc_email_provider
    missing: list[str] = []
    if provider == "resend" and not settings.resend_api_key.get_secret_value():
        missing.append("RESEND_API_KEY")
    if provider == "smtp" and not settings.gc_smtp_host:
        missing.append("GC_SMTP_HOST")
    if provider != "none" and not settings.gc_email_from:
        missing.append("GC_EMAIL_FROM")
    if provider == "none":
        return Check(
            "config.email",
            "warn",
            "GC_EMAIL_PROVIDER=none: no staff e-mails, no e-mail fallback",
            fix="set GC_EMAIL_PROVIDER=resend with RESEND_API_KEY and GC_EMAIL_FROM, or smtp",
            doc=DOC_INTEGRATIONS,
        )
    if missing:
        return Check(
            "config.email",
            "warn",
            f"e-mail not configured: {', '.join(missing)} missing",
            fix=f"set {', '.join(missing)}",
            doc=DOC_INTEGRATIONS,
        )
    return Check("config.email", "pass", f"GC_EMAIL_PROVIDER={provider}")


def check_llm_config(settings: Settings) -> Check:
    if settings.gc_llm_provider == "none":
        return Check(
            "config.llm",
            "warn",
            "GC_LLM_PROVIDER=none: the scripted responder answers, not the language model",
            fix="set GC_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY",
            doc=DOC_INTEGRATIONS,
        )
    if not settings.anthropic_api_key.get_secret_value():
        return Check(
            "config.llm",
            "fail",
            "GC_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty",
            fix="set ANTHROPIC_API_KEY (workspace key with a spend limit)",
            doc=DOC_INTEGRATIONS,
        )
    return Check(
        "config.llm",
        "pass",
        f"anthropic: {settings.gc_llm_model_primary}, failover {settings.gc_llm_model_failover}",
    )


def check_keys(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    try:
        keys = settings.master_keys()
        active = settings.gc_active_key_id
        if active not in keys:
            raise SettingsError("GC_ACTIVE_KEY_ID names no key in GC_MASTER_KEYS")
        checks.append(Check("keys.master", "pass", f"{len(keys)} master key(s), active {active}"))
    except Exception as exc:
        checks.append(
            Check(
                "keys.master",
                "fail",
                f"GC_MASTER_KEYS or GC_ACTIVE_KEY_ID invalid: {exc}",
                fix="run concierge setup, or set GC_MASTER_KEYS to a JSON map of key id to a 32-byte base64 key and GC_ACTIVE_KEY_ID",
                doc=DOC_SECURITY,
            )
        )
    signing = settings.gc_signing_key.get_secret_value()
    if len(signing) < 32:
        checks.append(
            Check(
                "keys.signing",
                "fail",
                "GC_SIGNING_KEY is missing or shorter than 32 characters",
                fix="set GC_SIGNING_KEY (concierge setup generates one)",
                doc=DOC_SECURITY,
            )
        )
    else:
        checks.append(Check("keys.signing", "pass", "GC_SIGNING_KEY set"))
    try:
        settings.backup_key()
        checks.append(Check("keys.backup", "pass", "GC_BACKUP_KEY set"))
    except Exception as exc:
        checks.append(
            Check(
                "keys.backup",
                "fail",
                f"GC_BACKUP_KEY invalid: {exc}",
                fix="set GC_BACKUP_KEY to a 32-byte base64 key (concierge setup generates one)",
                doc=DOC_SECURITY,
            )
        )
    return checks


async def check_migrations(ctx: AppContext) -> Check:
    try:
        current, head = await head_state(ctx.db.engine)
    except Exception as exc:
        return Check(
            "database.connection",
            "fail",
            f"cannot open the database: {type(exc).__name__}",
            fix="check GC_DATABASE_URL and that the database server is reachable",
            doc=DOC_OPERATIONS,
        )
    if current != head:
        return Check(
            "database.migrations",
            "fail",
            f"database at {current or 'no revision'}, code expects {head}",
            fix="run concierge db upgrade",
            doc=DOC_OPERATIONS,
        )
    return Check("database.migrations", "pass", f"at head {head}")


def check_fonts(fonts_dir: Path = FONTS_DIR) -> Check:
    missing = [name for name in FONT_FILES if not (fonts_dir / name).is_file()]
    if missing:
        return Check(
            "fonts",
            "fail",
            f"missing font files: {', '.join(missing)}",
            fix=f"restore the bundled fonts in {fonts_dir} (a broken install; reinstall the package)",
            doc=DOC_OPERATIONS,
        )
    return Check("fonts", "pass", "Inter and Syne present (Latin, Cyrillic, Turkish glyphs)")


def check_timezone(name: str, *, field_name: str) -> Check:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return Check(
            "tzdata",
            "fail",
            f"time zone {name!r} unknown ({field_name})",
            fix="install the tzdata package and use an IANA name such as Europe/Berlin",
            doc=DOC_OPERATIONS,
        )
    return Check("tzdata", "pass", f"{name} loads ({field_name})")


def check_price_tables(newest: tuple[date | None, date | None], today: date) -> Check:
    message_date, model_date = newest
    dates = [d for d in (message_date, model_date) if d is not None]
    oldest = min(dates) if dates else None
    reminder = f"re-check Meta's rate card before every price change: {META_PRICING_URL}"
    edited = f"last edit {oldest.isoformat()}" if oldest else "no dated row yet"
    if oldest is None or (today - oldest).days > STALE_PRICE_DAYS:
        return Check(
            "prices.age",
            "warn",
            f"price tables older than {STALE_PRICE_DAYS} days ({edited}); {reminder}",
            fix="review the rates in the operator console (Prices) and add a dated row",
            doc=DOC_OPERATIONS,
        )
    return Check("prices.age", "pass", f"{edited}; {reminder}")


def check_disk(directory: Path, usage: Callable[[Path], Any] = shutil.disk_usage) -> Check:
    try:
        free = int(usage(directory).free)
    except OSError as exc:
        return Check(
            "disk",
            "fail",
            f"cannot read free space of {directory}: {exc}",
            fix="check GC_DATABASE_URL and the data directory permissions",
            doc=DOC_OPERATIONS,
        )
    if free < DISK_FAIL_BYTES:
        return Check(
            "disk",
            "fail",
            f"{free // (1024 * 1024)} MB free at {directory}",
            fix="free disk space now; the database and backups stop writing when the disk is full",
            doc=DOC_OPERATIONS,
        )
    if free < DISK_WARN_BYTES:
        return Check(
            "disk",
            "warn",
            f"{free // (1024 * 1024)} MB free at {directory}",
            fix="free disk space or prune old backups (concierge backup --keep-days)",
            doc=DOC_OPERATIONS,
        )
    return Check("disk", "pass", f"{free // (1024 * 1024 * 1024)} GB free at {directory}")


def check_backups(directory: Path, now: datetime) -> Check:
    newest = latest_backup(directory) if directory.is_dir() else None
    if newest is None:
        return Check(
            "backups",
            "warn",
            f"no backup in {directory}",
            fix="run concierge backup (and schedule it daily; see OPERATIONS.md, backups)",
            doc=DOC_OPERATIONS,
        )
    stamp = newest.name.split("-", 1)[1].split(".", 1)[0]
    try:
        made = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        made = datetime.fromtimestamp(newest.stat().st_mtime, tz=UTC)
    if now - made > BACKUP_MAX_AGE:
        return Check(
            "backups",
            "warn",
            f"newest backup {newest.name} is older than {BACKUP_MAX_AGE.days} days",
            fix="run concierge backup and check the scheduled job",
            doc=DOC_OPERATIONS,
        )
    return Check("backups", "pass", f"newest backup {newest.name}")


def data_directory(settings: Settings) -> Path:
    path = sqlite_path(settings.database_url())
    return path.parent if path is not None else default_data_dir()


# ---------------------------------------------------------------- tenant checks
def registration_checks(settings: Settings, slug: str, profile: TenantProfile) -> list[Check]:
    """The § 29 BMG confirmation needs a real payment-proof provider outside demos."""
    if not profile.registration.enabled:
        return []
    psp = profile.registration.psp
    if psp == "stripe" and not settings.stripe_secret_key.get_secret_value():
        return [
            Check(
                "registration.proof",
                "fail",
                "registration.psp is stripe but no Stripe key is configured: confirmations are refused",
                fix="set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET, or set registration.psp to fake for a demo",
                doc=DOC_INTEGRATIONS,
                tenant=slug,
            )
        ]
    if psp == "fake" and settings.is_production:
        return [
            Check(
                "registration.proof",
                "fail",
                "registration.psp is fake in production: confirmations would be simulated",
                fix="set registration.psp to stripe and STRIPE_SECRET_KEY",
                doc=DOC_INTEGRATIONS,
                tenant=slug,
            )
        ]
    return [Check("registration.proof", "pass", f"payment proof provider {psp}", tenant=slug)]


async def tenant_checks(services: Any, tenant: Tenant, profile: TenantProfile) -> list[Check]:
    """Prompt sizes, KB coverage and overdue entries, legal status, channel configuration."""
    slug = tenant.slug
    checks: list[Check] = [check_timezone(profile.timezone, field_name=f"{slug}.timezone")]
    checks.extend(await prompt_size_checks(services, tenant, profile))
    coverage = await services.kb.coverage(tenant.id, list(profile.languages))
    gaps = sorted(
        f"{key}:{lang}"
        for key, per_lang in coverage.items()
        for lang, status in per_lang.items()
        if status != "approved"
    )
    if gaps:
        shown = ", ".join(gaps[:10]) + (f" and {len(gaps) - 10} more" if len(gaps) > 10 else "")
        checks.append(
            Check(
                "kb.coverage",
                "warn",
                f"{len(gaps)} required key/language pairs not approved: {shown}",
                fix="approve the go-live KB keys in the admin (Knowledge base) or import the pack",
                doc=DOC_CUSTOMISATION,
                tenant=slug,
            )
        )
    else:
        checks.append(Check("kb.coverage", "pass", "every required key approved", tenant=slug))
    checks.extend(registration_checks(services.ctx.settings, slug, profile))
    overdue = await services.kb.overdue(tenant.id)
    if overdue:
        keys = sorted({f"{row.key}:{row.lang}" for row in overdue})
        checks.append(
            Check(
                "kb.review",
                "warn",
                f"{len(keys)} entries past their review date: {', '.join(keys[:8])}",
                fix="confirm or update the entries in the admin (Knowledge base, review due)",
                doc=DOC_CUSTOMISATION,
                tenant=slug,
            )
        )
    else:
        checks.append(Check("kb.review", "pass", "no entry past its review date", tenant=slug))
    rows = await services.legal.rows(tenant.id)
    unapproved = sorted({f"{r.key}:{r.lang}" for r in rows if r.status != "approved"})
    if not rows:
        checks.append(
            Check(
                "legal.status",
                "warn",
                "no legal texts installed",
                fix="install the drafts (tenant add does this) and have a lawyer review them",
                doc=DOC_CUSTOMISATION,
                tenant=slug,
            )
        )
    elif unapproved:
        checks.append(
            Check(
                "legal.status",
                "warn",
                f"{len(unapproved)} legal texts not approved (lawyer review pending): {', '.join(unapproved[:6])}",
                fix="approve each text with name and date in the admin (Legal) after the review",
                doc=DOC_CUSTOMISATION,
                tenant=slug,
            )
        )
    else:
        checks.append(Check("legal.status", "pass", "every legal text approved", tenant=slug))
    checks.append(await whatsapp_config_check(services, tenant, profile))
    checks.extend(await availability_config_checks(services, tenant, profile))
    checks.append(voice_config_check(services.ctx.settings, profile, slug))
    return checks


async def prompt_size_checks(services: Any, tenant: Tenant, profile: TenantProfile) -> list[Check]:
    checks: list[Check] = []
    prompts = services.brain.prompts
    budget = profile.limits.prompt_token_budget
    for lang in profile.languages:
        snapshot = await services.kb.snapshot(tenant.id, lang)
        views, _ = await services.kb.resolve(tenant.id, lang, sensitivities=("staff_only",))
        persona = next((v.text for v in views if v.key == "tone"), "")
        policy = next((v.text for v in views if v.key == "refusals"), "")
        for channel in CHANNELS:
            blocks = prompts.system_blocks(
                profile, kb_snapshot=snapshot.text, channel=channel, persona=persona, policy=policy
            )
            tokens = estimate_tokens("\n".join(b.text for b in blocks))
            name = f"prompt.{lang}.{channel}"
            if tokens < PROMPT_MIN_TOKENS:
                checks.append(
                    Check(
                        name,
                        "warn",
                        f"about {tokens} tokens (estimate): under {PROMPT_MIN_TOKENS}, the prompt cache does nothing",
                        fix="approve more KB facts for this language; the cached prefix must exceed 512 tokens",
                        doc=DOC_CUSTOMISATION,
                        tenant=tenant.slug,
                    )
                )
            elif tokens > budget:
                checks.append(
                    Check(
                        name,
                        "warn",
                        f"about {tokens} tokens (estimate): over the budget of {budget}",
                        fix="shorten free-text KB entries or raise limits.prompt_token_budget in the profile",
                        doc=DOC_CUSTOMISATION,
                        tenant=tenant.slug,
                    )
                )
            else:
                checks.append(
                    Check(name, "pass", f"about {tokens} tokens (estimate)", tenant=tenant.slug)
                )
    return checks


async def whatsapp_config_check(services: Any, tenant: Tenant, profile: TenantProfile) -> Check:
    missing_fields = [
        name
        for name, value in (
            ("whatsapp.phone_number_id", profile.whatsapp.phone_number_id),
            ("whatsapp.waba_id", profile.whatsapp.waba_id),
        )
        if not value
    ]
    names = await services.secrets.names(tenant.id)
    missing_secrets = [
        s for s in ("wa_access_token", "wa_app_secret", "wa_verify_token") if s not in names
    ]
    if missing_fields or missing_secrets:
        parts = []
        if missing_fields:
            parts.append("profile fields " + ", ".join(missing_fields))
        if missing_secrets:
            parts.append("secrets " + ", ".join(missing_secrets))
        return Check(
            "whatsapp.config",
            "warn",
            "WhatsApp not configured: " + "; ".join(parts),
            fix="set the fields in the tenant profile and enter each secret with concierge secrets set "
            f"{tenant.slug} <name>",
            doc=DOC_INTEGRATIONS,
            tenant=tenant.slug,
        )
    return Check("whatsapp.config", "pass", "number, WABA and secrets present", tenant=tenant.slug)


async def availability_config_checks(
    services: Any, tenant: Tenant, profile: TenantProfile
) -> list[Check]:
    names = set(await services.secrets.names(tenant.id))
    fields = profile.availability
    checks: list[Check] = []
    needs: dict[str, list[str]] = {
        "beds24": [s for s in ("beds24_refresh_token",) if s not in names]
        + ([] if fields.beds24.property_id else ["field availability.beds24.property_id"]),
        "apaleo": [s for s in ("apaleo_client_id", "apaleo_client_secret") if s not in names]
        + ([] if fields.apaleo.property_id else ["field availability.apaleo.property_id"]),
        "mews": []
        if (fields.mews.hotel_id and fields.mews.configuration_id)
        or {"mews_client_token", "mews_access_token"} <= names
        else ["fields availability.mews.hotel_id and configuration_id (or the connector secrets)"],
        "ical": []
        if any(n.startswith("ical_url_") for n in names) and fields.ical_units
        else ["secrets ical_url_<room>_<n> and field availability.ical_units"],
    }
    for source in fields.chain:
        missing = needs.get(source, [])
        if missing:
            checks.append(
                Check(
                    f"availability.{source}",
                    "warn",
                    f"{source} in the chain but not configured: {', '.join(missing)}",
                    fix=f"enter the secrets with concierge secrets set {tenant.slug} <name> and set the profile fields",
                    doc=DOC_INTEGRATIONS,
                    tenant=tenant.slug,
                )
            )
        else:
            checks.append(
                Check(f"availability.{source}", "pass", f"{source} configured", tenant=tenant.slug)
            )
    return checks


def voice_config_check(settings: Settings, profile: TenantProfile, slug: str) -> Check:
    voice = profile.voice
    if voice.provider == "none":
        return Check("voice.config", "pass", "voice fallback off", tenant=slug)
    missing: list[str] = []
    if voice.provider == "retell" and not settings.retell_api_key.get_secret_value():
        missing.append("RETELL_API_KEY")
    if voice.provider == "twilio_cr" and not (
        settings.twilio_account_sid and settings.twilio_auth_token.get_secret_value()
    ):
        missing.append("TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN")
    if not voice.number:
        missing.append("field voice.number")
    if missing:
        return Check(
            "voice.config",
            "warn",
            f"voice ({voice.provider}) not configured: {', '.join(missing)}",
            fix="set the variables and the number; concierge voice provision creates the agent",
            doc=DOC_INTEGRATIONS,
            tenant=slug,
        )
    return Check("voice.config", "pass", f"{voice.provider} with number", tenant=slug)


# ---------------------------------------------------------------- runs
async def run_offline(
    ctx: AppContext,
    services: Any,
    *,
    tenant_slug: str | None = None,
    fonts_dir: Path = FONTS_DIR,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> DoctorReport:
    settings = ctx.settings
    report = DoctorReport()
    for check in check_environment(settings):
        report.add(check)
    for check in check_keys(settings):
        report.add(check)
    report.add(await check_migrations(ctx))
    report.add(check_fonts(fonts_dir))
    report.add(check_timezone(settings.gc_tz_default, field_name="GC_TZ_DEFAULT"))
    newest = await services.metering.newest_price_dates()
    report.add(check_price_tables(newest, ctx.clock.now().date()))
    directory = data_directory(settings)
    report.add(check_disk(directory, disk_usage))
    report.add(check_backups(directory / "backups", ctx.clock.now()))
    dead = await services.jobs.dead()
    if dead:
        report.add(
            Check(
                "jobs.dead",
                "warn",
                f"{len(dead)} dead job(s)",
                fix="open the operator console (dead jobs), fix the cause and re-enqueue",
                doc=DOC_OPERATIONS,
            )
        )
    else:
        report.add(Check("jobs.dead", "pass", "no dead jobs"))
    tenants = await services.tenants.list()
    if tenant_slug:
        tenants = [t for t in tenants if t.slug == tenant_slug]
        if not tenants:
            report.add(
                Check("tenant", "fail", f"no tenant {tenant_slug!r}", fix="concierge tenant list")
            )
    for tenant in tenants:
        profile = services.tenants.profile_of(tenant)
        for check in await tenant_checks(services, tenant, profile):
            report.add(check)
    return report


async def record_live_result(
    ctx: AppContext, tenant: Tenant, checks: list[Check], platform_failed: list[str]
) -> bool:
    """One audit row per tenant; a platform failure (URL, model, mail) blocks every tenant."""
    failed = [c.name for c in checks if c.status == "fail"] + list(platform_failed)
    ok = not failed
    async with ctx.db.scope() as session:
        audit.record(
            session,
            actor="cli:doctor",
            action=LIVE_ACTION,
            target=tenant.slug,
            tenant_id=tenant.id,
            details={"ok": ok, "failed": failed, "checks": len(checks)},
        )
        await session.commit()
    return ok


async def run_live(
    ctx: AppContext,
    services: Any,
    *,
    tenant_slug: str | None = None,
    probes: Callable[[AppContext, Any, Tenant | None], Awaitable[list[Check]]],
) -> DoctorReport:
    """Offline checks first, then the live probes; one audit row per tenant."""
    report = await run_offline(ctx, services, tenant_slug=tenant_slug)
    report.live = True
    platform = await probes(ctx, services, None)
    for check in platform:
        report.add(check)
    platform_failed = [c.name for c in platform if c.status == "fail"]
    tenants = await services.tenants.list()
    if tenant_slug:
        tenants = [t for t in tenants if t.slug == tenant_slug]
    for tenant in tenants:
        checks = await probes(ctx, services, tenant)
        for check in checks:
            report.add(check)
        await record_live_result(ctx, tenant, checks, platform_failed)
    return report


async def live_ok_for_tenant(ctx: AppContext, slug: str) -> bool | None:
    """True/False from the latest recorded live run; None when none was recorded."""
    async with ctx.db.scope() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is None:
            return None
        row = await session.scalar(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant.id, AuditLog.action == LIVE_ACTION)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    if row is None:
        return None
    return bool(row.details.get("ok"))
