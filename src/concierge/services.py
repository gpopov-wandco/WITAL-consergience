"""Composition root: every service wired once from the application context.

Implements: F01, F28, F32, 6.2

Adapters are injected here and nowhere else: the language model (Anthropic or the
scripted fake), the WhatsApp port (Graph client or the in-process fake). Tests build
a ``Services`` with fakes; the demo builds it with fakes and the mock Graph server;
production builds it from settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any

from sqlalchemy import select

from concierge.admin.auth import AuthService
from concierge.agent.brain import Brain, ModelConfig
from concierge.agent.guardrails import Guardrails
from concierge.agent.prompting import PromptAssembler, PromptTexts, canary_for
from concierge.agent.responder import Responder
from concierge.agent.texts import GuestTexts, guest_texts
from concierge.availability.backends import ToolBackendBuilder
from concierge.availability.booking import InquiryForm, InquiryService
from concierge.availability.chain import AvailabilityService
from concierge.availability.factory import AdapterFactory
from concierge.availability.manual import ManualInventoryService
from concierge.channels.voice.postcall import PostCall
from concierge.channels.voice.session import VoiceEngine
from concierge.channels.web.service import WidgetService
from concierge.channels.whatsapp.inbound import WhatsAppInbound
from concierge.channels.whatsapp.outbox import OutboxService, RetryableSend
from concierge.channels.whatsapp.port import WhatsAppPort
from concierge.channels.whatsapp.routes import SecretCache, WebhookIngress
from concierge.clock import local_date, local_to_utc
from concierge.context import AppContext
from concierge.conversations.service import ConversationService
from concierge.db.models import Conversation, Job, OperatorAlert, Tenant
from concierge.gdpr.retention import RetentionService
from concierge.gdpr.service import GdprService
from concierge.handoff.requests import RequestService
from concierge.handoff.service import HandoffService
from concierge.handoff.timers import JOB_KIND as TIMERS_JOB_KIND
from concierge.handoff.timers import HandoffTimers
from concierge.jobs.service import JobService
from concierge.jobs.worker import JobWorker, RetryLater
from concierge.journeys.service import ETA_SILENCE_KIND, JourneyEngine
from concierge.journeys.service import JOB_KIND as JOURNEY_JOB_KIND
from concierge.kb.store import KbService
from concierge.legal.service import LegalService
from concierge.llm.port import LLMPort, Usage
from concierge.log import get_logger
from concierge.maintenance import (
    DIGEST_KIND,
    RECONCILE_KIND,
    RETENTION_KIND,
    MaintenanceScheduler,
)
from concierge.metering.service import MeteringService, month_of
from concierge.notify.alerts import StaffAlerts
from concierge.notify.email import build_email_port
from concierge.notify.operator import OperatorNotifier
from concierge.notify.outbox import JOB_KIND as EMAIL_JOB_KIND
from concierge.notify.outbox import EmailOutbox, RetryableEmail
from concierge.notify.port import EmailPort, FakeEmail
from concierge.registration.proof import FakeProof, PaymentProofPort
from concierge.registration.service import RegistrationService
from concierge.reports.analytics import AnalyticsService
from concierge.reports.qa import QaService
from concierge.reservations.service import ReservationService
from concierge.templates.service import TemplateService
from concierge.tenants.packs import PackService
from concierge.tenants.profile import TenantProfile
from concierge.tenants.secrets import SecretStore
from concierge.tenants.service import TenantService

log = get_logger(__name__)


@dataclass
class Services:
    ctx: AppContext
    llm: LLMPort | None
    wa_port: WhatsAppPort
    texts: GuestTexts
    tenants: TenantService
    secrets: SecretStore
    kb: KbService
    conversations: ConversationService
    handoffs: HandoffService
    jobs: JobService
    worker: JobWorker
    brain: Brain
    outbox: OutboxService
    wa_inbound: WhatsAppInbound
    secret_cache: SecretCache
    ingress: WebhookIngress
    widget: WidgetService
    availability: AvailabilityService
    adapters: AdapterFactory
    inventory: ManualInventoryService
    reservations: ReservationService
    requests: RequestService
    inquiries: InquiryService
    templates: TemplateService
    journeys: JourneyEngine
    email: EmailPort | None
    registration: RegistrationService
    email_outbox: EmailOutbox
    alerts_out: StaffAlerts
    timers: HandoffTimers
    auth: AuthService
    legal: LegalService
    packs: PackService
    voice: VoiceEngine
    postcall: PostCall
    metering: MeteringService
    retention: RetentionService
    gdpr: GdprService
    analytics: AnalyticsService
    qa: QaService
    operator_alerts: OperatorNotifier
    maintenance: MaintenanceScheduler
    demo: bool = False
    alerts: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    async def close(self) -> None:
        await self.worker.stop()
        await self.ctx.close()


DEMO_EMAIL_FROM = "concierge@hotel-beispiel.invalid"


def build_services(
    ctx: AppContext,
    *,
    llm: LLMPort | None,
    wa_port: WhatsAppPort,
    demo: bool = False,
    adapters: AdapterFactory | None = None,
    email: EmailPort | None = None,
    proof: PaymentProofPort | None = None,
) -> Services:
    settings = ctx.settings
    texts = guest_texts()
    if email is None:
        # the offline demo keeps e-mail in memory; production needs a configured provider
        email = FakeEmail() if demo else build_email_port(settings)
    email_from = settings.gc_email_from or (DEMO_EMAIL_FROM if demo else "")
    tenants = TenantService(ctx.db, ctx.clock)
    secrets = SecretStore(ctx.db, ctx.keyring)
    kb = KbService(ctx.db, ctx.clock, tz=settings.gc_tz_default)
    conversations = ConversationService(ctx.db, ctx.keyring, ctx.clock)
    handoffs = HandoffService(ctx.db, ctx.clock)
    jobs = JobService(ctx.db, ctx.clock)
    alerts: list[tuple[str, str, dict[str, Any]]] = []

    async def alert_sink(kind: str, tenant_id: str, details: dict[str, Any]) -> None:
        alerts.append((kind, tenant_id, details))
        await record_alert(ctx, kind, tenant_id, details)

    metering = MeteringService(ctx.db, ctx.clock)
    qa = QaService(ctx.db, ctx.clock)

    async def usage_sink_llm(tenant_id: str, kind: str, usage: Usage, meta: dict[str, Any]) -> None:
        await metering.record_llm(tenant_id, usage, meta)

    async def usage_sink_wa(tenant_id: str, kind: str, details: dict[str, Any]) -> None:
        tenant = await tenants.get_by_id(tenant_id)
        if tenant is not None:
            await metering.record_whatsapp(tenant_id, tenants.profile_of(tenant), details)

    async def qa_sink(tenant_id: str, conversation_id: str, result: Any) -> None:
        await qa.on_turn(
            tenant_id,
            conversation_id,
            events=list(result.events),
            handoff_id=result.handoff_id,
            message_id=next((m.message_id for m in result.messages if m.message_id), None),
        )

    adapter_factory = adapters or AdapterFactory(ctx.db, ctx.clock, secrets)
    availability = AvailabilityService(ctx.clock, adapter_factory, alert_sink=alert_sink)
    inventory = ManualInventoryService(ctx.db, ctx.clock)
    reservations = ReservationService(ctx.db, ctx.keyring, ctx.clock, jobs)
    requests = RequestService(ctx.db, ctx.clock, ctx.keyring)
    inquiries = InquiryService(ctx.db, ctx.clock, ctx.keyring, handoffs, availability, Guardrails())
    templates = TemplateService(ctx.db, ctx.clock, wa_port, secrets)
    backend_builder = ToolBackendBuilder(
        availability=availability,
        reservations=reservations,
        requests=requests,
        handoffs=handoffs,
        conversations=conversations,
        texts=texts,
    )

    brain = Brain(
        kb=kb,
        conversations=conversations,
        handoffs=handoffs,
        texts=texts,
        prompts=PromptAssembler(PromptTexts.load(), canary=canary_for(settings.signing_key())),
        responder=Responder(kb, texts),
        guardrails=Guardrails(),
        clock=ctx.clock,
        keyring=ctx.keyring,
        llm=llm,
        models=ModelConfig(
            primary=settings.gc_llm_model_primary,
            failover=settings.gc_llm_model_failover,
            cheap=settings.gc_llm_model_cheap,
            structured_outputs=settings.gc_llm_structured_outputs,
            turn_timeout_s=settings.gc_llm_turn_timeout_s,
        ),
        backends_factory=backend_builder,
        availability_lookup=backend_builder.responder_lookup,
        usage_sink=usage_sink_llm,
        qa_sink=qa_sink,
        alert_sink=alert_sink,
        demo_banner=demo,
    )
    outbox = OutboxService(
        ctx.db, ctx.clock, wa_port, secrets, jobs, ctx.keyring, alert_sink=alert_sink
    )
    journeys = JourneyEngine(
        db=ctx.db,
        clock=ctx.clock,
        jobs=jobs,
        tenants=tenants,
        reservations=reservations,
        templates=templates,
        outbox=outbox,
        conversations=conversations,
        handoffs=handoffs,
        requests=requests,
        kb=kb,
        keyring=ctx.keyring,
        texts=texts,
        email=email,
        email_from=email_from,
        alert_sink=alert_sink,
        public_base_url=settings.public_base_url(),
    )
    registration = RegistrationService(
        ctx.db,
        ctx.keyring,
        ctx.clock,
        tenants,
        signing_key=settings.signing_key(),
        public_base_url=settings.public_base_url(),
        proof=proof or FakeProof(settings.public_base_url()),
    )
    journeys.registration_link = registration.link_builder
    email_outbox = EmailOutbox(ctx.db, ctx.clock, jobs, email, sender=email_from)
    staff_alerts = StaffAlerts(
        clock=ctx.clock,
        templates=templates,
        wa_outbox=outbox,
        email_outbox=email_outbox,
        public_base_url=settings.public_base_url(),
    )
    timers = HandoffTimers(ctx.clock, jobs, tenants, handoffs, staff_alerts)
    auth = AuthService(ctx.db, ctx.clock, signing_key=settings.signing_key())
    legal = LegalService(ctx.db, ctx.clock)
    packs = PackService(ctx.db, tenants, kb, legal, templates)
    voice = VoiceEngine(
        db=ctx.db,
        clock=ctx.clock,
        keyring=ctx.keyring,
        brain=brain,
        conversations=conversations,
        tenants=tenants,
        requests=requests,
        legal=legal,
        texts=texts,
    )
    backend_builder.voice_hooks = voice
    postcall = PostCall(
        db=ctx.db,
        clock=ctx.clock,
        tenants=tenants,
        handoffs=handoffs,
        requests=requests,
        engine=voice,
    )

    async def reconcile_voice(tenant: Tenant) -> None:
        await postcall.reconcile(tenant)

    timers.extra.append(reconcile_voice)
    retention = RetentionService(ctx.db, ctx.clock)
    gdpr = GdprService(ctx.db, ctx.clock, ctx.keyring)
    analytics = AnalyticsService(ctx.db, ctx.clock)
    operator_alerts = OperatorNotifier(
        ctx.db,
        ctx.clock,
        email,
        sender=email_from,
        operator_email=settings.gc_operator_email,
        public_base_url=settings.public_base_url(),
        alert_sink=alert_sink,
    )
    price_check_day: list[date | None] = [None]

    async def operator_maintenance(tenant: Tenant) -> None:
        today = ctx.clock.now().date()
        if price_check_day[0] != today:
            price_check_day[0] = today
            await operator_alerts.check_price_tables(await metering.newest_price_dates())
        await operator_alerts.run()

    timers.extra.append(operator_maintenance)
    maintenance = MaintenanceScheduler(jobs, ctx.clock, tenants)

    async def run_retention(tenant: Tenant) -> None:
        await retention.run(tenant, tenants.profile_of(tenant))

    async def run_reconcile(tenant: Tenant) -> None:
        profile = tenants.profile_of(tenant)
        now = ctx.clock.now()
        year, month = month_of(now, profile.timezone)
        await metering.reconcile_whatsapp(tenant.id, profile, year, month)
        previous = now.replace(day=1) - timedelta(days=1)
        year, month = month_of(previous, profile.timezone)
        await metering.reconcile_whatsapp(tenant.id, profile, year, month)

    async def run_digest(tenant: Tenant) -> None:
        profile = tenants.profile_of(tenant)
        if not profile.contacts.email or not email_outbox.configured:
            return
        end = ctx.clock.now()
        start = end - timedelta(days=7)
        summary = await analytics.summary(tenant.id, profile, start, end)
        subject, text, html = analytics.digest(
            summary, profile, dashboard_url=f"{settings.public_base_url()}/admin/reports"
        )
        await email_outbox.enqueue(
            tenant.id,
            profile,
            to=profile.contacts.email,
            subject=subject,
            text=text,
            idempotency_key=f"digest:{tenant.id}:{end.isocalendar().year}-W{end.isocalendar().week:02d}",
            category="digest",
            html=html,
        )

    maintenance.register(RETENTION_KIND, run_retention)
    maintenance.register(RECONCILE_KIND, run_reconcile)
    maintenance.register(DIGEST_KIND, run_digest)

    async def alert_staff(tenant_id: str, handoff: Any) -> None:
        tenant = await tenants.get_by_id(tenant_id)
        if tenant is None:
            return
        await staff_alerts.on_handoff(tenant, tenants.profile_of(tenant), handoff)

    handoffs.on_created.append(alert_staff)
    brain.journey_replies = journeys
    brain.stop_hook = journeys.stop_for_subject

    async def record_upsell(
        tenant: Tenant, conversation: Conversation, upsell_id: str, lang: str
    ) -> str:
        row = await requests.create(
            tenant.id,
            conversation_id=conversation.id,
            kind="upsell",
            details={"upsell_id": upsell_id, "lang": lang, "channel": conversation.channel},
            reservation_id=conversation.verified_reservation_id,
            idempotency_key=f"upsell:{conversation.id}:{upsell_id}",
        )
        return row.id

    brain.upsell_request = record_upsell
    reservations.on_change(journeys.on_reservation_change)

    async def consent_from_reservation(tenant_id: str, reservation: Any, outcome: str) -> None:
        if (
            reservation.whatsapp_optin
            and reservation.optin_at is not None
            and reservation.phone_bidx
        ):
            await gdpr.record_consent(
                tenant_id,
                subject_bidx=reservation.phone_bidx,
                kind="whatsapp_journey",
                text_version=reservation.optin_wording_version or "unknown",
                given_at=reservation.optin_at,
                channel=reservation.optin_source or "reservation",
                guest_id=reservation.guest_id,
                reservation_id=reservation.id,
            )
        if (
            reservation.marketing_optin
            and reservation.optin_at is not None
            and reservation.phone_bidx
        ):
            await gdpr.record_consent(
                tenant_id,
                subject_bidx=reservation.phone_bidx,
                kind="marketing",
                text_version=reservation.optin_wording_version or "unknown",
                given_at=reservation.optin_at,
                channel=reservation.optin_source or "reservation",
                guest_id=reservation.guest_id,
                reservation_id=reservation.id,
            )

    reservations.on_change(consent_from_reservation)
    wa_inbound = WhatsAppInbound(
        db=ctx.db,
        clock=ctx.clock,
        brain=brain,
        conversations=conversations,
        outbox=outbox,
        port=wa_port,
        secrets=secrets,
        usage_sink=usage_sink_wa,
        alert_sink=alert_sink,
    )
    secret_cache = SecretCache(ctx.db, secrets, ctx.clock)
    ingress = WebhookIngress(ctx.db, ctx.clock, jobs, secret_cache)
    widget = WidgetService(
        tenants=tenants,
        conversations=conversations,
        brain=brain,
        texts=texts,
        clock=ctx.clock,
        own_origin=settings.public_base_url(),
    )

    async def on_dead(job: Job, error: str) -> None:
        await alert_sink(
            "dead_job",
            job.tenant_id or "",
            {"kind": job.kind, "job_id": job.id, "error": error[:200]},
        )

    worker = JobWorker(jobs, ctx.clock, on_dead=on_dead)

    async def handle_wa_inbound(job: Job) -> None:
        await wa_inbound.process_event(str(job.payload["event_id"]))

    async def handle_outbox_send(job: Job) -> None:
        try:
            await outbox.send(str(job.payload["tenant_id"]), str(job.payload["outbox_id"]))
        except RetryableSend as exc:
            raise RetryLater(f"retry in {exc.delay_s or 'backoff'} s") from exc

    async def handle_journey_step(job: Job) -> None:
        await journeys.run_step(job)

    async def handle_registration_purge(job: Job) -> None:
        await registration.purge()
        await schedule_registration_purge(jobs, ctx)

    async def handle_email_send(job: Job) -> None:
        try:
            await email_outbox.send(str(job.payload["tenant_id"]), str(job.payload["outbox_id"]))
        except RetryableEmail as exc:
            raise RetryLater(str(exc)) from exc

    async def handle_handoff_timers(job: Job) -> None:
        await timers.run(job)

    worker.register("wa_inbound", handle_wa_inbound)
    worker.register("outbox_send", handle_outbox_send)
    worker.register(JOURNEY_JOB_KIND, handle_journey_step)
    worker.register("registration_purge", handle_registration_purge)
    worker.register(EMAIL_JOB_KIND, handle_email_send)
    worker.register(TIMERS_JOB_KIND, handle_handoff_timers)
    worker.register(ETA_SILENCE_KIND, journeys.run_eta_silence)
    for kind in (RETENTION_KIND, RECONCILE_KIND, DIGEST_KIND):
        worker.register(kind, maintenance.run)
    reservations.on_eta.append(journeys.on_eta)

    async def inquiry_email(
        tenant: Tenant, profile: TenantProfile, form: InquiryForm, key: str
    ) -> bool:
        """Booking inquiry to the hotel's address with reply-to the guest (F10, F18)."""
        if not email_outbox.configured or not profile.contacts.email:
            return False
        lines = [
            f"Buchungsanfrage / booking inquiry ({form.lang})",
            f"Anreise / arrival: {form.stay.arrival.isoformat()}",
            f"Abreise / departure: {form.stay.departure.isoformat()}",
            f"Erwachsene / adults: {form.occupancy.adults}, Kinder / children: {form.occupancy.children}",
            f"Zimmer / room: {form.room_type or '-'}",
            f"Name: {form.name.strip()}",
            f"E-Mail: {form.email.strip() or '-'}",
            f"WhatsApp: {form.phone.strip() or '-'}",
            "",
            form.wish.strip() or "-",
        ]
        await email_outbox.enqueue(
            tenant.id,
            profile,
            to=profile.contacts.email,
            subject=(
                f"Buchungsanfrage / booking inquiry {form.stay.arrival.isoformat()} "
                f"bis/to {form.stay.departure.isoformat()}"
            ),
            text="\n".join(lines),
            idempotency_key=f"inquiry:{key[:24]}",
            reply_to=form.email.strip() or None,
            category="inquiry",
        )
        return True

    inquiries.notify = inquiry_email
    return Services(
        ctx=ctx,
        llm=llm,
        wa_port=wa_port,
        texts=texts,
        tenants=tenants,
        secrets=secrets,
        kb=kb,
        conversations=conversations,
        handoffs=handoffs,
        jobs=jobs,
        worker=worker,
        brain=brain,
        outbox=outbox,
        wa_inbound=wa_inbound,
        secret_cache=secret_cache,
        ingress=ingress,
        widget=widget,
        availability=availability,
        adapters=adapter_factory,
        inventory=inventory,
        reservations=reservations,
        requests=requests,
        inquiries=inquiries,
        templates=templates,
        journeys=journeys,
        email=email,
        registration=registration,
        email_outbox=email_outbox,
        alerts_out=staff_alerts,
        timers=timers,
        auth=auth,
        legal=legal,
        packs=packs,
        voice=voice,
        postcall=postcall,
        metering=metering,
        retention=retention,
        gdpr=gdpr,
        analytics=analytics,
        qa=qa,
        operator_alerts=operator_alerts,
        maintenance=maintenance,
        demo=demo,
        alerts=alerts,
    )


