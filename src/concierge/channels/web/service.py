"""Widget sessions and messages (F22).

Implements: F22, C7, C15, 4.2

A session is an unguessable token kept only in the visitor's ``sessionStorage``;
the server stores its SHA-256 as the guest's channel key. Opening a session records
the AI disclosure as the first message of the conversation. Every message goes
through the Brain and returns the whole validated reply (no token streaming).
Limits: per IP per minute and per session per hour from the tenant profile.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select

from concierge.agent.brain import Brain
from concierge.agent.texts import GuestTexts
from concierge.clock import Clock
from concierge.conversations.service import BOT_SILENT_STATUSES, ConversationService, Inbound
from concierge.db.models import Conversation, Message, MessageText, Tenant
from concierge.security.limiter import MovingWindowLimiter
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

LANGS = ("de", "en", "ru", "uk", "tr", "it")


class WidgetError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class WidgetSession:
    token: str
    lang: str
    disclosure: str
    conversation_id: str
    last_id: str = ""


@dataclass
class WidgetReply:
    messages: list[dict[str, Any]]
    lang: str
    handoff: bool
    mode: str
    events: list[str] = field(default_factory=list)
    last_id: str = ""


@dataclass
class WidgetUpdates:
    """Outbound messages newer than the id the widget saw last (staff replies, F19/F22)."""

    messages: list[dict[str, Any]]
    status: str
    handoff: bool
    last_id: str


class WidgetService:
    def __init__(
        self,
        *,
        tenants: TenantService,
        conversations: ConversationService,
        brain: Brain,
        texts: GuestTexts,
        clock: Clock,
        own_origin: str,
        limiter: MovingWindowLimiter | None = None,
    ) -> None:
        self.tenants = tenants
        self.conversations = conversations
        self.brain = brain
        self.texts = texts
        self.clock = clock
        self.own_origins: set[str] = {own_origin.rstrip("/")}
        self.limiter = limiter or MovingWindowLimiter()

    # ------------------------------------------------------------ tenants
    async def tenant_for_key(self, public_key: str | None) -> tuple[Tenant, TenantProfile]:
        tenant = await self.tenants.by_public_key(public_key or "")
        if tenant is None:
            raise WidgetError(404, "unknown_key", "unknown widget key")
        return tenant, TenantProfile.model_validate(tenant.profile)

    def origin_allowed(self, profile: TenantProfile, origin: str | None) -> bool:
        if not origin:
            return True  # same-origin fetches and non-browser clients send no Origin
        normalised = _normalise_origin(origin)
        if any(normalised == _normalise_origin(own) for own in self.own_origins):
            return True
        return any(normalised == _normalise_origin(o) for o in profile.widget.origins)

    def config(self, tenant: Tenant, profile: TenantProfile) -> dict[str, Any]:
        greeting = {
            lang: profile.widget.greeting.get(lang)
            or self.texts.render(
                "disclosure",
                lang,
                hotel=profile.name,
                privacy_url=profile.contacts.privacy_url or "",
            )
            for lang in profile.languages
        }
        return {
            "hotel": profile.name,
            "languages": list(profile.languages),
            "default_language": profile.default_language,
            "primary_color": profile.widget.primary_color,
            "position": profile.widget.position,
            "logo_text": profile.widget.logo_text or profile.branding.logo_text or profile.name,
            "language_menu": profile.widget.language_menu,
            "phone": profile.contacts.reception_phone,
            "email": profile.contacts.email,
            "privacy_url": profile.contacts.privacy_url,
            "greeting": greeting,
            "labels": {lang: _labels(lang) for lang in profile.languages},
            "buttons": {
                lang: {b: self.texts.button(b, lang) for b in ("person", "book", "privacy")}
                for lang in profile.languages
            },
        }

    # ----------------------------------------------------------- sessions
    def check_ip(self, profile: TenantProfile, ip: str) -> None:
        if not self.limiter.hit(
            f"ip:{ip}", f"{profile.limits.widget_requests_per_ip_per_minute}/minute"
        ):
            raise WidgetError(429, "rate_limited", "too many requests from this address")

    async def start_session(
        self, tenant: Tenant, profile: TenantProfile, *, lang: str | None
    ) -> WidgetSession:
        chosen = lang if lang in profile.languages else profile.default_language
        token = secrets.token_urlsafe(32)
        guest = await self.conversations.get_or_create_guest(
            tenant.id, "web", _hash(token), lang=chosen
        )
        conversation = await self.conversations.open_conversation(
            tenant.id, guest.id, "web", lang=chosen
        )
        disclosure = self.texts.render(
            "disclosure", chosen, hotel=profile.name, privacy_url=profile.contacts.privacy_url or ""
        )
        stored = await self.conversations.record_outbound(
            tenant.id,
            conversation.id,
            disclosure,
            lang=chosen,
            kind="disclosure",
            intent="A:disclosure",
            retention_days=profile.retention.chat_days,
            status="sent",
        )
        await self.conversations.mark_disclosed(tenant.id, conversation.id)
        await self.conversations.update(tenant.id, conversation.id, lang=chosen)
        return WidgetSession(
            token=token,
            lang=chosen,
            disclosure=disclosure,
            conversation_id=conversation.id,
            last_id=stored.id,
        )

    async def message(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        *,
        token: str,
        text: str,
        lang: str | None,
        button_id: str | None = None,
    ) -> WidgetReply:
        if not token or len(token) > 128:
            raise WidgetError(400, "bad_session", "missing session")
        key = _hash(token)
        if not self.limiter.hit(
            f"session:{key}", f"{profile.limits.widget_messages_per_session_per_hour}/hour"
        ):
            raise WidgetError(429, "rate_limited", "too many messages in this session")
        text = (text or "").strip()
        if not text:
            raise WidgetError(400, "empty", "message is empty")
        if len(text) > 2000:
            text = text[:2000]
        guest = await self.conversations.get_guest(tenant.id, "web", key)
        if guest is None:
            raise WidgetError(400, "bad_session", "unknown session")
        inbound = Inbound(
            channel="web",
            channel_key=key,
            text=text,
            kind="text",
            button_id=button_id,
            language_override=lang if lang in LANGS else None,
        )
        result = await self.brain.respond(tenant, inbound)
        messages = [
            {
                "text": m.text,
                "kind": m.kind,
                "lang": m.lang,
                "buttons": [{"id": b.id, "title": b.title} for b in m.buttons],
            }
            for m in result.messages
        ]
        ids = [m.message_id for m in result.messages if m.message_id]
        return WidgetReply(
            messages=messages,
            lang=result.language,
            handoff=result.handoff_id is not None,
            mode=result.mode,
            events=result.events,
            last_id=max(ids) if ids else "",
        )

    async def updates(
        self, tenant: Tenant, profile: TenantProfile, *, token: str, after: str
    ) -> WidgetUpdates:
        """Staff replies and other outbound messages the widget has not shown yet."""
        if not token or len(token) > 128:
            raise WidgetError(400, "bad_session", "missing session")
        key = _hash(token)
        guest = await self.conversations.get_guest(tenant.id, "web", key)
        if guest is None:
            raise WidgetError(400, "bad_session", "unknown session")
        async with self.conversations.db.tenant_scope(tenant.id) as session:
            conversation = await session.scalar(
                select(Conversation)
                .where(Conversation.guest_id == guest.id, Conversation.channel == "web")
                .order_by(Conversation.created_at.desc())
                .limit(1)
            )
            if conversation is None:
                raise WidgetError(400, "bad_session", "unknown session")
            query = (
                select(Message, MessageText)
                .join(MessageText, MessageText.id == Message.id)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.direction == "out",
                )
                .order_by(Message.id)
                .limit(50)
            )
            if after:
                query = query.where(Message.id > after[:64])
            rows = (await session.execute(query)).all()
            status = conversation.status
        messages = [
            {
                "id": m.id,
                "text": t.text,
                "kind": m.kind,
                "lang": m.lang,
                "role": "staff" if (m.intent or "").startswith("S:") else "bot",
            }
            for m, t in rows
        ]
        return WidgetUpdates(
            messages=messages,
            status=status,
            handoff=status in BOT_SILENT_STATUSES,
            last_id=str(messages[-1]["id"]) if messages else after,
        )


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalise_origin(origin: str) -> str:
    parts = urlsplit(origin.strip().lower())
    if not parts.scheme or not parts.netloc:
        return origin.strip().lower().rstrip("/")
    return f"{parts.scheme}://{parts.netloc}"


def _labels(lang: str) -> dict[str, str]:
    table = {
        "de": {
            "open": "Chat öffnen",
            "close": "Chat schließen",
            "ai": "KI-Assistent",
            "send": "Senden",
            "input": "Ihre Nachricht",
            "language": "Sprache",
            "privacy": "Datenschutz",
            "phone": "Telefon",
            "email": "E-Mail",
            "staff": "Ein Mitarbeiter übernimmt.",
            "error": "Verbindung fehlgeschlagen. Bitte erneut versuchen.",
        },
        "en": {
            "open": "Open chat",
            "close": "Close chat",
            "ai": "AI assistant",
            "send": "Send",
            "input": "Your message",
            "language": "Language",
            "privacy": "Privacy",
            "phone": "Phone",
            "email": "E-mail",
            "staff": "A member of staff takes over.",
            "error": "Connection failed. Please try again.",
        },
        "ru": {
            "open": "Открыть чат",
            "close": "Закрыть чат",
            "ai": "ИИ-ассистент",
            "send": "Отправить",
            "input": "Ваше сообщение",
            "language": "Язык",
            "privacy": "Конфиденциальность",
            "phone": "Телефон",
            "email": "E-mail",
            "staff": "Вопрос передан сотруднику.",
            "error": "Ошибка соединения. Попробуйте ещё раз.",
        },
        "uk": {
            "open": "Відкрити чат",
            "close": "Закрити чат",
            "ai": "ШІ-асистент",
            "send": "Надіслати",
            "input": "Ваше повідомлення",
            "language": "Мова",
            "privacy": "Конфіденційність",
            "phone": "Телефон",
            "email": "E-mail",
            "staff": "Запит передано працівникові.",
            "error": "Помилка з'єднання. Спробуйте ще раз.",
        },
        "tr": {
            "open": "Sohbeti aç",
            "close": "Sohbeti kapat",
            "ai": "Yapay zekâ asistanı",
            "send": "Gönder",
            "input": "Mesajınız",
            "language": "Dil",
            "privacy": "Gizlilik",
            "phone": "Telefon",
            "email": "E-posta",
            "staff": "Bir çalışan devralıyor.",
            "error": "Bağlantı kurulamadı. Lütfen tekrar deneyin.",
        },
        "it": {
            "open": "Apri la chat",
            "close": "Chiudi la chat",
            "ai": "Assistente IA",
            "send": "Invia",
            "input": "Il Suo messaggio",
            "language": "Lingua",
            "privacy": "Privacy",
            "phone": "Telefono",
            "email": "E-mail",
            "staff": "Un membro del personale prende in carico.",
            "error": "Connessione non riuscita. Riprovi.",
        },
    }
    return table.get(lang, table["en"])
