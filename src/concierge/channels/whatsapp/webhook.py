"""Parse WhatsApp webhook payloads into typed events (6.6).

Implements: 6.6, F14, F24

Handles text, interactive ``button_reply`` and ``list_reply``, media (image, audio,
video, document, sticker), location, contacts, reaction, order, system, template
``button`` and ``unsupported`` messages; message statuses with pricing; and
``message_template_status_update`` changes. Unknown types become ``unsupported``
events, never a crash. Guests are keyed on the ``from`` id (business-scoped user
id); the phone number is an optional attribute.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

MEDIA_TYPES = ("image", "audio", "video", "document", "sticker")


@dataclass
class InboundMessage:
    wamid: str
    from_id: str
    kind: str
    timestamp: datetime
    text: str = ""
    profile_name: str | None = None
    button_id: str | None = None
    button_title: str | None = None
    list_id: str | None = None
    media_id: str | None = None
    mime_type: str | None = None
    media_sha256: str | None = None
    caption: str | None = None
    location: dict[str, Any] | None = None
    context_wamid: str | None = None
    raw_type: str = ""
    errors: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def is_media(self) -> bool:
        return self.kind in MEDIA_TYPES


@dataclass
class StatusUpdate:
    wamid: str
    status: str
    timestamp: datetime
    recipient_id: str
    conversation_id: str | None = None
    category: str | None = None
    billable: bool | None = None
    pricing_model: str | None = None
    pricing_type: str | None = None
    errors: list[dict[str, Any]] = dataclasses.field(default_factory=list)


@dataclass
class TemplateStatusUpdate:
    name: str
    language: str
    event: str
    reason: str = ""
    template_id: str = ""
    category: str | None = None
    previous_category: str | None = None


@dataclass
class ParsedChange:
    phone_number_id: str
    display_phone_number: str
    waba_id: str
    field: str
    messages: list[InboundMessage] = dataclasses.field(default_factory=list)
    statuses: list[StatusUpdate] = dataclasses.field(default_factory=list)
    template_updates: list[TemplateStatusUpdate] = dataclasses.field(default_factory=list)


def _ts(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return datetime.now(UTC)


def _dict(value: Any) -> dict[str, Any]:
    """The value when it is a mapping, else an empty one (malformed pieces are skipped)."""
    return value if isinstance(value, dict) else {}


def parse_webhook(payload: Any) -> list[ParsedChange]:
    """Every change in every entry, in order; malformed pieces are skipped."""
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return []
    changes: list[ParsedChange] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        waba_id = str(entry.get("id", ""))
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            field_name = str(change.get("field", ""))
            metadata = _dict(value.get("metadata"))
            parsed = ParsedChange(
                phone_number_id=str(metadata.get("phone_number_id", "")),
                display_phone_number=str(metadata.get("display_phone_number", "")),
                waba_id=waba_id,
                field=field_name,
            )
            if field_name == "message_template_status_update":
                parsed.template_updates.append(
                    TemplateStatusUpdate(
                        name=str(value.get("message_template_name", "")),
                        language=str(value.get("message_template_language", "")),
                        event=str(value.get("event", "")),
                        reason=str(value.get("reason", "") or ""),
                        template_id=str(value.get("message_template_id", "")),
                    )
                )
                changes.append(parsed)
                continue
            if field_name == "template_category_update":
                parsed.template_updates.append(
                    TemplateStatusUpdate(
                        name=str(value.get("message_template_name", "")),
                        language=str(value.get("message_template_language", "")),
                        event="CATEGORY_UPDATE",
                        template_id=str(value.get("message_template_id", "")),
                        category=str(value.get("new_category", "")) or None,
                        previous_category=str(value.get("previous_category", "")) or None,
                    )
                )
                changes.append(parsed)
                continue
            names = _profile_names(value.get("contacts"))
            for message in value.get("messages") or []:
                if isinstance(message, dict):
                    parsed.messages.append(_message(message, names))
            for status in value.get("statuses") or []:
                if isinstance(status, dict):
                    parsed.statuses.append(_status(status))
            changes.append(parsed)
    return changes


def _profile_names(contacts: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for contact in contacts or []:
        if isinstance(contact, dict):
            wa_id = str(contact.get("wa_id", ""))
            profile = _dict(contact.get("profile"))
            if wa_id and profile.get("name"):
                names[wa_id] = str(profile["name"])[:200]
    return names


def _message(message: dict[str, Any], names: dict[str, str]) -> InboundMessage:
    raw_type = str(message.get("type", "unknown"))
    from_id = str(message.get("from", ""))
    item = InboundMessage(
        wamid=str(message.get("id", "")),
        from_id=from_id,
        kind="unsupported",
        timestamp=_ts(message.get("timestamp")),
        profile_name=names.get(from_id),
        raw_type=raw_type,
    )
    context = message.get("context")
    if isinstance(context, dict) and context.get("id"):
        item.context_wamid = str(context["id"])
    if raw_type == "text":
        body = (
            message.get("text", {}).get("body") if isinstance(message.get("text"), dict) else None
        )
        item.kind = "text"
        item.text = str(body or "")
    elif raw_type == "interactive":
        interactive = _dict(message.get("interactive"))
        subtype = str(interactive.get("type", ""))
        if subtype == "button_reply":
            reply = _dict(interactive.get("button_reply"))
            item.kind = "button"
            item.button_id = str(reply.get("id", ""))
            item.button_title = str(reply.get("title", ""))
            item.text = item.button_title
        elif subtype == "list_reply":
            reply = _dict(interactive.get("list_reply"))
            item.kind = "list"
            item.list_id = str(reply.get("id", ""))
            item.button_title = str(reply.get("title", ""))
            item.text = item.button_title
        else:
            item.kind = "unsupported"
    elif raw_type == "button":
        button = _dict(message.get("button"))
        item.kind = "template_button"
        item.button_id = str(button.get("payload", ""))
        item.button_title = str(button.get("text", ""))
        item.text = item.button_title
    elif raw_type in MEDIA_TYPES:
        media = _dict(message.get(raw_type))
        item.kind = raw_type
        item.media_id = str(media.get("id", "")) or None
        item.mime_type = str(media.get("mime_type", "")) or None
        item.media_sha256 = str(media.get("sha256", "")) or None
        item.caption = str(media.get("caption", "")) or None
        item.text = item.caption or ""
    elif raw_type == "location":
        location = _dict(message.get("location"))
        item.kind = "location"
        item.location = {k: location.get(k) for k in ("latitude", "longitude", "name", "address")}
    elif raw_type in {"contacts", "reaction", "order", "system"}:
        item.kind = raw_type
    elif raw_type == "unsupported":
        item.kind = "unsupported"
        errors = message.get("errors")
        if isinstance(errors, list):
            item.errors = [e for e in errors if isinstance(e, dict)]
    return item


def _status(status: dict[str, Any]) -> StatusUpdate:
    pricing = _dict(status.get("pricing"))
    conversation = _dict(status.get("conversation"))
    origin = _dict(conversation.get("origin"))
    errors = status.get("errors")
    return StatusUpdate(
        wamid=str(status.get("id", "")),
        status=str(status.get("status", "")),
        timestamp=_ts(status.get("timestamp")),
        recipient_id=str(status.get("recipient_id", "")),
        conversation_id=str(conversation.get("id", "")) or None,
        category=str(pricing.get("category") or origin.get("type") or "") or None,
        billable=bool(pricing["billable"]) if "billable" in pricing else None,
        pricing_model=str(pricing.get("pricing_model", "")) or None,
        pricing_type=str(pricing.get("type", "")) or None,
        errors=[e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else [],
    )
