"""Guest and conversation persistence shared by every channel.

Implements: F01, F17, F23, F25, C7, C8, 4.2, 6.6

Guests are keyed per channel (WhatsApp business-scoped user id, widget session token,
keyed hash of the caller number); phone, name and e-mail are stored encrypted with
blind indexes. Message text lives in ``message_text`` with an expiry from the tenant's
retention settings. One in-process lock per conversation serialises turns.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from concierge.clock import Clock
from concierge.db.models import Conversation, Guest, Message, MessageText
from concierge.db.session import Database
from concierge.security.crypto import Keyring

STATUS_RANK = {
    "received": 0,
    "queued": 1,
    "sending": 2,
    "sent": 3,
    "delivered": 4,
    "read": 5,
    "failed": 9,
}
BOT_SILENT_STATUSES = frozenset({"needs_staff", "staff_active"})
OPEN_STATUSES = frozenset(
    {"new", "bot_handling", "needs_staff", "staff_active", "waiting_guest", "snoozed"}
)


@dataclass
class Inbound:
    channel: str
    channel_key: str
    text: str
    kind: str = "text"
    external_id: str | None = None
    occurred_at: datetime | None = None
    button_id: str | None = None
    language_override: str | None = None
    phone: str | None = None
    profile_name: str | None = None
    wa_user_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ConversationService:
    def __init__(self, db: Database, keyring: Keyring, clock: Clock) -> None:
        self.db = db
        self.keyring = keyring
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    # -------------------------------------------------------------- guests
    async def get_or_create_guest(
        self,
        tenant_id: str,
        channel: str,
        channel_key: str,
        *,
        phone: str | None = None,
        name: str | None = None,
        lang: str = "de",
        wa_user_id: str | None = None,
    ) -> Guest:
        async with self.db.tenant_scope(tenant_id) as session:
            guest = await session.scalar(
                select(Guest).where(
                    Guest.tenant_id == tenant_id,
                    Guest.channel == channel,
                    Guest.channel_key == channel_key,
                )
            )
            now = self.clock.now()
            if guest is None:
                guest = Guest(
                    tenant_id=tenant_id, channel=channel, channel_key=channel_key, lang=lang
                )
                session.add(guest)
            guest.last_seen_at = now
            if wa_user_id:
                guest.wa_user_id = wa_user_id
            if phone and guest.phone_enc is None:
                guest.phone_enc = self.keyring.encrypt_str(phone, tenant_id, "phone")
                guest.phone_bidx = self.keyring.blind_index(phone, tenant_id, "phone")
            if name and guest.name_enc is None:
                guest.name_enc = self.keyring.encrypt_str(name, tenant_id, "name")
                guest.name_bidx = self.keyring.blind_index(name, tenant_id, "name")
            await session.commit()
            await session.refresh(guest)
            return guest

    async def get_guest(self, tenant_id: str, channel: str, channel_key: str) -> Guest | None:
        async with self.db.tenant_scope(tenant_id) as session:
            return await session.scalar(
                select(Guest).where(
                    Guest.tenant_id == tenant_id,
                    Guest.channel == channel,
                    Guest.channel_key == channel_key,
                )
            )

    def guest_phone(self, guest: Guest) -> str | None:
        if guest.phone_enc is None:
            return None
        return self.keyring.decrypt_str(guest.phone_enc, guest.tenant_id, "phone")

    # ------------------------------------------------------- conversations
    async def open_conversation(
        self, tenant_id: str, guest_id: str, channel: str, *, lang: str
    ) -> Conversation:
        async with self.db.tenant_scope(tenant_id) as session:
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.guest_id == guest_id,
                    Conversation.channel == channel,
                    Conversation.status.in_(OPEN_STATUSES),
                )
                .order_by(Conversation.created_at.desc())
            )
            if conversation is None:
                conversation = Conversation(
                    tenant_id=tenant_id, guest_id=guest_id, channel=channel, lang=lang
                )
                session.add(conversation)
                await session.commit()
                await session.refresh(conversation)
            return conversation

    async def get(self, tenant_id: str, conversation_id: str) -> Conversation | None:
        async with self.db.tenant_scope(tenant_id) as session:
            return await session.get(Conversation, conversation_id)

    async def update(self, tenant_id: str, conversation_id: str, **values: Any) -> Conversation:
        async with self.db.tenant_scope(tenant_id) as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise LookupError("conversation not found")
            for key, value in values.items():
                setattr(conversation, key, value)
            await session.commit()
            await session.refresh(conversation)
            return conversation

    async def merge_meta(
        self, tenant_id: str, conversation_id: str, **values: Any
    ) -> dict[str, Any]:
        async with self.db.tenant_scope(tenant_id) as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise LookupError("conversation not found")
            meta = dict(conversation.meta)
            meta.update(values)
            conversation.meta = meta
            await session.commit()
            return meta

    async def mark_disclosed(self, tenant_id: str, conversation_id: str) -> None:
        await self.update(tenant_id, conversation_id, ai_disclosed_at=self.clock.now())

    async def verify_attempt_state(self, tenant_id: str, conversation_id: str) -> int:
        """Attempts in the current hour (resets after an hour)."""
        async with self.db.tenant_scope(tenant_id) as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise LookupError("conversation not found")
            now = self.clock.now()
            if (
                conversation.verify_attempts_reset_at is None
                or conversation.verify_attempts_reset_at <= now
            ):
                conversation.verify_attempts = 0
                conversation.verify_attempts_reset_at = now + timedelta(hours=1)
                await session.commit()
            return int(conversation.verify_attempts)

    async def set_verify_attempts(
        self, tenant_id: str, conversation_id: str, attempts: int
    ) -> None:
        await self.update(tenant_id, conversation_id, verify_attempts=attempts)

    # ------------------------------------------------------------ messages
    async def record_inbound(
        self,
        tenant_id: str,
        conversation_id: str,
        inbound: Inbound,
        *,
        lang: str,
        retention_days: int,
    ) -> Message:
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            message = Message(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                direction="in",
                kind=inbound.kind,
                wamid=inbound.external_id,
                status="received",
                status_rank=0,
                lang=lang,
                meta={
                    k: v
                    for k, v in inbound.meta.items()
                    if k in {"button_id", "list_id", "media_type", "location"}
                },
                occurred_at=inbound.occurred_at or now,
                created_at=now,
            )
            session.add(message)
            await session.flush()
            session.add(
                MessageText(
                    id=message.id,
                    tenant_id=tenant_id,
                    text=inbound.text,
                    expires_at=now + timedelta(days=retention_days),
                )
            )
            conversation = await session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.last_inbound_at = now
                conversation.lang = lang
            await session.commit()
            await session.refresh(message)
            return message

    async def record_outbound(
        self,
        tenant_id: str,
        conversation_id: str,
        text: str,
        *,
        lang: str,
        kind: str = "text",
        intent: str | None = None,
        meta: dict[str, Any] | None = None,
        retention_days: int,
        status: str = "queued",
    ) -> Message:
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            message = Message(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                direction="out",
                kind=kind,
                status=status,
                status_rank=STATUS_RANK.get(status, 0),
                lang=lang,
                intent=intent,
                meta=meta or {},
                occurred_at=now,
                created_at=now,
            )
            session.add(message)
            await session.flush()
            session.add(
                MessageText(
                    id=message.id,
                    tenant_id=tenant_id,
                    text=text,
                    expires_at=now + timedelta(days=retention_days),
                )
            )
            conversation = await session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.last_outbound_at = now
            await session.commit()
            await session.refresh(message)
            return message

    async def history(
        self, tenant_id: str, conversation_id: str, *, limit: int = 12
    ) -> list[tuple[str, str]]:
        """Text-only turns, oldest first: (role, text)."""
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(Message, MessageText)
                    .join(MessageText, MessageText.id == Message.id)
                    .where(
                        Message.tenant_id == tenant_id, Message.conversation_id == conversation_id
                    )
                    .order_by(Message.occurred_at.desc(), Message.created_at.desc())
                    .limit(limit)
                )
            ).all()
        turns = [("user" if m.direction == "in" else "assistant", t.text) for m, t in rows]
        turns.reverse()
        return turns

    async def apply_status(self, tenant_id: str, wamid: str, status: str) -> Message | None:
        """Highest status rank wins; ``failed`` is terminal."""
        async with self.db.tenant_scope(tenant_id) as session:
            message = await session.scalar(
                select(Message).where(Message.tenant_id == tenant_id, Message.wamid == wamid)
            )
            if message is None:
                return None
            rank = STATUS_RANK.get(status, 0)
            if message.status == "failed":
                return message
            if rank > message.status_rank or status == "failed":
                message.status = status
                message.status_rank = rank
                await session.commit()
                await session.refresh(message)
            return message
