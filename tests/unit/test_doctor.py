"""Doctor (F28): a failing case per check, exit codes, no secrets in the output, live probes
offline through injected transports, and the recorded live result."""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from concierge.context import AppContext
from concierge.db.session import Database
from concierge.eval.golden import GoldenStack
from concierge.llm.port import LLMError, LLMRequest, LLMResponse, ModelInfo, TextPart, Usage
from concierge.notify.port import EmailMessage, EmailResult
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile
from concierge.tools import doctor
from concierge.tools.livechecks import LiveProbes

pytestmark = pytest.mark.feature("F28")

FAKE_KEY = "fake-anthropic-key-for-doctor-tests"
FAKE_TOKEN = "EAAfake-graph-token"


def with_(settings: Settings, **overrides: Any) -> Settings:
    """A validated copy (``model_copy`` would leave secret fields as plain strings)."""
    data = settings.model_dump()
    data.update(overrides)
    return Settings.model_validate(data)


def names(report: doctor.DoctorReport, status: str | None = None) -> dict[str, doctor.Check]:
    return {c.name: c for c in report.checks if status is None or c.status == status}


# ------------------------------------------------------------ pure checks
def test_environment_and_key_checks_name_the_variable(settings: Settings, master_key: str) -> None:
    production = with_(settings, gc_env="production", gc_public_base_url="http://localhost:8000")
    checks = {c.name: c for c in doctor.check_environment(production)}
    assert checks["config.public_url"].status == "fail"
    assert "GC_PUBLIC_BASE_URL" in checks["config.public_url"].fix
    synced = with_(settings, gc_database_url="sqlite+aiosqlite:////home/u/OneDrive/x.db")
    assert {c.name: c for c in doctor.check_environment(synced)}["config.database"].status == "warn"
    quiet = with_(settings, gc_operator_email="", gc_metrics_token="")
    found = {c.name: c for c in doctor.check_environment(quiet)}
    assert (
        found["config.operator_email"].status == "warn"
        and "GC_OPERATOR_EMAIL" in found["config.operator_email"].fix
    )
    assert found["config.metrics_token"].status == "warn"
    assert doctor.check_email_config(with_(settings, gc_email_provider="resend")).status == "warn"
    assert (
        "RESEND_API_KEY"
        in doctor.check_email_config(with_(settings, gc_email_provider="resend")).detail
    )
    assert doctor.check_email_config(with_(settings, gc_email_provider="none")).status == "warn"
    assert (
        doctor.check_email_config(
            with_(
                settings,
                gc_email_provider="smtp",
                gc_smtp_host="mail.example.invalid",
                gc_email_from="a@b.invalid",
            )
        ).status
        == "pass"
    )
    assert doctor.check_llm_config(settings).status == "warn"
    broken = with_(settings, gc_llm_provider="anthropic")
    llm = doctor.check_llm_config(broken)
    assert llm.status == "fail" and "ANTHROPIC_API_KEY" in llm.detail
    keys = {c.name: c for c in doctor.check_keys(with_(settings, gc_master_keys="not json"))}
    assert keys["keys.master"].status == "fail" and "GC_MASTER_KEYS" in keys["keys.master"].detail
    keys = {c.name: c for c in doctor.check_keys(with_(settings, gc_active_key_id="k9"))}
    assert keys["keys.master"].status == "fail" and "GC_ACTIVE_KEY_ID" in keys["keys.master"].detail
    keys = {c.name: c for c in doctor.check_keys(with_(settings, gc_signing_key="short"))}
    assert keys["keys.signing"].status == "fail" and "GC_SIGNING_KEY" in keys["keys.signing"].detail
    keys = {c.name: c for c in doctor.check_keys(with_(settings, gc_backup_key="nope"))}
    assert keys["keys.backup"].status == "fail" and "GC_BACKUP_KEY" in keys["keys.backup"].detail
    assert all(c.status == "pass" for c in doctor.check_keys(settings))
    assert master_key not in doctor.DoctorReport(doctor.check_keys(settings)).render()


