"""Deterministic responder (F04): keyword and regex intents answered from the KB.

Implements: F04, F11, C6

Used in demo mode without keys, in degraded mode (model down, budget reached,
circuit open) and for tenants with ``ai_mode: faq_only``. It shares the KB and the
grounding rules with the model path: it never guesses, answers only with approved
KB text in the guest's language, asks for the missing availability slots and hands
off everything else.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from concierge.agent.schemas import AgentReply, Button, HandoffDecision
from concierge.agent.texts import GuestTexts
from concierge.kb.store import KbService, KbView

INTENTS_PATH = Path(__file__).resolve().parent / "intents.yaml"
_DATE_PATTERNS = [
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)"),
]
_ADULTS_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:erwachsen|personen|person|adult|people|guests?|взросл|человек|гост|доросл|осіб|особ|yetişkin|kişi|adult|person)",
    re.I,
)
_CHILDREN_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:kind|children|child|kids|дет|ребён|ребен|діт|дитин|çocuk|bambin)", re.I
)


@dataclass
class Intent:
    name: str
    keys: list[str]
    verified_only: bool
    patterns: dict[str, list[re.Pattern[str]]]


@dataclass
class IntentTable:
    version: str
    intents: list[Intent]

    @classmethod
    def load(cls, path: Path = INTENTS_PATH) -> IntentTable:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        intents = []
        for name, spec in data["intents"].items():
            patterns = {
                lang: [re.compile(p, re.I) for p in plist]
                for lang, plist in spec.get("patterns", {}).items()
            }
            intents.append(
                Intent(name, list(spec.get("keys", [])), bool(spec.get("verified_only")), patterns)
            )
        return cls(str(data["version"]), intents)

    def match(self, text: str, lang: str) -> Intent | None:
        """First intent matching in the guest's language; other languages only as a fallback."""
        norm = unicodedata.normalize("NFKC", text).casefold()
        order = [lang] + [code for code in ("de", "en", "ru", "uk", "tr", "it") if code != lang]
        for code in order:
            for intent in self.intents:
                if any(p.search(norm) for p in intent.patterns.get(code, [])):
                    return intent
        return None


@lru_cache(maxsize=1)
def intent_table() -> IntentTable:
    return IntentTable.load()


@dataclass
class Slots:
    arrival: date | None = None
    departure: date | None = None
    adults: int | None = None
    children: int = 0

    @property
    def complete(self) -> bool:
        return self.arrival is not None and self.departure is not None and self.adults is not None


def extract_slots(text: str, previous: Slots | None = None) -> Slots:
    slots = Slots(**(previous.__dict__ if previous else {}))
    dates: list[date] = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()
            try:
                if len(groups[0]) == 4:
                    dates.append(date(int(groups[0]), int(groups[1]), int(groups[2])))
                else:
                    dates.append(date(int(groups[2]), int(groups[1]), int(groups[0])))
            except ValueError:
                continue
    dates = sorted(set(dates))
    if len(dates) >= 2:
        slots.arrival, slots.departure = dates[0], dates[1]
    elif len(dates) == 1 and slots.arrival is None:
        slots.arrival = dates[0]
    elif (
        len(dates) == 1
        and slots.arrival is not None
        and slots.departure is None
        and dates[0] > slots.arrival
    ):
        slots.departure = dates[0]
    adults = _ADULTS_RE.search(text)
    if adults:
        slots.adults = max(1, min(10, int(adults.group(1))))
    children = _CHILDREN_RE.search(text)
    if children:
        slots.children = max(0, min(10, int(children.group(1))))
    return slots


@dataclass
class ResponderContext:
    tenant_id: str
    lang: str
    channel: str
    verified: bool
    stay_date: date | None
    hotel_phone: str
    handoff_window: str | None = None
    previous_slots: Slots | None = None
    availability_summary: Any = None
    events: list[str] = field(default_factory=list)


