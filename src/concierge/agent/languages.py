"""Language detection, stickiness and script checks (F23).

Implements: F23, 4.3, 8.5

``lingua-language-detector`` is limited to the six supported languages. The
conversation language is sticky: short messages ("ok", "danke") keep the previous
language, a confident detection on a longer message switches it. Replies are checked
for the right script by words: Ukrainian must never use ы э ъ ё, Russian must not use
і ї є ґ, and on longer Cyrillic texts the detector must not name the other language;
Latin-script languages must be mostly Latin. URLs, e-mail addresses, tokens with
digits and placeholders are ignored so that Wi-Fi names or links never trip the check.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from lingua import Language, LanguageDetectorBuilder

LANGS: tuple[str, ...] = ("de", "en", "ru", "uk", "tr", "it")
_LINGUA = {
    "de": Language.GERMAN,
    "en": Language.ENGLISH,
    "ru": Language.RUSSIAN,
    "uk": Language.UKRAINIAN,
    "tr": Language.TURKISH,
    "it": Language.ITALIAN,
}
_CODES = {v: k for k, v in _LINGUA.items()}
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
_UK_ONLY = set("іїєґІЇЄҐ")
_RU_ONLY = set("ыэъёЫЭЪЁ")
_SHORT_ACKS = {
    "ok",
    "okay",
    "ja",
    "nein",
    "danke",
    "yes",
    "no",
    "thanks",
    "thank you",
    "да",
    "нет",
    "спасибо",
    "так",
    "ні",
    "дякую",
    "evet",
    "hayır",
    "teşekkürler",
    "sì",
    "si",
    "grazie",
    "merci",
}
MIN_CHARS_FOR_SWITCH = 12
MIN_CONFIDENCE = 0.6
MIN_CONFIDENCE_WITH_MARGIN = 0.35
MIN_MARGIN = 1.6
"""A lower confidence still counts when the best language leads the runner-up clearly."""
_MIN_CYRILLIC_FOR_DETECTION = 30


@lru_cache(maxsize=1)
def _detector():  # type: ignore[no-untyped-def]
    return LanguageDetectorBuilder.from_languages(*_LINGUA.values()).build()


@lru_cache(maxsize=1)
def _cyrillic_detector():  # type: ignore[no-untyped-def]
    return LanguageDetectorBuilder.from_languages(Language.RUSSIAN, Language.UKRAINIAN).build()


@dataclass(frozen=True)
class Detection:
    lang: str | None
    confidence: float
    runner_up: float = 0.0

    @property
    def strong(self) -> bool:
        return self.lang is not None and self.confidence >= MIN_CONFIDENCE

    @property
    def clear_lead(self) -> bool:
        """A lower confidence that still leads the runner-up clearly (longer texts only)."""
        return (
            self.lang is not None
            and self.confidence >= MIN_CONFIDENCE_WITH_MARGIN
            and self.confidence >= MIN_MARGIN * self.runner_up
        )


def detect(text: str) -> Detection:
    """Best language among the six, with its confidence; ``None`` when unsure."""
    cleaned = text.strip()
    if not cleaned:
        return Detection(None, 0.0)
    values = _detector().compute_language_confidence_values(cleaned)
    if not values:
        return Detection(None, 0.0)
    best = values[0]
    runner_up = float(values[1].value) if len(values) > 1 else 0.0
    return Detection(_CODES.get(best.language), float(best.value), runner_up)


def choose_language(
    text: str,
    previous: str | None,
    supported: Sequence[str],
    default: str,
    override: str | None = None,
) -> tuple[str, bool]:
    """Return (language, supported) applying stickiness and the per-message override."""
    if override and override in LANGS:
        return override, override in supported
    stripped = unicodedata.normalize("NFKC", text).strip().casefold().strip(".!?, ")
    if previous and (len(stripped) < MIN_CHARS_FOR_SWITCH or stripped in _SHORT_ACKS):
        return previous, previous in supported
    detection = detect(text)
    # one word such as "STOP" is the same in many languages: only a strong detection
    # switches on short texts, a clear lead is enough on a sentence
    confident = detection.strong or (len(stripped) >= MIN_CHARS_FOR_SWITCH and detection.clear_lead)
    if not confident or detection.lang is None:
        lang = previous or default
        return lang, lang in supported
    chosen: str = detection.lang
    if chosen == "ru" and _UK_ONLY & set(text):
        chosen = "uk"
    return chosen, chosen in supported


def _word_scripts(text: str) -> tuple[int, int]:
    """Count Cyrillic-dominant and Latin-dominant words, ignoring links and tokens with digits."""
    cyrillic_words = latin_words = 0
    for token in text.split():
        if "://" in token or "@" in token or "www." in token or any(ch.isdigit() for ch in token):
            continue
        cyr = len(_CYRILLIC.findall(token))
        lat = len(_LATIN.findall(token))
        if cyr + lat < 2:
            continue
        if cyr > lat:
            cyrillic_words += 1
        else:
            latin_words += 1
    return cyrillic_words, latin_words


def _cyrillic_language(body: str) -> str | None:
    """``ru`` or ``uk`` for a longer Cyrillic text, ``None`` when too short or unsure."""
    if len(_CYRILLIC.findall(body)) < _MIN_CYRILLIC_FOR_DETECTION:
        return None
    values = _cyrillic_detector().compute_language_confidence_values(body)
    if not values or values[0].value < 0.7:
        return None
    return _CODES.get(values[0].language)


def script_ok(text: str, lang: str) -> bool:
    """Whether the reply uses the script the language needs."""
    body = _PLACEHOLDER.sub(" ", text)
    cyrillic_words, latin_words = _word_scripts(body)
    total = cyrillic_words + latin_words
    if total == 0:
        return True
    share = cyrillic_words / total
    if lang not in {"ru", "uk"}:
        return share <= 0.2
    if share < 0.5:
        return False
    letters = set(body)
    if lang == "uk" and _RU_ONLY & letters:
        return False
    if lang == "ru" and _UK_ONLY & letters:
        return False
    detected = _cyrillic_language(body)
    return detected is None or detected == lang


def unsupported_note_languages(supported: Sequence[str]) -> str:
    names = {
        "de": "Deutsch",
        "en": "English",
        "ru": "Русский",
        "uk": "Українська",
        "tr": "Türkçe",
        "it": "Italiano",
    }
    return ", ".join(names[code] for code in supported if code in names)