def test_fonts_timezone_prices_disk_and_backups(tmp_path: Path) -> None:
    fonts = doctor.check_fonts(tmp_path)
    assert fonts.status == "fail" and "Inter-Regular.ttf" in fonts.detail
    assert doctor.check_fonts().status == "pass"
    zone = doctor.check_timezone("Mars/Olympus", field_name="GC_TZ_DEFAULT")
    assert zone.status == "fail" and "Mars/Olympus" in zone.detail and "tzdata" in zone.fix
    assert doctor.check_timezone("Europe/Berlin", field_name="x").status == "pass"
    today = date(2026, 9, 29)
    stale = doctor.check_price_tables((date(2026, 1, 1), date(2026, 3, 1)), today)
    assert stale.status == "warn" and "rate card" in stale.detail and "2026-01-01" in stale.detail
    fresh = doctor.check_price_tables((date(2026, 9, 1), date(2026, 9, 29)), today)
    assert fresh.status == "pass" and "rate card" in fresh.detail
    assert doctor.check_price_tables((None, None), today).status == "warn"

    class Usage:
        def __init__(self, free: int) -> None:
            self.free = free

    low = doctor.check_disk(tmp_path, lambda _p: Usage(100 * 1024 * 1024))
    assert low.status == "fail" and "MB free" in low.detail
    assert doctor.check_disk(tmp_path, lambda _p: Usage(600 * 1024 * 1024)).status == "warn"
    assert doctor.check_disk(tmp_path, lambda _p: Usage(50 * 1024**3)).status == "pass"
    now = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    missing = doctor.check_backups(tmp_path / "backups", now)
    assert missing.status == "warn" and "concierge backup" in missing.fix
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "concierge-20260901T100000Z.sqlite.enc").write_bytes(b"x")
    old = doctor.check_backups(tmp_path / "backups", now)
    assert old.status == "warn" and "older than" in old.detail
    (tmp_path / "backups" / "concierge-20260929T090000Z.sqlite.enc").write_bytes(b"x")
    assert doctor.check_backups(tmp_path / "backups", now).status == "pass"


def test_report_exit_codes_and_rendering() -> None:
    report = doctor.DoctorReport([doctor.Check("a", "pass", "fine")])
    assert report.exit_code == 0 and "1 pass, 0 warn, 0 fail" in report.render()
    report.add(doctor.Check("b", "warn", "hmm", fix="do x", doc="docs/OPERATIONS.md", tenant="t"))
    assert report.exit_code == 1 and "fix: do x (docs/OPERATIONS.md)" in report.render()
    report.add(doctor.Check("c", "fail", "bad"))
    assert report.exit_code == 2 and report.to_dict()["exit_code"] == 2
    assert "[t] hmm" in report.render()


