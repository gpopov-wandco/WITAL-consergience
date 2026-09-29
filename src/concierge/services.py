"""Composition root: every service wired once from the application context.

Implements: F01, F28, F32, 6.2

Adapters are injected here and nowhere else: the language model (Anthropic or the
scripted fake), the WhatsApp port (Graph client or the in-process fake). Tests build
a ``Services`` with fakes; the demo builds it with fakes and the mock Graph server;
production builds it from settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select

from concierge.agent.brain import Brain, ModelConfig
from concierge.agent.guardrails import Guardrails
from concierge.agent.prompting import PromptAssembler, PromptTexts, canary_for
from concierge.agent.responder import Responder
from concierge.agent.texts import GuestTexts, guest_texts
from concierge.channels.whatsapp.inbound import WhatsAppInbound
from concierge.channels.whatsapp.outbox import OutboxService, RetryableSend
from concierge.channels.whatsapp.port import WhatsAppPort
from concierge.channels.whatsapp.routes import SecretCache, WebhookIngress
from concierge.context import AppContext
from concierge.conversations.service import ConversationService
from concierge.db.models import Job, OperatorAlert, UsageEvent
from concierge.handoff.service import HandoffService
from concierge.jobs.service import JobService
from concierge.jobs.worker import JobWorker, RetryLater
from concierge.kb.store import KbService
from concierge.llm.port import LLMPort, Usage
from concierge.log import get_logger
from concierge.metering.prices import to_micro
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
        usage_sink=usage_sink_llm,
        alert_sink=alert_sink,
        demo_banner=demo,
    )
    outbox = OutboxService(
        ctx.db, ctx.clock, wa_port, secrets, jobs, ctx.keyring, alert_sink=alert_sink
    )
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

    worker.register("wa_inbound", handle_wa_inbound)
    worker.register("outbox_send", handle_outbox_send)
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
                ref=f"llm:{meta.get('request_id') or ''}:{ctx.clock.now().timestamp()}",
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
