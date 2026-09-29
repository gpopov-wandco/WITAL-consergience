"""Reply validators (8.4): grounding, forbidden words, identity, secrets, script, length.

Implements: F01, F07, F23, C6, C8, C17, 8.4

Every number, time, date, price, phone number and URL in ``reply_text`` must occur in
the approved KB snapshot, a tool result of this turn, the context envelope or the
guest's own message (echoing the guest's dates and party size is how slots are
confirmed). Comparison is normalised (``2,10`` equals ``2.10``, ``9:00`` equals
``09:00``). A violation triggers one regeneration, then a handoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from concierge.agent.guardrails import luhn_ok
from concierge.agent.languages import script_ok
from concierge.agent.schemas import AgentReply

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+\d[\d\s()/-]{6,}\d")
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})(?!\d)|(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"
)
_NUMBER_RE = re.compile(r"(?<![\w:/.,-])(\d+(?:[.,]\d+)?)(?![\w:/-])")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")
_ALWAYS_ALLOWED_NUMBERS = {"1", "2", "110", "112"}

FORBIDDEN_BINDING: dict[str, list[str]] = {
    "de": ["reserviert", "gebucht", "garantiert", "fest zugesagt", "gehalten", "eingebucht"],
    "en": [
        "reserved",
        "booked",
        "guaranteed",
        "held for you",
        "confirmed your booking",
        "your booking is confirmed",
    ],
    "ru": [
        "забронирован",
        "забронировано",
        "зарезервирован",
        "зарезервировано",
        "гарантирован",
        "гарантировано",
    ],
    "uk": ["заброньовано", "зарезервовано", "гарантовано", "заброньований", "зарезервований"],
    "tr": ["rezerve edildi", "rezerve ettim", "ayrıldı", "garanti edildi", "garantidir"],
    "it": ["prenotato", "prenotata", "riservato", "riservata", "garantito", "garantita"],
}
_NEGATIONS = r"(?:nicht|kein[e]?|not|no|non|не|değil|nessun[ao]?)"
HUMAN_CLAIMS: list[str] = [
    r"\bich bin (?:ein )?(?:echter )?mensch\b",
    r"\bich bin keine? (?:ki|maschine|bot)\b",
    r"\bi am (?:a )?(?:real )?(?:human|person)\b",
    r"\bi'?m (?:a )?(?:real )?(?:human|person)\b",
    r"\bi am not (?:an? )?(?:ai|bot|machine)\b",
    r"\bя (?:живой )?человек\b",
    r"\bя не бот\b",
    r"\bя (?:жива )?людина\b",
    r"\bя не бот\b",
    r"\bben (?:gerçek )?bir insanım\b",
    r"\bbot değilim\b",
    r"\bsono (?:una )?persona (?:vera|reale)\b",
    r"\bsono un essere umano\b",
    r"\bnon sono (?:un )?bot\b",
]


@dataclass
class ValidationContext:
    lang: str
    canary: str
    kb_text: str = ""
    tool_texts: list[str] = field(default_factory=list)
    envelope: str = ""
    guest_text: str = ""
    hotel_text: str = ""
    """The HOTEL block of the system prompt (contacts the model may quote)."""
    channel: str = "whatsapp"
    allow_binding_words: bool = False
    max_chars: int = 350
    hard_cap_chars: int = 700
    allowed_button_ids: frozenset[str] = frozenset()


@dataclass
class ValidationResult:
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def normalise_number(token: str) -> str:
    value = token.replace(",", ".")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value.lstrip("0") or "0"


_AMOUNT_RE = re.compile(
    r"(?:€|\bEUR\b|\bEuro\b|\bCHF\b|\bUSD\b|\$)\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*(?:€|\bEUR\b|\bEuro\b|\bCHF\b|\bUSD\b|\$)",
    re.I,
)


def _without_amounts(text: str) -> str:
    """The text with money amounts removed (a guest cannot vouch for a price)."""
    return _AMOUNT_RE.sub(" ", text)


def extract_facts(text: str) -> dict[str, set[str]]:
    """Normalised URLs, e-mails, phones, times, dates and numbers in a text."""
    facts: dict[str, set[str]] = {
        "url": set(),
        "email": set(),
        "phone": set(),
        "time": set(),
        "date": set(),
        "number": set(),
    }
    remaining = text
    for match in _URL_RE.findall(remaining):
        facts["url"].add(match.rstrip(".,;:").rstrip("/").lower())
    remaining = _URL_RE.sub(" ", remaining)
    for match in _EMAIL_RE.findall(remaining):
        facts["email"].add(match.rstrip(".,;:").lower())
    remaining = _EMAIL_RE.sub(" ", remaining)
    for match in _PHONE_RE.findall(remaining):
        facts["phone"].add(re.sub(r"\D", "", match))
    remaining = _PHONE_RE.sub(" ", remaining)
    for hour, minute in _TIME_RE.findall(remaining):
        facts["time"].add(f"{int(hour):02d}:{minute}")
    remaining = _TIME_RE.sub(" ", remaining)
    for groups in _DATE_RE.findall(remaining):
        if groups[3]:
            facts["date"].add(f"{groups[3]}-{groups[4]}-{groups[5]}")
        else:
            day, month, year = int(groups[0]), int(groups[1]), groups[2]
            year_full = year if len(year) == 4 else "20" + year
            facts["date"].add(f"{year_full}-{month:02d}-{day:02d}")
    remaining = _DATE_RE.sub(" ", remaining)
    for number in _NUMBER_RE.findall(remaining):
        facts["number"].add(normalise_number(number))
    return facts


def _forbidden_hits(text: str, lang: str) -> list[str]:
    lowered = text.casefold()
    hits = []
    for phrase in FORBIDDEN_BINDING.get(lang, []) + FORBIDDEN_BINDING["en"]:
        for match in re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", lowered):
            before = lowered[max(0, match.start() - 24) : match.start()]
            if re.search(_NEGATIONS + r"\s+\w*\s*$", before) or re.search(
                _NEGATIONS + r"\s*$", before
            ):
                continue
            hits.append(phrase)
    return hits


def validate_reply(reply: AgentReply, ctx: ValidationContext) -> ValidationResult:
    result = ValidationResult()
    text = reply.reply_text
    if ctx.canary and ctx.canary in text:
        result.violations.append("canary marker in reply")
    if reply.language != ctx.lang:
        result.violations.append(f"reply language {reply.language} differs from {ctx.lang}")
    if not script_ok(text, ctx.lang):
        result.violations.append(f"reply is not written in the script of {ctx.lang}")
    if len(text) > ctx.hard_cap_chars:
        result.violations.append(f"reply longer than {ctx.hard_cap_chars} characters")
    elif len(text) > ctx.max_chars:
        result.warnings.append(f"reply longer than {ctx.max_chars} characters")
    for match in _CARD_RE.findall(text):
        if luhn_ok(re.sub(r"\D", "", match)):
            result.violations.append("card number in reply")
            break
    for pattern in HUMAN_CLAIMS:
        if re.search(pattern, text, re.I):
            result.violations.append("claims to be human")
            break
    if not ctx.allow_binding_words:
        hits = _forbidden_hits(text, ctx.lang)
        if hits:
            result.violations.append(
                "binding words without a confirmed reservation: " + ", ".join(sorted(set(hits)))
            )
    allowed = extract_facts("\n".join([ctx.kb_text, *ctx.tool_texts, ctx.envelope, ctx.hotel_text]))
    # the guest's own words vouch for dates, times, party sizes and their own contact
    # details, never for prices or links (C6: hotel facts come from the hotel)
    guest = extract_facts(_without_amounts(ctx.guest_text))
    for kind in ("date", "time", "number", "phone", "email"):
        allowed[kind] |= guest[kind]
    found = extract_facts(text)
    for kind in ("url", "email", "phone", "time", "date"):
        missing = found[kind] - allowed[kind]
        if missing:
            result.violations.append(f"{kind} not in facts: {', '.join(sorted(missing))}")
    missing_numbers = {
        n
        for n in found["number"]
        if n not in allowed["number"] and n not in _ALWAYS_ALLOWED_NUMBERS
    }
    if missing_numbers:
        result.violations.append("numbers not in facts: " + ", ".join(sorted(missing_numbers)))
    if ctx.channel == "voice" and reply.buttons:
        result.violations.append("buttons are not allowed on the voice channel")
    if ctx.channel == "voice" and found["url"]:
        result.violations.append("URLs are not allowed on the voice channel")
    if ctx.allowed_button_ids:
        bad = [b.id for b in reply.buttons if b.id not in ctx.allowed_button_ids]
        if bad:
            result.violations.append("unknown button ids: " + ", ".join(bad))
    return result


def corrective_message(result: ValidationResult) -> str:
    """The user-turn message that asks the model to fix its previous reply."""
    lines = [
        "Your previous reply was rejected by the validator. Fix these points and answer again as one JSON object:"
    ]
    lines += [f"- {v}" for v in result.violations]
    lines.append(
        "Use only facts from the HOTEL FACTS section or tool results of this turn; if a fact is missing, say so and offer a member of staff."
    )
    return "\n".join(lines)
