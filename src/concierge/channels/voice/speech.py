"""Turn a chat reply into something a voice can read aloud (F26).

Implements: F26, F23

At most two short sentences, no lists, markdown, URLs or e-mail addresses; times and
prices in the spoken form of the reply language. Digits stay digits where every TTS
engine reads them correctly (Russian, Ukrainian, Turkish, Italian minutes).
"""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_TIME = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b(?:\s?Uhr)?")
_EUR_BEFORE = re.compile(r"(?:EUR|€)\s?(\d+(?:[.,]\d+)?)")
_EUR_AFTER = re.compile(r"(\d+(?:[.,]\d+)?)\s?(?:EUR|€)")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.M)
_MARKDOWN = re.compile(r"[*_`#]+")
_SPACES = re.compile(r"\s+")
_TRAILING_URL_INTRO = re.compile(r"[:\s]+$")

_EURO = {"de": "Euro", "en": "euros", "ru": "евро", "uk": "євро", "tr": "avro", "it": "euro"}


def spoken_time(hour: int, minute: int, lang: str) -> str:
    if lang == "de":
        return f"{hour} Uhr" if minute == 0 else f"{hour} Uhr {minute}"
    if lang == "en":
        suffix = "am" if hour < 12 else "pm"
        twelve = hour % 12 or 12
        return f"{twelve} {suffix}" if minute == 0 else f"{twelve}:{minute:02d} {suffix}"
    if lang == "it":
        return f"le {hour}" if minute == 0 else f"le {hour} e {minute}"
    if lang == "tr":
        return f"saat {hour}" if minute == 0 else f"saat {hour}.{minute:02d}"
    if lang == "ru":
        return f"{hour} часов" if minute == 0 else f"{hour}:{minute:02d}"
    if lang == "uk":
        return f"{hour} годин" if minute == 0 else f"{hour}:{minute:02d}"
    return f"{hour}:{minute:02d}"


def spoken_price(amount: str, lang: str) -> str:
    word = _EURO.get(lang, "Euro")
    if lang == "en" and amount in {"1", "1.00", "1,00"}:
        word = "euro"
    return f"{amount} {word}"


def for_speech(text: str, lang: str, *, website: str = "", max_sentences: int = 2) -> str:
    """Speech-ready text: no lists, markup, URLs or e-mails; spoken times and prices."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _BULLET.match(line) and stripped[-1] not in ".!?…:":
            stripped += "."  # a list item becomes a sentence of its own
        lines.append(_BULLET.sub("", stripped))
    cleaned = " ".join(lines)
    cleaned = _MARKDOWN.sub("", cleaned)
    cleaned = _URL.sub(website or "", cleaned)
    cleaned = _EMAIL.sub("", cleaned)
    cleaned = _EUR_BEFORE.sub(lambda m: spoken_price(m.group(1), lang), cleaned)
    cleaned = _EUR_AFTER.sub(lambda m: spoken_price(m.group(1), lang), cleaned)
    cleaned = _TIME.sub(lambda m: spoken_time(int(m.group(1)), int(m.group(2)), lang), cleaned)
    cleaned = cleaned.replace("\n", " ")
    cleaned = _SPACES.sub(" ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,;:])\s*([,.;:!?])", r"\2", cleaned)
    sentences = [s.strip() for s in _SENTENCE_END.split(cleaned) if s.strip()]
    kept = sentences[:max_sentences]
    return " ".join(kept)
