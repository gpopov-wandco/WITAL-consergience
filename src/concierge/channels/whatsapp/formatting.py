"""Build Graph ``messages`` payloads within Meta's limits (6.6 Sending).

Implements: 6.6, F14, F22

Text at most 4,096 characters, split at paragraph borders and preferably kept
under 700; at most 3 reply buttons with titles up to 20 characters; list rows at
most 10 with titles up to 24 and descriptions up to 72 characters. Button ids encode
an intent (``gc:<id>``) and are validated when they come back.
"""

from __future__ import annotations

import re
from typing import Any

TEXT_MAX = 4096
TEXT_PREFERRED = 700
BUTTON_MAX = 3
BUTTON_TITLE_MAX = 20
LIST_ROWS_MAX = 10
LIST_ROW_TITLE_MAX = 24
LIST_ROW_DESC_MAX = 72
LIST_BUTTON_MAX = 20
BUTTON_PREFIX = "gc:"
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def split_text(text: str, *, max_len: int = TEXT_MAX, preferred: int = TEXT_PREFERRED) -> list[str]:
    """Split long text at paragraph borders, then sentences, then hard cuts."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= preferred:
        return [text]
    parts: list[str] = []
    current = ""
    for paragraph in re.split(r"\n{2,}", text):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= preferred:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if len(paragraph) <= preferred:
            current = paragraph
            continue
        for sentence in _SENTENCE_END.split(paragraph):
            candidate = f"{current} {sentence}" if current else sentence
            if len(candidate) <= preferred:
                current = candidate
                continue
            if current:
                parts.append(current)
            while len(sentence) > max_len:
                parts.append(sentence[:max_len])
                sentence = sentence[max_len:]
            current = sentence
    if current:
        parts.append(current)
    return [p for p in parts if p]


def text_payload(
    to: str, body: str, *, reply_to: str | None = None, preview_url: bool = False
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "to": to,
        "type": "text",
        "text": {"body": body[:TEXT_MAX], "preview_url": preview_url},
    }
    if reply_to:
        payload["context"] = {"message_id": reply_to}
    return payload


def buttons_payload(to: str, body: str, buttons: list[tuple[str, str]]) -> dict[str, Any]:
    """Reply buttons (at most 3, titles up to 20 characters); ids get the ``gc:`` prefix."""
    if not buttons:
        return text_payload(to, body)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for button_id, title in buttons:
        if len(items) >= BUTTON_MAX:
            break
        clean_id = _clean_id(button_id)
        if clean_id in seen:
            continue
        seen.add(clean_id)
        items.append(
            {
                "type": "reply",
                "reply": {
                    "id": BUTTON_PREFIX + clean_id,
                    "title": title.strip()[:BUTTON_TITLE_MAX],
                },
            }
        )
    return {
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {"buttons": items},
        },
    }


def list_payload(
    to: str,
    body: str,
    button_title: str,
    sections: list[tuple[str, list[tuple[str, str, str]]]],
) -> dict[str, Any]:
    """List message (at most 10 rows in total)."""
    total = 0
    out_sections = []
    for title, rows in sections:
        out_rows = []
        for row_id, row_title, description in rows:
            if total >= LIST_ROWS_MAX:
                break
            total += 1
            row: dict[str, Any] = {
                "id": BUTTON_PREFIX + _clean_id(row_id),
                "title": row_title.strip()[:LIST_ROW_TITLE_MAX],
            }
            if description:
                row["description"] = description.strip()[:LIST_ROW_DESC_MAX]
            out_rows.append(row)
        if out_rows:
            out_sections.append({"title": title.strip()[:LIST_ROW_TITLE_MAX], "rows": out_rows})
    return {
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:1024]},
            "action": {"button": button_title.strip()[:LIST_BUTTON_MAX], "sections": out_sections},
        },
    }


def template_payload(
    to: str, name: str, language: str, components: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    template: dict[str, Any] = {"name": name, "language": {"code": language}}
    if components:
        template["components"] = components
    return {"to": to, "type": "template", "template": template}


def parse_button_id(raw: str | None, allowed: frozenset[str]) -> str | None:
    """The intent id behind a returned button or row id, or ``None`` when not ours."""
    if not raw or not raw.startswith(BUTTON_PREFIX):
        return None
    candidate = raw[len(BUTTON_PREFIX) :]
    return candidate if candidate in allowed else None


def _clean_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_:\-]", "", value)[:60]
    return cleaned or "x"


def language_code(lang: str) -> str:
    """Template language codes Meta expects for the six languages."""
    return {"de": "de", "en": "en", "ru": "ru", "uk": "uk", "tr": "tr", "it": "it"}.get(lang, "en")
