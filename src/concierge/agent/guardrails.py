"""Deterministic guardrails around the model (F03).

Implements: F03, C8, C17

Order of checks on every inbound text: card and ID redaction (always applied before
storage and any model call), emergency (static reply, P0, no model), STOP words,
requests for a person, questions about other guests, injection markers (logged, never
blocked) and complaint cues (a hint for the handoff policy). Keyword tables are
versioned YAML with at least ten emergency terms per language.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

KEYWORDS_PATH = Path(__file__).resolve().parent / "keywords.yaml"
Kind = Literal["none", "emergency", "stop", "person", "other_guest"]

_CARD_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_CVV_RE = re.compile(r"\b(?:cvv|cvc|csc|sicherheitscode|prüfziffer)\s*[:=]?\s*\d{3,4}\b", re.I)
_ID_RE = re.compile(
    r"\b(?:ausweisnummer|ausweis[- ]?nr\.?|passnummer|reisepassnummer|passport (?:no|number)|id (?:number|no)|номер паспорта|паспорт №|номер паспорту|pasaport (?:no|numarası)|numero (?:di )?passaporto|carta d'identità)\s*[:=]?\s*([A-Z0-9]{6,12})\b",
    re.I,
)
REDACTED = "[redacted]"


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact_sensitive(text: str) -> tuple[str, list[str]]:
    """Replace card numbers (Luhn-valid), IBANs, CVVs and ID numbers in context."""
    found: list[str] = []

    def card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if luhn_ok(digits):
            found.append("card")
            return REDACTED
        return match.group(0)

    out = _CARD_RE.sub(card, text)
    if _IBAN_RE.search(out):
        found.append("iban")
        out = _IBAN_RE.sub(REDACTED, out)
    if _CVV_RE.search(out):
        found.append("cvv")
        out = _CVV_RE.sub(REDACTED, out)

    def identity(match: re.Match[str]) -> str:
        found.append("id")
        return match.group(0).replace(match.group(1), REDACTED)

    out = _ID_RE.sub(identity, out)
    return out, found


@dataclass
class Keywords:
    version: str
    emergency: dict[str, list[str]]
    stop: dict[str, list[str]]
    person: dict[str, list[str]]
    other_guest: dict[str, list[str]]
    injection: list[str]
    complaint: dict[str, list[str]]

    @classmethod
    def load(cls, path: Path = KEYWORDS_PATH) -> Keywords:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            version=str(data["version"]),
            emergency={k: [normalise(t) for t in v] for k, v in data["emergency"].items()},
            stop={k: [normalise(t) for t in v] for k, v in data["stop"].items()},
            person={k: [normalise(t) for t in v] for k, v in data["person"].items()},
            other_guest={k: [normalise(t) for t in v] for k, v in data["other_guest"].items()},
            injection=[normalise(t) for t in data["injection"]["any"]],
            complaint={k: [normalise(t) for t in v] for k, v in data["complaint"].items()},
        )


@dataclass
class GuardrailOutcome:
    kind: Kind = "none"
    text: str = ""
    redactions: list[str] = field(default_factory=list)
    injection: bool = False
    complaint: bool = False
    matched: str = ""

    @property
    def blocks_model(self) -> bool:
        return self.kind != "none"


def _contains_phrase(haystack: str, phrase: str) -> bool:
    pattern = r"(?<![\w])" + re.escape(phrase) + r"(?![\w])"
    return re.search(pattern, haystack) is not None


class Guardrails:
    def __init__(self, keywords: Keywords | None = None) -> None:
        self.keywords = keywords or Keywords.load()

    def _langs(self, lang: str) -> list[str]:
        """Check the guest's language first, then every other language."""
        ordered = [lang] if lang in self.keywords.emergency else []
        ordered += [code for code in self.keywords.emergency if code != lang]
        return ordered

    def check(self, text: str, lang: str) -> GuardrailOutcome:
        redacted, redactions = redact_sensitive(text)
        outcome = GuardrailOutcome(text=redacted, redactions=redactions)
        norm = normalise(redacted)
        stripped = norm.strip(" .!?,;:\"'«»„“”")
        for code in self._langs(lang):
            for term in self.keywords.emergency.get(code, []):
                if _contains_phrase(norm, term):
                    outcome.kind = "emergency"
                    outcome.matched = term
                    return outcome
        for code in self._langs(lang):
            for term in self.keywords.stop.get(code, []):
                whole_word = " " not in term
                if (whole_word and stripped == term) or (
                    not whole_word and _contains_phrase(norm, term)
                ):
                    outcome.kind = "stop"
                    outcome.matched = term
                    return outcome
        for code in self._langs(lang):
            for term in self.keywords.other_guest.get(code, []):
                if _contains_phrase(norm, term):
                    outcome.kind = "other_guest"
                    outcome.matched = term
                    break
            if outcome.kind != "none":
                break
        if outcome.kind == "none":
            for code in self._langs(lang):
                for term in self.keywords.person.get(code, []):
                    if _contains_phrase(norm, term):
                        outcome.kind = "person"
                        outcome.matched = term
                        break
                if outcome.kind != "none":
                    break
        outcome.injection = any(term in norm for term in self.keywords.injection)
        outcome.complaint = any(
            _contains_phrase(norm, term)
            for code in self._langs(lang)
            for term in self.keywords.complaint.get(code, [])
        )
        return outcome

    def counts(self) -> dict[str, dict[str, int]]:
        return {
            "emergency": {k: len(v) for k, v in self.keywords.emergency.items()},
            "stop": {k: len(v) for k, v in self.keywords.stop.items()},
            "person": {k: len(v) for k, v in self.keywords.person.items()},
        }


@lru_cache(maxsize=1)
def guardrails() -> Guardrails:
    return Guardrails()


def is_off_topic_marker(reply: dict[str, Any]) -> bool:
    intent = str(reply.get("intent", ""))
    return intent.startswith("R:") and "off_topic" in intent