async def record_alert(ctx: AppContext, kind: str, tenant_id: str, details: dict[str, Any]) -> None:
    """Deduplicated operator alert rows (F32); e-mail delivery is the notifier's job."""
    key = f"{tenant_id}:{details.get('template', '')}{details.get('code', '')}{details.get('kind', '')}"[
        :200
    ]
    async with ctx.db.scope() as session:
        existing = await session.scalar(
            select(OperatorAlert).where(OperatorAlert.kind == kind, OperatorAlert.alert_key == key)
        )
        if existing is None:
            session.add(OperatorAlert(kind=kind, alert_key=key, tenant_id=tenant_id or None))
        else:
            existing.count += 1
            existing.resolved_at = None
        await session.commit()


async def record_llm_usage(
    ctx: AppContext, tenant_id: str, usage: Usage, meta: dict[str, Any]
) -> None:
    await MeteringService(ctx.db, ctx.clock).record_llm(tenant_id, usage, meta)


async def record_whatsapp_usage(ctx: AppContext, tenant_id: str, details: dict[str, Any]) -> None:
    """One usage event per billable status webhook (idempotent by ref)."""
    tenants = TenantService(ctx.db, ctx.clock)
    tenant = await tenants.get_by_id(tenant_id)
    if tenant is None:
        return
    await MeteringService(ctx.db, ctx.clock).record_whatsapp(
        tenant_id, tenants.profile_of(tenant), details
    )


async def schedule_registration_purge(jobs: JobService, ctx: AppContext) -> None:
    """One purge job per day (dedupe by date), run shortly after local midnight."""
    now = ctx.clock.now()
    tomorrow = local_date(now, ctx.settings.gc_tz_default) + timedelta(days=1)
    run_at = local_to_utc(tomorrow, time(1, 0), ctx.settings.gc_tz_default)
    await jobs.enqueue(
        "registration_purge",
        {},
        run_at=run_at,
        dedupe_key=f"maintenance:registration_purge:{tomorrow.isoformat()}",
        max_attempts=3,
    )
