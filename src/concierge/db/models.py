"""ORM models for every table (6.4 of the build prompt).

Implements: F31, F05, F12, F13, F14, F15, F17, F24, F25, F26, C15, C16

Guest name, phone and e-mail are stored encrypted (``*_enc``) with a keyed blind index
(``*_bidx``) for equality lookups. Message text lives in ``message_text`` so that
retention can drop it independently. Unique keys include ``tenant_id``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from concierge.db.base import AppendOnly, Base, TenantScoped, Timestamped, id_column, utcnow

# --------------------------------------------------------------------- tenants


class Tenant(Base, Timestamped):
    __tablename__ = "tenant"

    id: Mapped[str] = id_column("ten")
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Berlin")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="demo")
    ai_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="full")
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    kill_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    profile: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    public_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    phone_number_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    waba_id: Mapped[str | None] = mapped_column(String(64))
    display_phone_number: Mapped[str | None] = mapped_column(String(32), unique=True)
    voice_agent_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    voice_number: Mapped[str | None] = mapped_column(String(32), unique=True)
    activated_at: Mapped[datetime | None]
    dpa_signed_at: Mapped[datetime | None]
    subprocessors_acknowledged_at: Mapped[datetime | None]


class TenantSecret(Base, TenantScoped, Timestamped):
    __tablename__ = "tenant_secret"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_secret_name"),)

    id: Mapped[str] = id_column("sec")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)


class User(Base, Timestamped):
    """Hotel owners and staff (tenant-bound) and WITALCO operators (tenant_id NULL)."""

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email_lower", name="uq_user_tenant_email"),
        Index(
            "ux_user_operator_email",
            "email_lower",
            unique=True,
            sqlite_where=text("tenant_id IS NULL"),
            postgresql_where=text("tenant_id IS NULL"),
        ),
    )

    id: Mapped[str] = id_column("usr")
    tenant_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("tenant.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    email_lower: Mapped[str] = mapped_column(String(254), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    locale: Mapped[str] = mapped_column(String(5), nullable=False, default="de")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None]
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    invite_expires_at: Mapped[datetime | None]
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    last_login_at: Mapped[datetime | None]
    whatsapp_alert_number_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    alerts_opted_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LoginSession(Base):
    __tablename__ = "login_session"

    id: Mapped[str] = id_column("ses")
    user_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("tenant.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    ip_ref: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class ApiKey(Base, TenantScoped, Timestamped):
    __tablename__ = "api_key"

    id: Mapped[str] = id_column("key")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scopes: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    revoked_at: Mapped[datetime | None]


class SystemSetting(Base):
    __tablename__ = "system_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


# ---------------------------------------------------------------------- guests


class Guest(Base, TenantScoped, Timestamped):
    __tablename__ = "guest"
    __table_args__ = (
        UniqueConstraint("tenant_id", "channel", "channel_key", name="uq_guest_channel_key"),
        Index("ix_guest_phone_bidx", "tenant_id", "phone_bidx"),
        Index("ix_guest_email_bidx", "tenant_id", "email_bidx"),
    )

    id: Mapped[str] = id_column("gst")
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_key: Mapped[str] = mapped_column(String(128), nullable=False)
    wa_user_id: Mapped[str | None] = mapped_column(String(64))
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    phone_bidx: Mapped[str | None] = mapped_column(String(40))
    name_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    name_bidx: Mapped[str | None] = mapped_column(String(40))
    email_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    email_bidx: Mapped[str | None] = mapped_column(String(40))
    lang: Mapped[str] = mapped_column(String(5), nullable=False, default="de")
    restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_seen_at: Mapped[datetime | None]
    erased_at: Mapped[datetime | None]


class Conversation(Base, TenantScoped, Timestamped):
    __tablename__ = "conversation"
    __table_args__ = (
        Index("ix_conversation_status", "tenant_id", "status"),
        Index("ix_conversation_guest", "tenant_id", "guest_id"),
    )

    id: Mapped[str] = id_column("conv")
    guest_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("guest.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    priority: Mapped[str | None] = mapped_column(String(2))
    lang: Mapped[str] = mapped_column(String(5), nullable=False, default="de")
    window_expires_at: Mapped[datetime | None]
    ai_disclosed_at: Mapped[datetime | None]
    verified_reservation_id: Mapped[str | None] = mapped_column(String(40))
    verify_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verify_attempts_reset_at: Mapped[datetime | None]
    last_inbound_at: Mapped[datetime | None]
    last_outbound_at: Mapped[datetime | None]
    unresolved_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    redirect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upsell_offered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stopped_at: Mapped[datetime | None]
    assigned_user_id: Mapped[str | None] = mapped_column(String(40))
    snoozed_until: Mapped[datetime | None]
    voice_call_id: Mapped[str | None] = mapped_column(String(40))
    meta: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)


class Message(Base, TenantScoped):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("tenant_id", "wamid", name="uq_message_wamid"),
        Index("ix_message_conversation", "tenant_id", "conversation_id", "occurred_at"),
    )

    id: Mapped[str] = id_column("msg")
    conversation_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="text")
    wamid: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")
    status_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lang: Mapped[str | None] = mapped_column(String(5))
    intent: Mapped[str | None] = mapped_column(String(48))
    meta: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class MessageText(Base, TenantScoped):
    __tablename__ = "message_text"

    id: Mapped[str] = mapped_column(
        String(40), ForeignKey("message.id", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False, index=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_event"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "event_key", name="uq_webhook_event_key"),
        Index("ix_webhook_event_received", "received_at"),
    )

    id: Mapped[str] = id_column("evt")
    tenant_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("tenant.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    received_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    processed_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(String(500))


class Outbox(Base, TenantScoped, Timestamped):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_outbox_idempotency"),
        Index("ix_outbox_due", "state", "next_attempt_at"),
    )

    id: Mapped[str] = id_column("out")
    conversation_id: Mapped[str | None] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None]
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="service")
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(String(300))
    sent_at: Mapped[datetime | None]
    message_id: Mapped[str | None] = mapped_column(String(40))


class Handoff(Base, TenantScoped, Timestamped):
    __tablename__ = "handoff"
    __table_args__ = (Index("ix_handoff_open", "tenant_id", "status", "sla_due_at"),)

    id: Mapped[str] = id_column("hand")
    conversation_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    priority: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    trigger: Mapped[str] = mapped_column(String(48), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sla_due_at: Mapped[datetime | None]
    sla_breached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_alert_at: Mapped[datetime | None]
    acknowledged_by: Mapped[str | None] = mapped_column(String(40))
    acknowledged_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    assigned_user_id: Mapped[str | None] = mapped_column(String(40))


class GuestRequest(Base, TenantScoped, Timestamped):
    """Requests the assistant records for staff (early check-in, invoice, callback)."""

    __tablename__ = "guest_request"

    id: Mapped[str] = id_column("req")
    conversation_id: Mapped[str | None] = mapped_column(String(40))
    reservation_id: Mapped[str | None] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    idempotency_key: Mapped[str | None] = mapped_column(String(160))


class ConversationNote(Base, TenantScoped):
    __tablename__ = "conversation_note"

    id: Mapped[str] = id_column("note")
    conversation_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(40), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


# ------------------------------------------------------------------- knowledge


class KbEntry(Base, TenantScoped, Timestamped):
    __tablename__ = "kb_entry"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", "lang", "version", name="uq_kb_entry_version"),
        Index("ix_kb_entry_lookup", "tenant_id", "key", "lang", "status"),
    )

    id: Mapped[str] = id_column("kb")
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    lang: Mapped[str] = mapped_column(String(5), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    truth: Mapped[str] = mapped_column(String(24), nullable=False, default="authoritative_kb")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    sensitivity: Mapped[str] = mapped_column(String(24), nullable=False, default="public")
    review_due: Mapped[date | None] = mapped_column(Date)
    volatile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approver: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None]


class KbGap(Base, TenantScoped, Timestamped):
    """Unanswered questions collected for the gap report."""

    __tablename__ = "kb_gap"

    id: Mapped[str] = id_column("gap")
    question: Mapped[str] = mapped_column(String(600), nullable=False)
    lang: Mapped[str] = mapped_column(String(5), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    conversation_id: Mapped[str | None] = mapped_column(String(40))


class QuestionnaireAnswer(Base, TenantScoped, Timestamped):
    __tablename__ = "questionnaire_answer"
    __table_args__ = (UniqueConstraint("tenant_id", "item_key", name="uq_questionnaire_item"),)

    id: Mapped[str] = id_column("qa")
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)


class LegalText(Base, TenantScoped, Timestamped):
    __tablename__ = "legal_text"
    __table_args__ = (UniqueConstraint("tenant_id", "key", "lang", name="uq_legal_text"),)

    id: Mapped[str] = id_column("legal")
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    lang: Mapped[str] = mapped_column(String(5), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft_unreviewed")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None]


class Template(Base, TenantScoped, Timestamped):
    __tablename__ = "template"
    __table_args__ = (UniqueConstraint("tenant_id", "name", "lang", name="uq_template_name_lang"),)

    id: Mapped[str] = id_column("tpl")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    lang: Mapped[str] = mapped_column(String(5), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    header: Mapped[str | None] = mapped_column(String(60))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    footer: Mapped[str | None] = mapped_column(String(60))
    buttons: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    variables: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    samples: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    meta_template_id: Mapped[str | None] = mapped_column(String(64))
    meta_category: Mapped[str | None] = mapped_column(String(16))
    last_status_at: Mapped[datetime | None]
    status_reason: Mapped[str | None] = mapped_column(String(300))


# ---------------------------------------------------------------- reservations


class Reservation(Base, TenantScoped, Timestamped):
    __tablename__ = "reservation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "res_id", name="uq_reservation_res_id"),
        Index("ix_reservation_arrival", "tenant_id", "arrival"),
        Index("ix_reservation_phone_bidx", "tenant_id", "phone_bidx"),
    )

    id: Mapped[str] = id_column("res")
    res_id: Mapped[str] = mapped_column(String(64), nullable=False)
    guest_id: Mapped[str | None] = mapped_column(String(40))
    name_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    name_bidx: Mapped[str | None] = mapped_column(String(40))
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    phone_bidx: Mapped[str | None] = mapped_column(String(40))
    email_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    email_bidx: Mapped[str | None] = mapped_column(String(40))
    lang: Mapped[str] = mapped_column(String(5), nullable=False, default="de")
    arrival: Mapped[date] = mapped_column(Date, nullable=False)
    departure: Mapped[date] = mapped_column(Date, nullable=False)
    room_type: Mapped[str | None] = mapped_column(String(64))
    rate_plan: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="booked")
    adults: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    children: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    whatsapp_optin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    optin_source: Mapped[str | None] = mapped_column(String(64))
    optin_at: Mapped[datetime | None]
    optin_wording_version: Mapped[str | None] = mapped_column(String(32))
    marketing_optin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    marketing_optin_at: Mapped[datetime | None]
    eta: Mapped[time | None] = mapped_column(Time)
    eta_set_at: Mapped[datetime | None]
    key_safe_sent_at: Mapped[datetime | None]
    journey_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled_at: Mapped[datetime | None]
    meta: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)


class RoomType(Base, TenantScoped, Timestamped):
    __tablename__ = "room_type"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_room_type_code"),)

    id: Mapped[str] = id_column("rt")
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    names: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    units_total: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_occupancy: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    base_price_cents: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_ids: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)


class DayInventory(Base, TenantScoped):
    __tablename__ = "day_inventory"
    __table_args__ = (
        UniqueConstraint("tenant_id", "room_type_id", "date", name="uq_day_inventory"),
    )

    id: Mapped[str] = id_column("inv")
    room_type_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("room_type.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    units_available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_stay: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    closed_arrival: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    closed_departure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    price_cents: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(40), nullable=False, default="")


class RateRule(Base, TenantScoped, Timestamped):
    __tablename__ = "rate_rule"

    id: Mapped[str] = id_column("rr")
    room_type_id: Mapped[str | None] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)


# ------------------------------------------------------------------------ jobs


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (Index("ix_job_due", "status", "run_at"),)

    id: Mapped[str] = id_column("job")
    tenant_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("tenant.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    run_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    lease_until: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(String(500))
    worker_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
    done_at: Mapped[datetime | None]


# -------------------------------------------------------------------- metering


class UsageEvent(Base, TenantScoped, AppendOnly):
    __tablename__ = "usage_event"
    __table_args__ = (
        UniqueConstraint("tenant_id", "ref", name="uq_usage_event_ref"),
        Index("ix_usage_event_time", "tenant_id", "kind", "occurred_at"),
    )

    id: Mapped[str] = id_column("use")
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="message")
    cost_micro_eur: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_micro_usd: Mapped[int | None] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    ref: Mapped[str | None] = mapped_column(String(200))
    meta: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class PriceTable(Base, Timestamped):
    __tablename__ = "price_table"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "region", "category", "effective_from", name="uq_price_table_row"
        ),
    )

    id: Mapped[str] = id_column("price")
    tenant_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("tenant.id", ondelete="CASCADE"), index=True
    )
    region: Mapped[str] = mapped_column(String(8), nullable=False, default="DE")
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    price_micro_eur: Mapped[int] = mapped_column(Integer, nullable=False)
    free_allowance_per_month: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(40), nullable=False, default="")


class ModelPrice(Base, Timestamped):
    __tablename__ = "model_price"
    __table_args__ = (UniqueConstraint("model", "effective_from", name="uq_model_price"),)

    id: Mapped[str] = id_column("mp")
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    input_micro_usd_per_mtok: Mapped[int] = mapped_column(Integer, nullable=False)
    output_micro_usd_per_mtok: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_write_micro_usd_per_mtok: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_read_micro_usd_per_mtok: Mapped[int] = mapped_column(Integer, nullable=False)
    source_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")


class FxRate(Base, Timestamped):
    __tablename__ = "fx_rate"
    __table_args__ = (UniqueConstraint("base", "quote", "effective_from", name="uq_fx_rate"),)

    id: Mapped[str] = id_column("fx")
    base: Mapped[str] = mapped_column(String(3), nullable=False)
    quote: Mapped[str] = mapped_column(String(3), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    rate_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False, default="")


# ------------------------------------------------------------------------ GDPR


class Consent(Base, TenantScoped, AppendOnly):
    __tablename__ = "consent"
    __table_args__ = (Index("ix_consent_subject", "tenant_id", "subject_bidx", "kind"),)

    id: Mapped[str] = id_column("cons")
    guest_id: Mapped[str | None] = mapped_column(String(40))
    reservation_id: Mapped[str | None] = mapped_column(String(40))
    subject_bidx: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    text_version: Mapped[str] = mapped_column(String(32), nullable=False)
    given_at: Mapped[datetime] = mapped_column(nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    source_page: Mapped[str | None] = mapped_column(String(300))
    ip_ref: Mapped[str | None] = mapped_column(String(32))
    withdrawn_at: Mapped[datetime | None]
    evidence: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Suppression(Base, TenantScoped):
    __tablename__ = "suppression"
    __table_args__ = (
        UniqueConstraint("tenant_id", "channel", "subject_bidx", name="uq_suppression_subject"),
    )

    id: Mapped[str] = id_column("sup")
    subject_bidx: Mapped[str] = mapped_column(String(40), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class AuditLog(Base, AppendOnly):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_tenant_time", "tenant_id", "created_at"),)

    id: Mapped[str] = id_column("aud")
    tenant_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("tenant.id"))
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    details: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class DeletionLog(Base, AppendOnly):
    __tablename__ = "deletion_log"

    id: Mapped[str] = id_column("del")
    tenant_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("tenant.id"))
    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class GdprRequest(Base, TenantScoped, Timestamped):
    __tablename__ = "gdpr_request"

    id: Mapped[str] = id_column("gdpr")
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_bidx: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    due_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None]
    tasks: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    export_ref: Mapped[str | None] = mapped_column(String(200))


# ---------------------------------------------------------------- registration


class Meldeschein(Base, TenantScoped):
    __tablename__ = "meldeschein"

    id: Mapped[str] = id_column("mld")
    reservation_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    data_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    arrival: Mapped[date] = mapped_column(Date, nullable=False)
    departure: Mapped[date] = mapped_column(Date, nullable=False)
    confirmed_at: Mapped[datetime | None]
    proof_id: Mapped[str | None] = mapped_column(String(40))
    delete_after: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    deleted_at: Mapped[datetime | None]


class PaymentProof(Base, TenantScoped):
    __tablename__ = "payment_proof"

    id: Mapped[str] = id_column("proof")
    meldeschein_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    session_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    assignment_number_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    confirmed_at: Mapped[datetime | None]


# ----------------------------------------------------------------------- voice


class VoiceCall(Base, TenantScoped, Timestamped):
    __tablename__ = "voice_call"
    __table_args__ = (UniqueConstraint("provider", "call_id", name="uq_voice_call_id"),)

    id: Mapped[str] = id_column("call")
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    caller_ref: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    conversation_id: Mapped[str | None] = mapped_column(String(40))
    lang: Mapped[str] = mapped_column(String(5), nullable=False, default="de")
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    duration_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    billable_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_cost_micro_eur: Mapped[int | None] = mapped_column(Integer)
    disclosure_spoken_at: Mapped[datetime | None]
    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_expires_at: Mapped[datetime | None]
    summary_de: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reconciled_at: Mapped[datetime | None]


class CallbackTask(Base, TenantScoped, Timestamped):
    __tablename__ = "callback_task"

    id: Mapped[str] = id_column("cb")
    voice_call_id: Mapped[str | None] = mapped_column(String(40))
    conversation_id: Mapped[str | None] = mapped_column(String(40))
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    phone_bidx: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    lang: Mapped[str] = mapped_column(String(5), nullable=False, default="de")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    due_at: Mapped[datetime | None]
    done_at: Mapped[datetime | None]


# -------------------------------------------------------------------------- QA


class QaReview(Base, TenantScoped, Timestamped):
    __tablename__ = "qa_review"

    id: Mapped[str] = id_column("qa")
    conversation_id: Mapped[str] = mapped_column(String(40), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    reviewer: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolved_at: Mapped[datetime | None]


class OperatorAlert(Base):
    __tablename__ = "operator_alert"
    __table_args__ = (UniqueConstraint("kind", "alert_key", name="uq_operator_alert"),)

    id: Mapped[str] = id_column("alert")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    alert_key: Mapped[str] = mapped_column(String(200), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("tenant.id"))
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    last_sent_at: Mapped[datetime | None]
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resolved_at: Mapped[datetime | None]


GUEST_DATA_TABLES: tuple[type[Base], ...] = (
    Guest,
    Conversation,
    Message,
    MessageText,
    WebhookEvent,
    Outbox,
    Handoff,
    GuestRequest,
    ConversationNote,
    KbGap,
    Reservation,
    Consent,
    Suppression,
    GdprRequest,
    Meldeschein,
    PaymentProof,
    VoiceCall,
    CallbackTask,
    QaReview,
)
"""Every table that can hold guest data; the GDPR registry test checks export and erasure cover them."""