class Responder:
    def __init__(
        self, kb: KbService, texts: GuestTexts, intents: IntentTable | None = None
    ) -> None:
        self.kb = kb
        self.texts = texts
        self.intents = intents or intent_table()

    def _button(self, button_id: str, lang: str) -> Button:
        return Button(id=button_id, title=self.texts.button(button_id, lang))

    async def respond(
        self, text: str, ctx: ResponderContext
    ) -> tuple[AgentReply, list[KbView], Slots | None]:
        lang = ctx.lang
        intent = self.intents.match(text, lang)
        continuing_slots = ctx.previous_slots is not None and (
            intent is None or intent.name == "availability"
        )
        if continuing_slots or (intent is not None and intent.name == "availability"):
            return await self._availability(text, ctx)
        if intent is None:
            return self._handoff_reply("kb_miss", lang, "P3", "no intent matched"), [], None
        if intent.name == "group":
            reply = AgentReply(
                reply_text=self.texts.render("group_lead", lang),
                language=lang,
                intent="H:group",
                handoff=HandoffDecision(needed=True, priority="P2", reason="group inquiry"),
                confidence=0.9,
                buttons=[self._button("person", lang)],
            )
            return reply, [], None
        if intent.verified_only and not ctx.verified:
            reply = AgentReply(
                reply_text=self.texts.render("verification_ask", lang),
                language=lang,
                intent=f"Q:{intent.name}",
                handoff=HandoffDecision(needed=False),
                confidence=0.9,
                buttons=[self._button("verify", lang)] if ctx.channel != "voice" else [],
            )
            return reply, [], None
        views: list[KbView] = []
        for key in intent.keys:
            view = await self.kb.lookup(
                ctx.tenant_id, key, lang, verified=ctx.verified, stay_date=ctx.stay_date
            )
            if view is not None:
                views.append(view)
        if not views:
            return (
                self._handoff_reply(
                    "kb_miss", lang, "P3", f"no approved KB entry for {intent.name}"
                ),
                [],
                None,
            )
        chosen = views[:2] if intent.name == "directions" else views[:1]
        if intent.name == "directions" and len(views) > 1:
            chosen = [self._pick_direction(text, views)]
        text_out = " ".join(v.text for v in chosen)
        buttons = [self._button("person", lang)] if ctx.channel != "voice" else []
        if intent.name == "late_arrival" and ctx.channel != "voice":
            buttons.insert(0, self._button("eta", lang))
        reply = AgentReply(
            reply_text=text_out,
            language=lang,
            intent=f"A:{intent.name}",
            handoff=HandoffDecision(needed=False),
            fact_refs=[v.key for v in chosen],
            confidence=0.95,
            buttons=buttons[:3],
        )
        return reply, chosen, None

    @staticmethod
    def _pick_direction(text: str, views: list[KbView]) -> KbView:
        norm = text.casefold()
        wants = {
            "directions.airport": (
                "flughafen",
                "airport",
                "аэропорт",
                "аеропорт",
                "havaliman",
                "aeroporto",
            ),
            "directions.hbf": (
                "bahnhof",
                "station",
                "вокзал",
                "gar",
                "stazione",
                "zug",
                "train",
                "поезд",
                "потяг",
                "tren",
                "treno",
            ),
            "directions.car": (
                "auto",
                "car",
                "машин",
                "автомоб",
                "araba",
                "macchina",
                "a5",
                "autobahn",
            ),
        }
        for view in views:
            if any(w in norm for w in wants.get(view.key, ())):
                return view
        return views[0]

    async def _availability(
        self, text: str, ctx: ResponderContext
    ) -> tuple[AgentReply, list[KbView], Slots]:
        lang = ctx.lang
        slots = extract_slots(text, ctx.previous_slots)
        if slots.arrival is None or slots.departure is None:
            key = "availability_ask_dates"
            reply = AgentReply(
                reply_text=self.texts.render(key, lang),
                language=lang,
                intent="Q:availability",
                handoff=HandoffDecision(needed=False),
                confidence=0.9,
                buttons=[],
            )
            return reply, [], slots
        if slots.adults is None:
            reply = AgentReply(
                reply_text=self.texts.render("availability_ask_guests", lang),
                language=lang,
                intent="Q:availability",
                handoff=HandoffDecision(needed=False),
                confidence=0.9,
                buttons=[],
            )
            return reply, [], slots
        if ctx.availability_summary is None:
            reply = AgentReply(
                reply_text=self.texts.render("availability_unknown", lang),
                language=lang,
                intent="H:availability",
                handoff=HandoffDecision(
                    needed=True, priority="P2", reason="availability without live data"
                ),
                confidence=0.8,
                buttons=[self._button("person", lang)] if ctx.channel != "voice" else [],
            )
            return reply, [], slots
        summary = ctx.availability_summary
        wording_key = {
            "live_pms": "availability_fresh",
            "live_pms_cached": "availability_cached",
            "ical": "availability_calendar",
            "manual": "availability_calendar",
            "none": "availability_unknown",
        }[summary.get("source", "none")]
        reply_text = self.texts.render(
            wording_key,
            lang,
            summary=summary.get("summary", ""),
            time=summary.get("fetched_time", ""),
            date=summary.get("updated_date", ""),
        )
        if summary.get("source") != "none":
            reply_text += " " + self.texts.render("availability_indicative", lang)
        buttons = (
            [self._button("book", lang), self._button("person", lang)]
            if ctx.channel != "voice"
            else []
        )
        reply = AgentReply(
            reply_text=reply_text,
            language=lang,
            intent="P:availability" if summary.get("source") != "none" else "H:availability",
            handoff=HandoffDecision(
                needed=summary.get("source") == "none",
                priority="P2" if summary.get("source") == "none" else "none",
                reason="" if summary.get("source") != "none" else "no availability source",
            ),
            fact_refs=["check_availability"],
            confidence=0.9,
            buttons=buttons,
        )
        return reply, [], slots

    def _handoff_reply(self, text_key: str, lang: str, priority: str, reason: str) -> AgentReply:
        return AgentReply(
            reply_text=self.texts.render(text_key, lang),
            language=lang,
            intent="H:kb_miss",
            handoff=HandoffDecision(needed=True, priority=priority, reason=reason),
            confidence=0.5,
            buttons=[self._button("person", lang)],
        )