# ------------------------------------------------------------ the offline run
async def test_offline_run_flags_missing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = await GoldenStack.create(tmp_path / "doc.db", responder_only=True)
    try:
        report = await doctor.run_offline(stack.services.ctx, stack.services)
        found = names(report)
        assert found["database.migrations"].status == "pass"
        assert found["keys.master"].status == "pass" and found["fonts"].status == "pass"
        assert found["tzdata"].status == "pass"
        assert found["prices.age"].status == "pass" and "rate card" in found["prices.age"].detail
        assert found["backups"].status == "warn"
        assert found["jobs.dead"].status == "pass"
        # the demo tenant: fixture KB approved, legal drafts unreviewed, WhatsApp demo secrets set
        by_tenant = {c.name: c for c in report.checks if c.tenant == "hotel-beispiel"}
        assert by_tenant["kb.coverage"].status == "pass"
        assert (
            by_tenant["legal.status"].status == "warn"
            and "lawyer review" in by_tenant["legal.status"].detail
        )
        assert by_tenant["whatsapp.config"].status == "pass"
        assert by_tenant["prompt.de.whatsapp"].status == "pass"
        assert by_tenant["availability.manual"].status == "pass"
        assert by_tenant["voice.config"].status in {"pass", "warn"}
        assert report.exit_code == 1  # warnings only
        assert not report.failed
        # a second tenant without KB, legal texts or secrets
        profile = TenantProfile.model_validate(
            {
                "slug": "bare-hotel",
                "name": "Bare Hotel",
                "timezone": "Europe/Berlin",
                "status": "pilot",
                "languages": ["de", "en"],
                "availability": {"chain": ["beds24", "ical", "none"]},
                "voice": {"provider": "retell"},
                "widget": {"public_key": "pk_barehotel"},
            }
        )
        await stack.services.tenants.create(profile, actor="test")
        report = await doctor.run_offline(
            stack.services.ctx, stack.services, tenant_slug="bare-hotel"
        )
        bare = {c.name: c for c in report.checks if c.tenant == "bare-hotel"}
        assert bare["prompt.de.whatsapp"].status == "pass"  # the core prompt alone is long
        monkeypatch.setattr(doctor, "PROMPT_MIN_TOKENS", 10**6)
        small = await doctor.run_offline(
            stack.services.ctx, stack.services, tenant_slug="bare-hotel"
        )
        under = {c.name: c for c in small.checks if c.tenant == "bare-hotel"}["prompt.de.widget"]
        assert under.status == "warn" and "1000000" in under.detail and "512" in under.fix
        monkeypatch.setattr(doctor, "PROMPT_MIN_TOKENS", 512)
        assert bare["kb.coverage"].status == "warn" and "breakfast:de" in bare["kb.coverage"].detail
        assert bare["legal.status"].status == "warn"
        assert (
            bare["whatsapp.config"].status == "warn"
            and "wa_access_token" in bare["whatsapp.config"].detail
        )
        assert (
            bare["availability.beds24"].status == "warn"
            and "beds24_refresh_token" in bare["availability.beds24"].detail
        )
        assert bare["availability.ical"].status == "warn"
        assert (
            bare["voice.config"].status == "warn"
            and "RETELL_API_KEY" in bare["voice.config"].detail
        )
        assert report.exit_code == 1
        missing = await doctor.run_offline(stack.services.ctx, stack.services, tenant_slug="nope")
        assert names(missing)["tenant"].status == "fail"
        # a tenant with a KB far over the prompt budget
        big = profile.model_copy(
            update={
                "slug": "big-hotel",
                "limits": {"prompt_token_budget": 1000},
                "widget": {"public_key": "pk_bighotel"},
            }
        )
        await stack.services.tenants.create(
            TenantProfile.model_validate(big.model_dump(mode="json")), actor="test"
        )
        tenant = await stack.services.tenants.require("big-hotel")
        for index in range(12):
            await stack.services.kb.upsert(
                tenant.id,
                "house_rules",
                "de",
                text="Regel " * 200 + str(index),
                status="approved",
                approver="t",
                actor="t",
            )
        report = await doctor.run_offline(
            stack.services.ctx, stack.services, tenant_slug="big-hotel"
        )
        over = {c.name: c for c in report.checks if c.tenant == "big-hotel"}["prompt.de.whatsapp"]
        assert over.status == "warn" and "budget" in over.detail
    finally:
        await stack.close()


async def test_offline_run_fails_on_a_database_behind_head(
    settings: Settings, tmp_path: Path
) -> None:
    class Stub:
        pass

    database = Database(settings.database_url())
    try:
        from concierge.clock import FakeClock
        from concierge.security.crypto import Keyring

        ctx = AppContext(
            settings=settings,
            db=database,
            keyring=Keyring(
                {
                    "k1": base64.b64decode(
                        settings.gc_master_keys.get_secret_value()
                        and json.loads(settings.gc_master_keys.get_secret_value())["k1"]
                    )
                },
                "k1",
            ),
            clock=FakeClock(datetime(2026, 9, 29, 10, 0, tzinfo=UTC)),
        )
        check = await doctor.check_migrations(ctx)
        assert check.status == "fail" and "db upgrade" in check.fix
    finally:
        await database.dispose()


# ------------------------------------------------------------ live probes offline
class FakeLLM:
    def __init__(self, *, auth_ok: bool) -> None:
        self.auth_ok = auth_ok

    async def list_models(self) -> list[ModelInfo]:
        if not self.auth_ok:
            raise LLMError("auth", "invalid x-api-key")
        return [ModelInfo("claude-sonnet-5-5"), ModelInfo("claude-sonnet-5")]

    async def count_tokens(self, request: LLMRequest) -> int:
        return 42

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse([TextPart("OK")], "end_turn", Usage(10, 1), request.model)


class FakeMail:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage, *, sender: str) -> EmailResult:
        self.sent.append(message)
        return EmailResult(ok=self.ok, error="" if self.ok else "401 invalid api key")


