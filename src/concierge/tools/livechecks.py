"""Live integration probes for ``concierge doctor --live`` (section 7 of the build prompt).

Implements: F28, F21

Every probe fails with the variable or profile field to fix; nothing here sends guest
data (C4): the model gets a fixed sixteen-token prompt, Meta is only read, and the
webhook round trips carry synthetic events signed with the configured secrets. The
probes take injectable transports so tests run them offline.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx2

from concierge.channels.voice.port import VOICE_SOCKET_SECRET
from concierge.channels.whatsapp.graph import GraphClient
from concierge.context import AppContext
from concierge.db.models import Tenant
from concierge.llm.anthropic_adapter import AnthropicAdapter
from concierge.llm.port import LLMError, LLMPort, LLMRequest, Message, TextBlock, TextPart
from concierge.notify.email import build_email_port
from concierge.notify.port import EmailMessage, EmailPort
from concierge.security.signatures import sign_meta, sign_retell, sign_stripe, sign_twilio
from concierge.tools.doctor import DOC_INTEGRATIONS, DOC_OPERATIONS, Check

CLOCK_SKEW_S = 60
PROBE_TIMEOUT_S = 15.0
TEST_MAIL_SUBJECT = "WITALCO Guest Concierge: doctor test mail"


@dataclass
class LiveProbes:
    """Injectable network dependencies; production uses the real ones."""

    transport: httpx2.AsyncBaseTransport | None = None
    llm: LLMPort | None = None
    email: EmailPort | None = None
    ws_connect: Callable[[str], Awaitable[None]] | None = None
    now: Callable[[], datetime] = field(default_factory=lambda: lambda: datetime.now(UTC))

    def client(self) -> httpx2.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": PROBE_TIMEOUT_S}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx2.AsyncClient(**kwargs)

    async def __call__(self, ctx: AppContext, services: Any, tenant: Tenant | None) -> list[Check]:
        if tenant is None:
            return await self.platform_checks(ctx, services)
        return await self.tenant_checks(ctx, services, tenant)

    # --------------------------------------------------------------- platform
    async def platform_checks(self, ctx: AppContext, services: Any) -> list[Check]:
        settings = ctx.settings
        checks: list[Check] = []
        base = settings.public_base_url()
        async with self.client() as client:
            for path in ("/healthz", "/readyz"):
                try:
                    response = await client.get(base + path)
                except httpx2.HTTPError as exc:
                    checks.append(
                        Check(
                            f"platform{path}",
                            "fail",
                            f"{base}{path} unreachable: {type(exc).__name__}",
                            fix="GC_PUBLIC_BASE_URL must resolve to this service over valid TLS (DNS, Caddy, firewall)",
                            doc=DOC_OPERATIONS,
                        )
                    )
                    continue
                if response.status_code != 200:
                    checks.append(
                        Check(
                            f"platform{path}",
                            "fail",
                            f"{base}{path} answered {response.status_code}",
                            fix="the service behind GC_PUBLIC_BASE_URL is not ready: check concierge serve and /readyz",
                            doc=DOC_OPERATIONS,
                        )
                    )
                    continue
                checks.append(Check(f"platform{path}", "pass", f"{base}{path} answers 200"))
                if path == "/healthz":
                    checks.append(self._clock_check(response.headers.get("date")))
        checks.append(await self.anthropic_checks(ctx, services))
        checks.append(await self.email_check(settings))
        checks.append(self.stripe_key_check(settings))
        return checks

    def _clock_check(self, header: str | None) -> Check:
        if not header:
            return Check("platform.clock", "pass", "no Date header to compare (local server)")
        try:
            remote = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return Check("platform.clock", "pass", "Date header unreadable; skipped")
        skew = abs((self.now() - remote).total_seconds())
        if skew > CLOCK_SKEW_S:
            return Check(
                "platform.clock",
                "warn",
                f"clock differs from the server's Date header by {skew:.0f} s",
                fix="enable NTP (timedatectl set-ntp true); signatures with timestamps fail on skew",
                doc=DOC_OPERATIONS,
            )
        return Check("platform.clock", "pass", f"clock skew {skew:.0f} s")

    async def anthropic_checks(self, ctx: AppContext, services: Any) -> Check:
        settings = ctx.settings
        if settings.gc_llm_provider != "anthropic":
            return Check(
                "anthropic",
                "warn",
                "not configured (GC_LLM_PROVIDER is not anthropic)",
                fix="set GC_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY",
                doc=DOC_INTEGRATIONS,
            )
        try:
            llm = self.llm or AnthropicAdapter(
                settings.anthropic_api_key.get_secret_value(), timeout_s=PROBE_TIMEOUT_S
            )
            models = {m.id for m in await llm.list_models()}
            wanted = [settings.gc_llm_model_primary, settings.gc_llm_model_failover]
            missing = [m for m in wanted if m not in models]
            if missing:
                return Check(
                    "anthropic.models",
                    "fail",
                    f"GET /v1/models lacks {', '.join(missing)}",
                    fix="set GC_LLM_MODEL_PRIMARY and GC_LLM_MODEL_FAILOVER to IDs the key can use",
                    doc=DOC_INTEGRATIONS,
                )
            cheap = settings.gc_llm_model_cheap
            note = ""
            if cheap and cheap not in models:
                note = f"; GC_LLM_MODEL_CHEAP {cheap} is not listed (retired?)"
            request = LLMRequest(
                model=settings.gc_llm_model_primary,
                system=[TextBlock("Reply with the single word OK.")],
                messages=[Message("user", [TextPart("OK?")])],
                max_tokens=16,
                effort="low",
                cache_tail=False,
            )
            tokens = await llm.count_tokens(request)
            response = await llm.complete(request)
            text = "".join(p.text for p in response.content if isinstance(p, TextPart))
            return Check(
                "anthropic",
                "warn" if note else "pass",
                f"models listed, count_tokens {tokens}, a 16-token call answered {text.strip()[:12]!r}{note}",
                fix="set GC_LLM_MODEL_CHEAP to a current model or leave it empty" if note else "",
                doc=DOC_INTEGRATIONS,
            )
        except LLMError as exc:
            fix = (
                "check ANTHROPIC_API_KEY (workspace key with a spend limit)"
                if exc.kind in {"auth", "unknown"}
                else "check the network path to api.anthropic.com and the spend limit"
            )
            return Check("anthropic", "fail", f"{exc.kind}: {exc}", fix=fix, doc=DOC_INTEGRATIONS)
        except Exception as exc:
            return Check(
                "anthropic",
                "fail",
                f"{type(exc).__name__}: {exc}",
                fix="check ANTHROPIC_API_KEY and the network path to api.anthropic.com",
                doc=DOC_INTEGRATIONS,
            )

    async def email_check(self, settings: Any) -> Check:
        port = self.email or build_email_port(settings)
        if port is None:
            return Check(
                "email",
                "warn",
                "not configured",
                fix="set GC_EMAIL_PROVIDER=resend with RESEND_API_KEY and GC_EMAIL_FROM, or smtp with GC_SMTP_HOST",
                doc=DOC_INTEGRATIONS,
            )
        if not settings.gc_operator_email:
            return Check(
                "email",
                "warn",
                "no GC_OPERATOR_EMAIL to send the test mail to",
                fix="set GC_OPERATOR_EMAIL",
                doc=DOC_INTEGRATIONS,
            )
        try:
            result = await port.send(
                EmailMessage(
                    to=settings.gc_operator_email,
                    subject=TEST_MAIL_SUBJECT,
                    text="This test mail was sent by concierge doctor --live.",
                ),
                sender=settings.gc_email_from,
            )
        except Exception as exc:
            return Check(
                "email",
                "fail",
                f"test mail failed: {type(exc).__name__}",
                fix="check RESEND_API_KEY and the verified sending domain (or GC_SMTP_* for smtp) and GC_EMAIL_FROM",
                doc=DOC_INTEGRATIONS,
            )
        if not result.ok:
            return Check(
                "email",
                "fail",
                f"test mail rejected: {result.error or 'no detail'}",
                fix="check RESEND_API_KEY and the verified sending domain (or GC_SMTP_* for smtp) and GC_EMAIL_FROM",
                doc=DOC_INTEGRATIONS,
            )
        return Check("email", "pass", f"test mail accepted for {settings.gc_operator_email}")

    def stripe_key_check(self, settings: Any) -> Check:
        key = settings.stripe_secret_key.get_secret_value()
        if not key:
            return Check(
                "stripe.key",
                "warn",
                "not configured (registration module)",
                fix="set STRIPE_SECRET_KEY (restricted key) and STRIPE_WEBHOOK_SECRET when F15 is used",
                doc=DOC_INTEGRATIONS,
            )
        mode = "live" if "_live_" in key else "test" if "_test_" in key else "unknown"
        return Check("stripe.key", "pass", f"key mode {mode}")

    # --------------------------------------------------------------- per tenant
    async def tenant_checks(self, ctx: AppContext, services: Any, tenant: Tenant) -> list[Check]:
        settings = ctx.settings
        profile = services.tenants.profile_of(tenant)
        slug = tenant.slug
        checks: list[Check] = []
        checks.extend(await self.whatsapp_checks(ctx, services, tenant, profile))
        for source in profile.availability.chain:
            if source in {"manual", "none"}:
                continue
            checks.append(await self.availability_check(services, tenant, profile, source))
        base = settings.public_base_url()
        async with self.client() as client:
            if settings.stripe_webhook_secret.get_secret_value():
                checks.append(await self.stripe_webhook_check(client, base, settings))
            if profile.voice.provider == "retell":
                checks.extend(
                    await self.retell_checks(
                        client,
                        base,
                        settings,
                        tenant,
                        profile,
                        socket_secret=await services.secrets.get(tenant.id, VOICE_SOCKET_SECRET)
                        or "",
                    )
                )
            if profile.voice.provider == "twilio_cr":
                checks.extend(await self.twilio_checks(client, base, settings, slug))
        return checks

    async def whatsapp_checks(
        self, ctx: AppContext, services: Any, tenant: Tenant, profile: Any
    ) -> list[Check]:
        settings = ctx.settings
        slug = tenant.slug
        values = await services.secrets.get_many(
            tenant.id, ["wa_access_token", "wa_app_secret", "wa_verify_token"]
        )
        token = values.get("wa_access_token", "")
        phone_number_id = profile.whatsapp.phone_number_id
        waba_id = profile.whatsapp.waba_id
        if not token or not phone_number_id or not waba_id:
            return [
                Check(
                    "whatsapp",
                    "warn",
                    "not configured",
                    fix=f"set whatsapp.phone_number_id and waba_id in the profile and concierge secrets set {slug} wa_access_token",
                    doc=DOC_INTEGRATIONS,
                    tenant=slug,
                )
            ]
        checks: list[Check] = []
        graph = GraphClient(version=settings.gc_wa_graph_version, transport=self.transport)
        try:
            try:
                info = await graph.phone_info(phone_number_id, access_token=token)
                tier = str(info.get("messaging_limit_tier") or "unknown")
                checks.append(
                    Check(
                        "whatsapp.token",
                        "pass",
                        f"number {info.get('display_phone_number', '?')} ({info.get('verified_name', '?')}), quality {info.get('quality_rating', '?')}, tier {tier}",
                        tenant=slug,
                    )
                )
            except Exception as exc:
                checks.append(
                    Check(
                        "whatsapp.token",
                        "fail",
                        f"GET /{{phone_number_id}} failed: {_describe(exc)}",
                        fix=f"the system-user token is invalid or lacks whatsapp_business_messaging: concierge secrets set {slug} wa_access_token; check whatsapp.phone_number_id",
                        doc=DOC_INTEGRATIONS,
                        tenant=slug,
                    )
                )
            try:
                apps = await graph.subscribed_apps(waba_id, access_token=token)
                if apps:
                    checks.append(
                        Check(
                            "whatsapp.subscription",
                            "pass",
                            f"{len(apps)} app(s) subscribed",
                            tenant=slug,
                        )
                    )
                else:
                    checks.append(
                        Check(
                            "whatsapp.subscription",
                            "fail",
                            "no app subscribed to the WABA",
                            fix="subscribe the app to the WABA (Business settings, WhatsApp accounts) with the messages and message_template_status_update fields",
                            doc=DOC_INTEGRATIONS,
                            tenant=slug,
                        )
                    )
            except Exception as exc:
                checks.append(
                    Check(
                        "whatsapp.subscription",
                        "fail",
                        f"GET /{{waba_id}}/subscribed_apps failed: {_describe(exc)}",
                        fix="check whatsapp.waba_id and the token scopes (whatsapp_business_management)",
                        doc=DOC_INTEGRATIONS,
                        tenant=slug,
                    )
                )
            try:
                templates = await graph.list_templates(waba_id, access_token=token)
                approved = [
                    t for t in templates if str(getattr(t, "status", "")).upper() == "APPROVED"
                ]
                langs = {str(getattr(t, "language", getattr(t, "lang", ""))) for t in approved}
                missing = [lang for lang in profile.languages if lang not in langs]
                if missing:
                    checks.append(
                        Check(
                            "whatsapp.templates",
                            "warn",
                            f"{len(approved)} approved templates; none approved for {', '.join(missing)}",
                            fix="run concierge whatsapp templates sync and wait for Meta's approval per language",
                            doc=DOC_INTEGRATIONS,
                            tenant=slug,
                        )
                    )
                else:
                    checks.append(
                        Check(
                            "whatsapp.templates",
                            "pass",
                            f"{len(approved)} approved templates cover every language",
                            tenant=slug,
                        )
                    )
            except Exception as exc:
                checks.append(
                    Check(
                        "whatsapp.templates",
                        "fail",
                        f"GET /{{waba_id}}/message_templates failed: {_describe(exc)}",
                        fix="check whatsapp.waba_id and the token scopes",
                        doc=DOC_INTEGRATIONS,
                        tenant=slug,
                    )
                )
        finally:
            await graph.aclose()
        checks.append(await self.whatsapp_webhook_check(settings, slug, phone_number_id, values))
        checks.append(
            Check(
                "whatsapp.reminders",
                "pass",
                "before go-live: payment method on the WABA, business verification, and re-check Meta's rate card before every price change",
                tenant=slug,
            )
        )
        return checks

    async def whatsapp_webhook_check(
        self, settings: Any, slug: str, phone_number_id: str, values: dict[str, str]
    ) -> Check:
        secret = values.get("wa_app_secret", "")
        if not secret:
            return Check(
                "whatsapp.webhook",
                "fail",
                "wa_app_secret missing: the webhook cannot verify signatures",
                fix=f"concierge secrets set {slug} wa_app_secret (App settings, Basic)",
                doc=DOC_INTEGRATIONS,
                tenant=slug,
            )
        base = settings.public_base_url()
        raw = json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "id": "doctor",
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "messaging_product": "whatsapp",
                                    "metadata": {"phone_number_id": phone_number_id},
                                    "statuses": [
                                        {
                                            "id": "wamid.doctor.probe",
                                            "status": "sent",
                                            "timestamp": str(int(time.time())),
                                            "recipient_id": "0",
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ).encode()
        async with self.client() as client:
            try:
                response = await client.post(
                    base + "/wa/webhook",
                    content=raw,
                    headers={
                        "content-type": "application/json",
                        "x-hub-signature-256": sign_meta(raw, secret),
                    },
                )
            except httpx2.HTTPError as exc:
                return Check(
                    "whatsapp.webhook",
                    "fail",
                    f"{base}/wa/webhook unreachable: {type(exc).__name__}",
                    fix="GC_PUBLIC_BASE_URL must reach this service; Meta posts to {GC_PUBLIC_BASE_URL}/wa/webhook",
                    doc=DOC_INTEGRATIONS,
                    tenant=slug,
                )
        if response.status_code != 200:
            return Check(
                "whatsapp.webhook",
                "fail",
                f"signed test webhook answered {response.status_code}",
                fix=f"the stored wa_app_secret does not match the app: concierge secrets set {slug} wa_app_secret",
                doc=DOC_INTEGRATIONS,
                tenant=slug,
            )
        return Check("whatsapp.webhook", "pass", "signed test webhook accepted", tenant=slug)

    async def availability_check(
        self, services: Any, tenant: Tenant, profile: Any, source: str
    ) -> Check:
        try:
            adapter = await services.adapters(source, tenant, profile)
        except Exception as exc:
            adapter = None
            error = _describe(exc)
        else:
            error = ""
        if adapter is None:
            return Check(
                f"availability.{source}",
                "fail" if error else "warn",
                f"{source} not usable: {error or 'not configured'}",
                fix=f"enter the {source} secrets with concierge secrets set {tenant.slug} <name> and the profile fields (INTEGRATIONS.md)",
                doc=DOC_INTEGRATIONS,
                tenant=tenant.slug,
            )
        try:
            report = await adapter.doctor()
        except Exception as exc:
            return Check(
                f"availability.{source}",
                "fail",
                f"{source} doctor failed: {_describe(exc)}",
                fix=f"check the {source} credentials and the property id in the profile",
                doc=DOC_INTEGRATIONS,
                tenant=tenant.slug,
            )
        detail = report.detail or ("ok" if report.ok else "failed")
        if report.warnings:
            detail += "; " + "; ".join(report.warnings[:4])
        if not report.ok:
            return Check(
                f"availability.{source}",
                "fail",
                detail,
                fix=f"check the {source} credentials (secrets) and the property or feed configuration",
                doc=DOC_INTEGRATIONS,
                tenant=tenant.slug,
            )
        return Check(
            f"availability.{source}",
            "warn" if report.warnings else "pass",
            detail,
            fix="review the warnings" if report.warnings else "",
            doc=DOC_INTEGRATIONS,
            tenant=tenant.slug,
        )

    async def stripe_webhook_check(
        self, client: httpx2.AsyncClient, base: str, settings: Any
    ) -> Check:
        secret = settings.stripe_webhook_secret.get_secret_value().encode("utf-8")
        raw = json.dumps({"id": "evt_doctor_probe", "type": "doctor.probe"}).encode()
        header = sign_stripe(raw, secret, int(self.now().timestamp()))
        try:
            response = await client.post(
                base + "/r/psp/webhook",
                content=raw,
                headers={"content-type": "application/json", "stripe-signature": header},
            )
        except httpx2.HTTPError as exc:
            return Check(
                "stripe.webhook",
                "fail",
                f"{base}/r/psp/webhook unreachable: {type(exc).__name__}",
                fix="GC_PUBLIC_BASE_URL must reach this service",
                doc=DOC_INTEGRATIONS,
            )
        if response.status_code != 200:
            return Check(
                "stripe.webhook",
                "fail",
                f"signed test event answered {response.status_code}",
                fix="STRIPE_WEBHOOK_SECRET must be the signing secret of the endpoint {GC_PUBLIC_BASE_URL}/r/psp/webhook",
                doc=DOC_INTEGRATIONS,
            )
        return Check("stripe.webhook", "pass", "signed test event accepted")

    async def retell_checks(
        self,
        client: httpx2.AsyncClient,
        base: str,
        settings: Any,
        tenant: Tenant,
        profile: Any,
        *,
        socket_secret: str = "",
    ) -> list[Check]:
        slug = tenant.slug
        checks: list[Check] = []
        api_key = settings.retell_api_key.get_secret_value()
        if not api_key:
            return [
                Check(
                    "retell",
                    "fail",
                    "RETELL_API_KEY is empty",
                    fix="set RETELL_API_KEY (the key with the webhook badge)",
                    doc=DOC_INTEGRATIONS,
                    tenant=slug,
                )
            ]
        raw = json.dumps({"event": "doctor_probe", "call": {"call_id": "doctor-probe"}}).encode()
        timestamp = int(self.now().timestamp() * 1000)
        try:
            response = await client.post(
                base + "/voice/retell/webhook",
                content=raw,
                headers={
                    "content-type": "application/json",
                    "x-retell-signature": sign_retell(raw, api_key, timestamp),
                },
            )
            if response.status_code in {401, 403}:
                checks.append(
                    Check(
                        "retell.signature",
                        "fail",
                        f"signed probe answered {response.status_code}",
                        fix="RETELL_API_KEY must be the key that signs the webhooks (webhook badge)",
                        doc=DOC_INTEGRATIONS,
                        tenant=slug,
                    )
                )
            else:
                checks.append(
                    Check("retell.signature", "pass", "signed probe accepted", tenant=slug)
                )
        except httpx2.HTTPError as exc:
            checks.append(
                Check(
                    "retell.signature",
                    "fail",
                    f"{base}/voice/retell/webhook unreachable: {type(exc).__name__}",
                    fix="GC_PUBLIC_BASE_URL must reach this service",
                    doc=DOC_INTEGRATIONS,
                    tenant=slug,
                )
            )
        ws_url = base.replace("https://", "wss://").replace("http://", "ws://")
        ws_url += f"/voice/retell/llm/{tenant.public_key}/{socket_secret or 'missing'}/doctor-probe"
        if not socket_secret:
            checks.append(
                Check(
                    "retell.wss",
                    "fail",
                    "no voice socket secret stored for the tenant",
                    fix=f"concierge voice provision {slug} (creates the secret and the agent)",
                    doc=DOC_INTEGRATIONS,
                    tenant=slug,
                )
            )
            return checks
        try:
            if self.ws_connect is not None:
                await self.ws_connect(ws_url)
            else:
                await _ws_probe(ws_url)
            checks.append(
                Check("retell.wss", "pass", "custom-LLM WebSocket reachable", tenant=slug)
            )
        except Exception as exc:
            checks.append(
                Check(
                    "retell.wss",
                    "fail",
                    f"WebSocket {ws_url} failed: {type(exc).__name__}",
                    fix="the proxy must pass WebSocket upgrades to /voice/retell/llm/ (Caddy does by default)",
                    doc=DOC_INTEGRATIONS,
                    tenant=slug,
                )
            )
        if not profile.voice.number:
            checks.append(
                Check(
                    "retell.number",
                    "fail",
                    "voice.number is empty",
                    fix="set voice.number to the imported number (concierge voice provision)",
                    doc=DOC_INTEGRATIONS,
                    tenant=slug,
                )
            )
        else:
            checks.append(Check("retell.number", "pass", "voice.number mapped", tenant=slug))
        checks.append(
            Check(
                "retell.reminders",
                "pass",
                "US transfer: DPA with SCCs signed and the transfer assessment recorded before go-live",
                tenant=slug,
            )
        )
        return checks

    async def twilio_checks(
        self, client: httpx2.AsyncClient, base: str, settings: Any, slug: str
    ) -> list[Check]:
        token = settings.twilio_auth_token.get_secret_value()
        if not token or not settings.twilio_account_sid:
            return [
                Check(
                    "twilio",
                    "fail",
                    "TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is empty",
                    fix="set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN from the Twilio console",
                    doc=DOC_INTEGRATIONS,
                    tenant=slug,
                )
            ]
        checks: list[Check] = []
        for path, name in (
            ("/voice/twilio/status", "twilio.signature"),
            ("/voice/twilio/incoming", "twilio.twiml"),
        ):
            url = base + path
            params = {
                "CallSid": "CAdoctorprobe",
                "CallStatus": "completed",
                "From": "+10000000000",
                "To": "+10000000000",
            }
            try:
                response = await client.post(
                    url,
                    data=params,
                    headers={"x-twilio-signature": sign_twilio(url, params, token)},
                )
            except httpx2.HTTPError as exc:
                checks.append(
                    Check(
                        name,
                        "fail",
                        f"{url} unreachable: {type(exc).__name__}",
                        fix="GC_PUBLIC_BASE_URL must reach this service",
                        doc=DOC_INTEGRATIONS,
                        tenant=slug,
                    )
                )
                continue
            if response.status_code in {401, 403}:
                checks.append(
                    Check(
                        name,
                        "fail",
                        f"signed probe answered {response.status_code}",
                        fix="TWILIO_AUTH_TOKEN must match the account that owns the number",
                        doc=DOC_INTEGRATIONS,
                        tenant=slug,
                    )
                )
            elif name == "twilio.twiml" and "xml" not in response.headers.get("content-type", ""):
                checks.append(
                    Check(
                        name,
                        "fail",
                        f"{url} did not answer TwiML ({response.status_code})",
                        fix="the number's voice webhook must point at {GC_PUBLIC_BASE_URL}/voice/twilio/incoming",
                        doc=DOC_INTEGRATIONS,
                        tenant=slug,
                    )
                )
            else:
                checks.append(Check(name, "pass", f"{url} accepted the signed probe", tenant=slug))
        return checks


async def _ws_probe(url: str) -> None:
    import websockets

    async with websockets.connect(url, open_timeout=PROBE_TIMEOUT_S):
        return


def _describe(exc: Exception) -> str:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f" {status}" if status else "")
