"""Composition root: every service wired once from the application context.

Implements: F01, F28, F32, 6.2

Adapters are injected here and nowhere else: the language model (Anthropic or the
scripted fake), the WhatsApp port (Graph client or the in-process fake). Tests build
a ``Services`` with fakes; the demo builds it with fakes and the mock Graph server;
production builds it from settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select

from concierge.agent.brain import Brain, ModelConfig
from concierge.agent.guardrails import Guardrails
from concierge.agent.prompting import PromptAssembler, PromptTexts, canary_for
from concierge.agent.responder import Responder
from concierge.agent.texts import GuestTexts, guest_texts
from concierge.availability.backends import ToolBackendBuilder
from concierge.availability.booking import InquiryService
from concierge.availability.chain import AvailabilityService
from concierge.availability.factory import AdapterFactory
from concierge.availability.manual import ManualInventoryService
from concierge.channels.web.service import WidgetService
from concierge.channels.whatsapp.inbound import WhatsAppInbound
from concierge.channels.whatsapp.outbox import OutboxService, RetryableSend
from concierge.channels.whatsapp.port import WhatsAppPort
from concierge.channels.whatsapp.routes import SecretCache, WebhookIngress
from concierge.clock import local_date, local_to_utc
from concierge.context import AppContext
from concierge.conversations.service import ConversationService
from concierge.db.models import Conversation, Job, OperatorAlert, Tenant, UsageEvent
from concierge.handoff.requests import RequestService
from concierge.handoff.service import HandoffService
from concierge.ids import new_id
from concierge.jobs.service import JobService
from concierge.jobs.worker import JobWorker, RetryLater
from concierge.journeys.service import JOB_KIND as JOURNEY_JOB_KIND
from concierge.journeys.service import JourneyEngine
from concierge.kb.store import KbService
from concierge.llm.port import LLMPort, Usage
from concierge.log import get_logger
from concierge.metering.prices import to_micro
from concierge.notify.port import EmailPort
from concierge.registration.proof import FakeProof, PaymentProofPort
from concierge.registration.service import RegistrationService
from concierge.reservations.service import ReservationService
from concierge.templates.service import TemplateService
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
    demo: bool = False
    alerts: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    async def close(self) -> None:
        await self.worker.stop()
        await self.ctx.close()


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

    async def usage_sink_llm(tenant_id: str, kind: str, usage: Usage, meta: dict[str, Any]) -> None:
        await record_llm_usage(ctx, tenant_id, usage, meta)

    async def usage_sink_wa(tenant_id: str, kind: str, details: dict[str, Any]) -> None:
        await record_whatsapp_usage(ctx, tenant_id, details)

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
        email_from=settings.gc_email_from,
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

    worker.register("wa_inbound", handle_wa_inbound)
    worker.register("outbox_send", handle_outbox_send)
    worker.register(JOURNEY_JOB_KIND, handle_journey_step)
    worker.register("registration_purge", handle_registration_purge)
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
    usd = float(meta.get("usd", 0.0))
    async with ctx.db.tenant_scope(tenant_id) as session:
        session.add(
            UsageEvent(
                tenant_id=tenant_id,
                kind="llm",
                category=str(meta.get("model", "")),
                quantity=usage.input_tokens
                + usage.output_tokens
                + usage.cache_write_tokens
                + usage.cache_read_tokens,
                unit="token",
                cost_micro_usd=to_micro(usd),
                cost_micro_eur=0,
                occurred_at=ctx.clock.now(),
                ref=f"llm:{meta.get('request_id') or new_id('req')}",
                meta={
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "cache_write": usage.cache_write_tokens,
                    "cache_read": usage.cache_read_tokens,
                    "inference_geo": usage.inference_geo or "",
                    "request_id": meta.get("request_id", ""),
                },
            )
        )
        await session.commit()


async def record_whatsapp_usage(ctx: AppContext, tenant_id: str, details: dict[str, Any]) -> None:
    """One usage event per billable status webhook (idempotent by ref)."""
    ref = str(details.get("ref", ""))
    occurred: datetime = details.get("occurred_at") or ctx.clock.now()
    async with ctx.db.tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(UsageEvent).where(UsageEvent.tenant_id == tenant_id, UsageEvent.ref == ref)
        )
        if existing is not None:
            return
        session.add(
            UsageEvent(
                tenant_id=tenant_id,
                kind="whatsapp",
                category=str(details.get("category", "")),
                quantity=1,
                unit="message",
                cost_micro_eur=0,
                occurred_at=occurred,
                ref=ref,
                meta={
                    "billable": bool(details.get("billable")),
                    "pricing_model": details.get("pricing_model", ""),
                    "pricing_type": details.get("pricing_type", ""),
                    "conversation_id": details.get("conversation_id", ""),
                    "priced": False,
                },
            )
        )
        await session.commit()


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