def graph_handler(status: int) -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        if host == "graph.facebook.com":
            if status != 200:
                return httpx2.Response(
                    status, json={"error": {"message": "Invalid OAuth access token", "code": 190}}
                )
            path = request.url.path
            if path.endswith("/subscribed_apps"):
                return httpx2.Response(
                    200, json={"data": [{"whatsapp_business_api_data": {"id": "1"}}]}
                )
            if path.endswith("/message_templates"):
                return httpx2.Response(
                    200,
                    json={
                        "data": [
                            {
                                "name": "gc_booking_followup",
                                "language": "de",
                                "status": "APPROVED",
                                "category": "UTILITY",
                                "components": [],
                                "id": "1",
                            }
                        ]
                    },
                )
            return httpx2.Response(
                200,
                json={
                    "display_phone_number": "+49 69 000000",
                    "verified_name": "Hotel Beispiel",
                    "quality_rating": "GREEN",
                    "messaging_limit_tier": "TIER_1K",
                },
            )
        if host in {"127.0.0.1", "localhost"}:
            if request.url.path == "/healthz":
                return httpx2.Response(
                    200, json={"status": "ok"}, headers={"date": "Tue, 29 Sep 2026 10:00:00 GMT"}
                )
            if request.url.path == "/readyz":
                return httpx2.Response(503, json={"status": "not ready"})
            if request.url.path == "/wa/webhook":
                return httpx2.Response(200 if status == 200 else 403, text="ok")
            if request.url.path == "/r/psp/webhook":
                return httpx2.Response(401, json={"error": "invalid signature"})
        return httpx2.Response(404)

    return handler


async def test_live_probes_fail_precisely_with_fake_keys(tmp_path: Path) -> None:
    stack = await GoldenStack.create(tmp_path / "live.db", responder_only=True)
    try:
        ctx = stack.services.ctx
        ctx.settings = with_(
            ctx.settings,
            gc_llm_provider="anthropic",
            anthropic_api_key=FAKE_KEY,
            stripe_secret_key="sk_test_fake",
            stripe_webhook_secret="whsec_fake",
            retell_api_key="key_fake",
        )
        await stack.services.secrets.set(stack.tenant.id, "wa_access_token", FAKE_TOKEN, actor="t")
        probes = LiveProbes(
            transport=httpx2.MockTransport(graph_handler(401)),
            llm=FakeLLM(auth_ok=False),
            email=FakeMail(ok=False),
            ws_connect=_ws_fail,
            now=lambda: datetime(2026, 9, 29, 10, 0, 30, tzinfo=UTC),
        )
        report = await doctor.run_live(
            ctx, stack.services, tenant_slug="hotel-beispiel", probes=probes
        )
        assert report.live
        found = names(report)
        assert (
            found["platform/healthz"].status == "pass" and found["platform.clock"].status == "pass"
        )
        assert (
            found["platform/readyz"].status == "fail" and "readyz" in found["platform/readyz"].fix
        )
        assert found["anthropic"].status == "fail" and "ANTHROPIC_API_KEY" in found["anthropic"].fix
        assert found["email"].status == "fail" and "RESEND_API_KEY" in found["email"].fix
        assert found["stripe.key"].detail == "key mode test"
        assert (
            found["stripe.webhook"].status == "fail"
            and "STRIPE_WEBHOOK_SECRET" in found["stripe.webhook"].fix
        )
        assert (
            found["whatsapp.token"].status == "fail"
            and "wa_access_token" in found["whatsapp.token"].fix
        )
        assert (
            found["whatsapp.subscription"].status == "fail"
            and found["whatsapp.templates"].status == "fail"
        )
        assert (
            found["whatsapp.webhook"].status == "fail"
            and "wa_app_secret" in found["whatsapp.webhook"].fix
        )
        rendered = report.render()
        assert (
            FAKE_KEY not in rendered and FAKE_TOKEN not in rendered and "whsec_fake" not in rendered
        )
        assert json.dumps(report.to_dict()).count(FAKE_KEY) == 0
        assert await doctor.live_ok_for_tenant(ctx, "hotel-beispiel") is False
        assert report.exit_code == 2

        good = LiveProbes(
            transport=httpx2.MockTransport(graph_handler(200)),
            llm=FakeLLM(auth_ok=True),
            email=FakeMail(ok=True),
            ws_connect=_ws_ok,
            now=lambda: datetime(2026, 9, 29, 10, 0, 30, tzinfo=UTC),
        )
        ctx.settings = with_(ctx.settings, stripe_webhook_secret="")
        report = await doctor.run_live(
            ctx, stack.services, tenant_slug="hotel-beispiel", probes=good
        )
        found = names(report)
        assert (
            found["anthropic"].status == "pass" and "count_tokens 42" in found["anthropic"].detail
        )
        assert found["email"].status == "pass" and found["whatsapp.token"].status == "pass"
        assert "TIER_1K" in found["whatsapp.token"].detail
        assert found["whatsapp.templates"].status == "warn"  # only German approved
        assert (
            found["whatsapp.webhook"].status == "pass"
            and found["whatsapp.reminders"].status == "pass"
        )
        assert "stripe.webhook" not in found
        assert found["platform/readyz"].status == "fail"
        tenant_only = [c for c in report.checks if c.tenant == "hotel-beispiel"]
        assert all(c.status != "fail" for c in tenant_only)
        # a platform failure (/readyz) still blocks the tenant's activation gate
        assert await doctor.live_ok_for_tenant(ctx, "hotel-beispiel") is False
    finally:
        await stack.close()


