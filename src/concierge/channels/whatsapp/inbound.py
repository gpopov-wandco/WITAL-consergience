"""Process stored webhook events: messages through the Brain, statuses, templates.

Implements: 6.6, F01, F14, F24, C7, C8, C9

Per stored event: resolve the tenant, build an ``Inbound`` (media is looked up and
downloaded at once for size and digest checks, then discarded; ID and card images
are never kept), extend the 24-hour service window, send read and typing indicators
only when a reply will follow, run the Brain under its lock, format the replies
within Meta's limits and queue them in the outbox. Status webhooks update message
state (highest rank wins, ``failed`` terminal) and write usage events from
``pricing`` (never estimated). Template status updates change the registry.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from concierge.agent.brain import Brain, Outbound
from concierge.channels.whatsapp.formatting import (
    buttons_payload,
    parse_button_id,
    split_text,
    text_payload,
)
from concierge.channels.whatsapp.outbox import OutboxService
from concierge.channels.whatsapp.port import GraphError, WhatsAppPort
from concierge.channels.whatsapp.webhook import (
    InboundMessage,
    ParsedChange,
    StatusUpdate,
    TemplateStatusUpdate,
    parse_webhook,
)
from concierge.clock import Clock
from concierge.conversations.service import BOT_SILENT_STATUSES, ConversationService, Inbound
from concierge.db.models import Conversation, Template, Tenant, WebhookEvent
from concierge.db.session import Database
from concierge.log import bind_context, get_logger
from concierge.tenants.secrets import SecretStore

log = get_logger(__name__)

UsageSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
AlertSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
BUTTON_IDS = frozenset(
    {
        "book",
        "person",
        "eta",
        "directions",
        "parking",
        "breakfast",
        "yes",
        "no",
        "more",
        "verify",
        "privacy",
        "eta_before_18",
        "eta_18_22",
        "eta_after_22",
        "stay_ok",
        "stay_issue",
        "upsell_yes",
        "upsell_no",
    }
)
MEDIA_MAX_BYTES = 16 * 1024 * 1024
SERVICE_WINDOW = timedelta(hours=24)


class WhatsAppInbound:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        brain: Brain,
        conversations: ConversationService,
        outbox: OutboxService,
        port: WhatsAppPort,
        secrets: SecretStore,
        usage_sink: UsageSink | None = None,
        alert_sink: AlertSink | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.brain = brain
        self.conversations = conversations
        self.outbox = outbox
        self.port = port
        self.secrets = secrets
        self.usage_sink = usage_sink
        self.alert_sink = alert_sink

    # ------------------------------------------------------------- events
    async def process_event(self, event_id: str) -> str:
        """Handle one stored webhook event; returns the outcome for the job log."""
        async with self.db.scope() as session:
            event = await session.get(WebhookEvent, event_id)
            if event is None:
                return "missing"
            if event.status == "processed":
                return "duplicate"
            tenant = await session.get(Tenant, event.tenant_id) if event.tenant_id else None
            payload = dict(event.payload)
        if tenant is None:
            await self._mark(event_id, "ignored", "no tenant")
            return "ignored"
        bind_context(tenant=tenant.id)
        outcome = "processed"
        try:
            changes = parse_webhook(payload)
            for change in changes:
                for message in change.messages:
                    await self._handle_message(tenant, change, message)
                for status in change.statuses:
                    await self._handle_status(tenant, status)
                for update in change.template_updates:
                    await self._handle_template_update(tenant, update)
        except Exception as exc:
            await self._mark(event_id, "failed", f"{type(exc).__name__}: {exc}")
            raise
        await self._mark(event_id, outcome, None)
        return outcome

    async def _mark(self, event_id: str, status: str, error: str | None) -> None:
        async with self.db.scope() as session:
            event = await session.get(WebhookEvent, event_id)
            if event is not None:
                event.status = status
                event.processed_at = self.clock.now()
                event.error = error[:500] if error else None
                await session.commit()

    # ----------------------------------------------------------- messages
    async def _handle_message(
        self, tenant: Tenant, change: ParsedChange, message: InboundMessage
    ) -> None:
        token = await self.secrets.get(tenant.id, "wa_access_token") or ""
        kind = message.kind
        text = message.text
        media_note: dict[str, Any] = {}
        if message.is_media and message.media_id:
            media_note = await self._inspect_media(tenant, message, token)
        button_id = None
        if kind in {"button", "list"}:
            button_id = parse_button_id(message.button_id or message.list_id, BUTTON_IDS)
            if button_id is None:
                kind = "text"
            else:
                text = message.button_title or button_id
        if kind == "template_button":
            kind = "text"
            text = message.button_title or text
        if kind in {"contacts", "reaction", "order", "system"}:
            if kind in {"reaction", "system"}:
                return
            kind = "unsupported"
        phone = message.from_id if message.from_id.isdigit() else None
        inbound = Inbound(
            channel="whatsapp",
            channel_key=message.from_id,
            text=text,
            kind=kind if kind not in {"button", "list"} else "text",
            external_id=message.wamid,
            occurred_at=message.timestamp,
            button_id=button_id,
            phone=("+" + phone) if phone else None,
            profile_name=message.profile_name,
            wa_user_id=message.from_id,
            meta={"button_id": button_id, **media_note},
        )
        guest = await self.conversations.get_or_create_guest(
            tenant.id,
            "whatsapp",
            message.from_id,
            phone=inbound.phone,
            name=inbound.profile_name,
            wa_user_id=message.from_id,
        )
        conversation = await self.conversations.open_conversation(
            tenant.id, guest.id, "whatsapp", lang=guest.lang
        )
        await self.conversations.update(
            tenant.id, conversation.id, window_expires_at=message.timestamp + SERVICE_WINDOW
        )
        will_reply = conversation.status not in BOT_SILENT_STATUSES or not conversation.meta.get(
            "staff_ack_sent"
        )
        if will_reply and tenant.phone_number_id and token:
            try:
                await self.port.mark_read(
                    tenant.phone_number_id, message.wamid, access_token=token, typing=True
                )
            except GraphError as exc:
                log.info("whatsapp.mark_read_failed", code=exc.code)
        result = await self.brain.respond(tenant, inbound)
        for index, outbound in enumerate(result.messages):
            await self._queue_outbound(tenant, result.conversation_id, message, outbound, index)

    async def _queue_outbound(
        self,
        tenant: Tenant,
        conversation_id: str,
        message: InboundMessage,
        outbound: Outbound,
        index: int,
    ) -> None:
        parts = split_text(outbound.text)
        for part_index, part in enumerate(parts):
            last = part_index == len(parts) - 1
            if last and outbound.buttons:
                payload = buttons_payload(
                    message.from_id, part, [(b.id, b.title) for b in outbound.buttons]
                )
            else:
                payload = text_payload(message.from_id, part)
            key = f"reply:{message.wamid}:{index}:{part_index}"
            await self.outbox.enqueue(
                tenant.id,
                payload=payload,
                idempotency_key=key,
                conversation_id=conversation_id,
                category="service",
                message_id=outbound.message_id,
            )

    async def _inspect_media(
        self, tenant: Tenant, message: InboundMessage, token: str
    ) -> dict[str, Any]:
        """Look up and download media at once (URL lives five minutes); keep nothing."""
        note: dict[str, Any] = {"media_type": message.kind, "media_ok": False}
        if not message.media_id:
            return note
        try:
            info = await self.port.media_info(message.media_id, access_token=token)
            if info.file_size and info.file_size > MEDIA_MAX_BYTES:
                note["media_error"] = "too_large"
                return note
            data = await self.port.download_media(
                info.url, access_token=token, max_bytes=MEDIA_MAX_BYTES
            )
        except GraphError as exc:
            note["media_error"] = f"graph:{exc.code}"
            return note
        digest = hashlib.sha256(data).hexdigest()
        if (
            message.media_sha256
            and message.media_sha256 != digest
            and message.media_sha256 != _b64_of_hex(digest)
        ):
            note["media_error"] = "digest_mismatch"
            return note
        note["media_ok"] = True
        note["media_bytes"] = len(data)
        return note

    # ----------------------------------------------------------- statuses
    async def _handle_status(self, tenant: Tenant, status: StatusUpdate) -> None:
        message = await self.conversations.apply_status(tenant.id, status.wamid, status.status)
        if message is None:
            await self.outbox.reconcile_unknown(
                tenant.id, status.wamid, status.recipient_id, status.status
            )
        if status.status == "failed":
            log.info(
                "whatsapp.delivery_failed",
                wamid_suffix=status.wamid[-8:],
                errors=len(status.errors),
            )
        if status.billable is not None and self.usage_sink is not None:
            await self.usage_sink(
                tenant.id,
                "whatsapp",
                {
                    "ref": f"wa:{status.wamid}:{status.status}",
                    "category": status.category or "",
                    "billable": status.billable,
                    "pricing_model": status.pricing_model or "",
                    "pricing_type": status.pricing_type or "",
                    "occurred_at": status.timestamp,
                    "conversation_id": status.conversation_id or "",
                },
            )

    # ---------------------------------------------------------- templates
    async def _handle_template_update(self, tenant: Tenant, update: TemplateStatusUpdate) -> None:
        async with self.db.tenant_scope(tenant.id) as session:
            row = await session.scalar(
                select(Template).where(
                    Template.tenant_id == tenant.id,
                    Template.name == update.name,
                    Template.lang == _lang_from_code(update.language),
                )
            )
            if row is None:
                return
            if update.event == "CATEGORY_UPDATE":
                row.meta_category = update.category
                row.status_reason = (
                    f"category changed from {update.previous_category} to {update.category}"
                )
                await session.commit()
                if self.alert_sink is not None:
                    await self.alert_sink(
                        "template_category_changed",
                        tenant.id,
                        {
                            "template": update.name,
                            "lang": row.lang,
                            "category": update.category or "",
                        },
                    )
                return
            row.status = update.event.upper()
            row.status_reason = update.reason or None
            row.last_status_at = self.clock.now()
            if update.template_id:
                row.meta_template_id = update.template_id
            await session.commit()
        if (
            update.event.upper() in {"REJECTED", "PAUSED", "DISABLED"}
            and self.alert_sink is not None
        ):
            await self.alert_sink(
                "template_" + update.event.lower(),
                tenant.id,
                {"template": update.name, "lang": update.language, "reason": update.reason},
            )


def _lang_from_code(code: str) -> str:
    return code.split("_")[0].lower() if code else "en"


def _b64_of_hex(hex_digest: str) -> str:
    import base64

    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


async def conversation_for_wamid(db: Database, tenant_id: str, wamid: str) -> Conversation | None:
    from concierge.db.models import Message

    async with db.tenant_scope(tenant_id) as session:
        message = await session.scalar(
            select(Message).where(Message.tenant_id == tenant_id, Message.wamid == wamid)
        )
        if message is None:
            return None
        return await session.get(Conversation, message.conversation_id)
