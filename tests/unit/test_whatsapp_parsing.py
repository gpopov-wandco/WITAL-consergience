"""Webhook parsing, payload formatting within Meta's limits, error-code actions."""

from __future__ import annotations

from typing import Any

import pytest

from concierge.channels.whatsapp.errors import action_for
from concierge.channels.whatsapp.formatting import (
    buttons_payload,
    list_payload,
    parse_button_id,
    split_text,
    template_payload,
    text_payload,
)
from concierge.channels.whatsapp.webhook import parse_webhook

pytestmark = pytest.mark.feature("F14")


def _envelope(value: dict[str, Any], field: str = "messages") -> dict[str, Any]:
    value.setdefault("messaging_product", "whatsapp")
    value.setdefault("metadata", {"display_phone_number": "4969000000", "phone_number_id": "PN1"})
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{"field": field, "value": value}]}],
    }


def test_parse_text_button_list_media_location_unsupported() -> None:
    payload = _envelope(
        {
            "contacts": [{"profile": {"name": "Anna Muster"}, "wa_id": "4915112345678"}],
            "messages": [
                {
                    "from": "4915112345678",
                    "id": "wamid.1",
                    "timestamp": "1790000000",
                    "type": "text",
                    "text": {"body": "Hallo"},
                },
                {
                    "from": "4915112345678",
                    "id": "wamid.2",
                    "timestamp": "1790000001",
                    "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {"id": "gc:person", "title": "Mitarbeiter"},
                    },
                },
                {
                    "from": "4915112345678",
                    "id": "wamid.3",
                    "timestamp": "1790000002",
                    "type": "interactive",
                    "interactive": {
                        "type": "list_reply",
                        "list_reply": {"id": "gc:parking", "title": "Parken", "description": "x"},
                    },
                },
                {
                    "from": "4915112345678",
                    "id": "wamid.4",
                    "timestamp": "1790000003",
                    "type": "image",
                    "image": {
                        "id": "media-1",
                        "mime_type": "image/jpeg",
                        "sha256": "abc",
                        "caption": "mein Ausweis",
                    },
                },
                {
                    "from": "4915112345678",
                    "id": "wamid.5",
                    "timestamp": "1790000004",
                    "type": "location",
                    "location": {"latitude": 50.1, "longitude": 8.6, "name": "Messe"},
                },
                {
                    "from": "4915112345678",
                    "id": "wamid.6",
                    "timestamp": "1790000005",
                    "type": "unsupported",
                    "errors": [{"code": 131051, "title": "Unsupported message type"}],
                },
                {
                    "from": "4915112345678",
                    "id": "wamid.7",
                    "timestamp": "1790000006",
                    "type": "button",
                    "button": {"payload": "gc:eta", "text": "Ankunftszeit"},
                },
                {
                    "from": "4915112345678",
                    "id": "wamid.8",
                    "timestamp": "1790000007",
                    "type": "hologram",
                },
                "not a dict",
            ],
        }
    )
    changes = parse_webhook(payload)
    assert (
        len(changes) == 1 and changes[0].phone_number_id == "PN1" and changes[0].waba_id == "WABA1"
    )
    messages = changes[0].messages
    kinds = [m.kind for m in messages]
    assert kinds == [
        "text",
        "button",
        "list",
        "image",
        "location",
        "unsupported",
        "template_button",
        "unsupported",
    ]
    assert messages[0].text == "Hallo" and messages[0].profile_name == "Anna Muster"
    assert messages[1].button_id == "gc:person" and messages[1].text == "Mitarbeiter"
    assert messages[2].list_id == "gc:parking"
    assert (
        messages[3].media_id == "media-1"
        and messages[3].caption == "mein Ausweis"
        and messages[3].is_media
    )
    assert messages[4].location["latitude"] == 50.1
    assert messages[5].errors[0]["code"] == 131051
    assert messages[6].button_id == "gc:eta"
    assert messages[0].timestamp.year == 2026