async def test_retell_and_twilio_probes(tmp_path: Path) -> None:
    stack = await GoldenStack.create(tmp_path / "voice.db", responder_only=True)
    try:
        ctx = stack.services.ctx
        services = stack.services
        profile = services.tenants.profile_of(stack.tenant)
        voice = profile.model_copy(
            update={
                "voice": profile.voice.model_copy(
                    update={"provider": "retell", "number": "+4969000009"}
                )
            }
        )
        stack.tenant = await services.tenants.update_profile(stack.tenant.slug, voice, actor="t")
        ctx.settings = with_(ctx.settings, retell_api_key="key_fake", gc_llm_provider="none")

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/voice/retell/webhook":
                return httpx2.Response(401)
            if request.url.path.startswith("/voice/twilio/"):
                return httpx2.Response(403)
            return httpx2.Response(200, json={"status": "ok"})

        probes = LiveProbes(
            transport=httpx2.MockTransport(handler),
            ws_connect=_ws_fail,
            llm=FakeLLM(auth_ok=True),
            email=FakeMail(ok=True),
        )
        checks = {c.name: c for c in await probes(ctx, services, stack.tenant)}
        assert (
            checks["retell.signature"].status == "fail"
            and "RETELL_API_KEY" in checks["retell.signature"].fix
        )
        assert checks["retell.wss"].status == "fail" and "WebSocket" in checks["retell.wss"].fix
        assert (
            checks["retell.number"].status == "pass" and "SCCs" in checks["retell.reminders"].detail
        )
        twilio = voice.model_copy(
            update={"voice": voice.voice.model_copy(update={"provider": "twilio_cr"})}
        )
        stack.tenant = await services.tenants.update_profile(stack.tenant.slug, twilio, actor="t")
        ctx.settings = with_(
            ctx.settings, twilio_account_sid="ACfake", twilio_auth_token="fake-token"
        )
        checks = {c.name: c for c in await probes(ctx, services, stack.tenant)}
        assert (
            checks["twilio.signature"].status == "fail"
            and "TWILIO_AUTH_TOKEN" in checks["twilio.signature"].fix
        )
        ctx.settings = with_(ctx.settings, twilio_auth_token="")
        checks = {c.name: c for c in await probes(ctx, services, stack.tenant)}
        assert checks["twilio"].status == "fail" and "TWILIO_ACCOUNT_SID" in checks["twilio"].fix
    finally:
        await stack.close()


async def _ws_fail(url: str) -> None:
    raise ConnectionRefusedError(url)


async def _ws_ok(url: str) -> None:
    return None


def test_estimate_tokens_is_rough_and_positive() -> None:
    assert doctor.estimate_tokens("") == 1 and doctor.estimate_tokens("a" * 4000) == 1000
    assert doctor.PROMPT_MIN_TOKENS == 512 and doctor.STALE_PRICE_DAYS == 180
    assert timedelta(days=2) == doctor.BACKUP_MAX_AGE