def test_parse_statuses_and_template_updates() -> None:
    statuses = _envelope(
        {
            "statuses": [
                {
                    "id": "wamid.out",
                    "status": "delivered",
                    "timestamp": "1790000100",
                    "recipient_id": "4915112345678",
                    "conversation": {"id": "c1", "origin": {"type": "service"}},
                    "pricing": {
                        "billable": True,
                        "pricing_model": "PMP",
                        "category": "service",
                        "type": "regular",
                    },
                },
                {
                    "id": "wamid.out2",
                    "status": "failed",
                    "timestamp": "1790000101",
                    "recipient_id": "4915112345678",
                    "errors": [{"code": 131047}],
                },
            ]
        }
    )
    parsed = parse_webhook(statuses)[0]
    assert (
        parsed.statuses[0].billable is True
        and parsed.statuses[0].category == "service"
        and parsed.statuses[0].pricing_type == "regular"
    )
    assert parsed.statuses[1].status == "failed" and parsed.statuses[1].errors[0]["code"] == 131047
    update = parse_webhook(
        _envelope(
            {
                "event": "APPROVED",
                "message_template_id": 123,
                "message_template_name": "gc_prearrival_2d",
                "message_template_language": "de",
                "reason": None,
            },
            field="message_template_status_update",
        )
    )[0]
    assert (
        update.template_updates[0].event == "APPROVED"
        and update.template_updates[0].template_id == "123"
    )
    category = parse_webhook(
        _envelope(
            {
                "message_template_id": 123,
                "message_template_name": "gc_review_request",
                "message_template_language": "de",
                "previous_category": "UTILITY",
                "new_category": "MARKETING",
            },
            field="template_category_update",
        )
    )[0]
    assert (
        category.template_updates[0].event == "CATEGORY_UPDATE"
        and category.template_updates[0].category == "MARKETING"
    )


def test_parse_rejects_garbage() -> None:
    assert parse_webhook({"object": "page"}) == []
    assert parse_webhook("nope") == []
    assert (
        parse_webhook(
            {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": "x"}]}]}
        )
        == []
    )


def test_split_text_paragraphs_sentences_and_hard_cuts() -> None:
    short = "Hallo"
    assert split_text(short) == ["Hallo"]
    paragraphs = "\n\n".join(["Absatz " + str(i) + ". " + "x" * 300 for i in range(4)])
    parts = split_text(paragraphs)
    assert all(len(p) <= 700 for p in parts) and len(parts) == 2  # paragraphs merge pairwise
    sentences = " ".join(["Satz nummer " + str(i) + "." for i in range(120)])
    parts = split_text(sentences)
    assert all(len(p) <= 700 for p in parts) and "".join(parts).replace(
        " ", ""
    ) == sentences.replace(" ", "")
    wall = "y" * 9000
    parts = split_text(wall)
    assert [len(p) for p in parts] == [4096, 4096, 808]
    assert split_text("   ") == []


def test_buttons_list_template_and_ids() -> None:
    payload = buttons_payload(
        "491511",
        "Body",
        [
            ("person", "Mitarbeiter sprechen jetzt sofort"),
            ("book", "Buchen"),
            ("person", "dup"),
            ("more", "Mehr"),
            ("eta", "Zeit"),
        ],
    )
    buttons = payload["interactive"]["action"]["buttons"]
    assert (
        len(buttons) == 3
        and buttons[0]["reply"]["id"] == "gc:person"
        and len(buttons[0]["reply"]["title"]) == 20
    )
    assert [b["reply"]["id"] for b in buttons] == ["gc:person", "gc:book", "gc:more"]
    assert buttons_payload("1", "Body", [])["type"] == "text"
    lst = list_payload(
        "1",
        "Body",
        "Auswahl bitte hier",
        [("Themen", [(f"t{i}", "Titel " * 10, "Beschreibung " * 20) for i in range(12)])],
    )
    rows = lst["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 10 and len(rows[0]["title"]) == 24 and len(rows[0]["description"]) == 72
    assert lst["interactive"]["action"]["button"] == "Auswahl bitte hier"
    tpl = template_payload(
        "1",
        "gc_prearrival_2d",
        "de",
        [{"type": "body", "parameters": [{"type": "text", "text": "Anna"}]}],
    )
    assert tpl["template"]["language"] == {"code": "de"} and tpl["template"]["components"]
    assert text_payload("1", "x" * 5000)["text"]["body"] == "x" * 4096
    allowed = frozenset({"person", "book"})
    assert parse_button_id("gc:person", allowed) == "person"
    assert parse_button_id("gc:pay", allowed) is None
    assert parse_button_id("person", allowed) is None
    assert parse_button_id(None, allowed) is None


@pytest.mark.parametrize(
    ("code", "status", "action"),
    [
        (190, 401, "token_invalid"),
        (0, 401, "token_invalid"),
        (130429, 429, "backoff"),
        (131047, 400, "window_closed"),
        (131050, 400, "opted_out"),
        (132001, 400, "template_error"),
        (999999, 500, "retry"),
        (999999, 400, "fail"),
        (-1, None, "retry"),
        (131056, 400, "pair_rate"),
    ],
)
def test_error_actions(code: int, status: int | None, action: str) -> None:
    assert action_for(code, status) == action
