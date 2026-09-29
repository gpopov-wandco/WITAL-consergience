"""Agent tools: strict schemas, code-side re-authorisation and validation (F02).

Implements: F02, F10, F11, C6, 8.3

Tenant, guest and conversation come from the session, never from model arguments.
Tools are read-only except the queue-creating ones (``create_request``,
``create_handoff``, ``request_callback``, ``set_eta``); no tool sends a message,
changes a price or a booking, or returns another guest's data. Bad inputs return
``is_error`` with an instruction so the model can ask the guest instead of guessing.
Third-party text (PMS notes, calendar summaries) is returned as
``{"source": ..., "untrusted_text": ...}``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from concierge.kb.store import KbService
from concierge.llm.port import ToolSpec

LANG_ENUM = ["de", "en", "ru", "uk", "tr", "it"]
REQUEST_KINDS = [
    "early_checkin",
    "late_checkout",
    "invoice",
    "parking",
    "cot",
    "extra_bed",
    "group_inquiry",
    "allergy",
    "booking_change",
    "other",
]
PRIORITIES = ["P0", "P1", "P2", "P3"]
VERIFY_TRIES_PER_HOUR = 3
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


STAY_PROPERTIES: dict[str, Any] = {
    "arrival": {"type": "string", "description": "Arrival date, YYYY-MM-DD"},
    "departure": {"type": "string", "description": "Departure date, YYYY-MM-DD, after arrival"},
    "adults": {"type": "integer", "description": "Number of adults, 1 to 10"},
    "children": {"type": "integer", "description": "Number of children, 0 to 10"},
    "room_type": {"type": "string", "description": "Room type code, or an empty string for any"},
}

CHAT_TOOLS: list[ToolSpec] = [
    ToolSpec(
        "check_availability",
        "Availability and indicative prices for a stay from the hotel's availability sources. Quote only the numbers it returns and name the source as instructed in the result.",
        _obj(STAY_PROPERTIES),
    ),
    ToolSpec(
        "lookup_kb",
        "Read one hotel fact by key (for example parking, breakfast, directions.airport, wifi). Entries marked verified_guest_only are returned only after verify_reservation succeeded.",
        _obj({"key": {"type": "string", "description": "KB key"}}),
    ),
    ToolSpec(
        "search_kb",
        "Search the hotel facts by words when the key is unknown.",
        _obj({"query": {"type": "string", "description": "Search words in the guest's language"}}),
    ),
    ToolSpec(
        "verify_reservation",
        "Verify the guest's reservation with reference, surname and arrival date. Needed before Wi-Fi password, key-safe code or booking details.",
        _obj(
            {
                "reference": {
                    "type": "string",
                    "description": "Booking reference as the guest wrote it",
                },
                "surname": {"type": "string", "description": "Family name on the booking"},
                "arrival": {"type": "string", "description": "Arrival date, YYYY-MM-DD"},
            }
        ),
    ),
    ToolSpec(
        "get_booking_link",
        "The hotel's booking link for given dates and guests. Use after the guest confirmed dates and occupancy.",
        _obj(STAY_PROPERTIES),
    ),
    ToolSpec(
        "create_request",
        "Record a request for staff (early check-in, late check-out, invoice, parking, cot, extra bed, group inquiry, allergy, booking change, other). Nothing is confirmed by this tool.",
        _obj(
            {
                "kind": {"type": "string", "enum": REQUEST_KINDS},
                "details": {
                    "type": "string",
                    "description": "What the guest asked, in one or two sentences",
                },
            }
        ),
    ),
    ToolSpec(
        "set_eta",
        "Store the guest's expected arrival time (24-hour HH:MM) for today's or the booked arrival; staff are alerted.",
        _obj({"time": {"type": "string", "description": "HH:MM, 24-hour clock"}}),
    ),
    ToolSpec(
        "create_handoff",
        "Hand the conversation to a member of staff with a priority (P0 safety, P1 arrival today or in-house issue, P2 booking change or invoice, P3 general) and a short reason.",
        _obj(
            {
                "priority": {"type": "string", "enum": PRIORITIES},
                "reason": {"type": "string", "description": "Short reason for staff"},
            }
        ),
    ),
    ToolSpec(
        "request_callback",
        "Record that the guest wants a call back from the hotel.",
        _obj({"reason": {"type": "string", "description": "Short reason"}}),
    ),
    ToolSpec(
        "set_language",
        "Switch the conversation language when the guest asks for it.",
        _obj({"language": {"type": "string", "enum": LANG_ENUM}}),
    ),
]

VOICE_TOOLS: list[ToolSpec] = [
    t
    for t in CHAT_TOOLS
    if t.name in {"check_availability", "lookup_kb", "request_callback", "set_language"}
] + [
    ToolSpec(
        "transfer_to_human",
        "Transfer the call to the reception during staffed hours.",
        _obj({"reason": {"type": "string", "description": "Short reason"}}),
    ),
    ToolSpec(
        "end_call",
        "End the call politely after the caller's request is complete.",
        _obj({"reason": {"type": "string", "description": "Short reason"}}),
    ),
]


@dataclass
class ToolBackends:
    """Callables the runner uses; every one is optional and replaced by a fake in tests."""

    check_availability: Callable[[date, date, int, int, str], Awaitable[dict[str, Any]]] | None = (
        None
    )
    verify_reservation: Callable[[str, str, date], Awaitable[dict[str, Any] | None]] | None = None
    booking_link: Callable[[date, date, int, int, str], Awaitable[str | None]] | None = None
    create_request: Callable[[str, str], Awaitable[str]] | None = None
    set_eta: Callable[[time], Awaitable[None]] | None = None
    create_handoff: Callable[[str, str], Awaitable[str]] | None = None
    request_callback: Callable[[str], Awaitable[str]] | None = None
    set_language: Callable[[str], Awaitable[None]] | None = None
    transfer_to_human: Callable[[str], Awaitable[bool]] | None = None
    end_call: Callable[[str], Awaitable[None]] | None = None


@dataclass
class ToolSession:
    """Session-derived facts; the model can never supply these."""

    tenant_id: str
    conversation_id: str
    guest_id: str
    lang: str
    channel: str
    verified: bool = False
    stay_date: date | None = None
    verify_attempts: int = 0
    events: list[str] = field(default_factory=list)


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    binding_confirmation: bool = False
    """True when a PMS reservation with a confirmation number was returned (F07 rule 3)."""


class ToolRunner:
    def __init__(self, kb: KbService, backends: ToolBackends | None = None) -> None:
        self.kb = kb
        self.backends = backends or ToolBackends()

    @staticmethod
    def specs(channel: str) -> list[ToolSpec]:
        return list(VOICE_TOOLS if channel == "voice" else CHAT_TOOLS)

    async def run(self, name: str, args: dict[str, Any], session: ToolSession) -> ToolOutcome:
        handler = getattr(self, f"_tool_{name}", None)
        allowed = {t.name for t in self.specs(session.channel)}
        if handler is None or name not in allowed:
            return ToolOutcome(_error(f"unknown tool {name}; use one of {sorted(allowed)}"), True)
        try:
            outcome: ToolOutcome = await handler(args, session)
        except _BadInput as exc:
            return ToolOutcome(_error(str(exc)), True)
        return outcome

    # ------------------------------------------------------------- tools
    async def _tool_lookup_kb(self, args: dict[str, Any], session: ToolSession) -> ToolOutcome:
        key = _string(args, "key", max_len=120)
        view = await self.kb.lookup(
            session.tenant_id,
            key,
            session.lang,
            verified=session.verified,
            stay_date=session.stay_date,
        )
        if view is None:
            if not session.verified:
                sensitive = await self.kb.lookup(
                    session.tenant_id, key, session.lang, verified=True, stay_date=session.stay_date
                )
                if sensitive is not None:
                    return ToolOutcome(
                        _error(
                            f"{key} is available to verified guests only; ask for booking reference, surname and arrival date and call verify_reservation first"
                        ),
                        True,
                    )
            return ToolOutcome(
                json.dumps(
                    {
                        "source": "kb",
                        "key": key,
                        "found": False,
                        "instruction": "no approved fact for this key; say so and offer a member of staff",
                    },
                    ensure_ascii=False,
                )
            )
        return ToolOutcome(
            json.dumps(
                {
                    "source": "kb",
                    "key": view.key,
                    "found": True,
                    "truth": view.truth,
                    "text": view.text,
                    "value": view.value,
                    "lang": view.lang,
                    "note": f"text available in {view.fallback_from} only"
                    if view.fallback_from
                    else "",
                },
                ensure_ascii=False,
            )
        )

    async def _tool_search_kb(self, args: dict[str, Any], session: ToolSession) -> ToolOutcome:
        query = _string(args, "query", max_len=300)
        views = await self.kb.search(
            session.tenant_id, query, session.lang, verified=session.verified
        )
        return ToolOutcome(
            json.dumps(
                {
                    "source": "kb",
                    "results": [{"key": v.key, "text": v.text, "value": v.value} for v in views],
                    "instruction": ""
                    if views
                    else "no matching fact; say so and offer a member of staff",
                },
                ensure_ascii=False,
            )
        )

    async def _tool_check_availability(
        self, args: dict[str, Any], session: ToolSession
    ) -> ToolOutcome:
        arrival, departure, adults, children, room_type = _stay(args)
        if self.backends.check_availability is None:
            return ToolOutcome(
                json.dumps(
                    {
                        "source": "none",
                        "availability": "unknown",
                        "instruction": "no availability source is configured; say the reception will check and create_handoff with priority P2",
                    },
                    ensure_ascii=False,
                )
            )
        result = await self.backends.check_availability(
            arrival, departure, adults, children, room_type
        )
        return ToolOutcome(json.dumps(result, ensure_ascii=False, default=str))

    async def _tool_verify_reservation(
        self, args: dict[str, Any], session: ToolSession
    ) -> ToolOutcome:
        reference = _string(args, "reference", max_len=64)
        surname = _string(args, "surname", max_len=100)
        arrival = _date(args, "arrival")
        if session.verify_attempts >= VERIFY_TRIES_PER_HOUR:
            return ToolOutcome(
                _error(
                    "verification is locked for this hour after three failed tries; offer the reception phone number"
                ),
                True,
            )
        if self.backends.verify_reservation is None:
            return ToolOutcome(
                _error("reservation verification is not configured; offer a member of staff"), True
            )
        session.verify_attempts += 1
        found = await self.backends.verify_reservation(reference, surname, arrival)
        if found is None:
            return ToolOutcome(
                json.dumps(
                    {
                        "source": "reservations",
                        "verified": False,
                        "instruction": "details do not match; ask the guest to check reference, surname and arrival date",
                    },
                    ensure_ascii=False,
                )
            )
        session.verified = True
        session.stay_date = arrival
        session.events.append("verified")
        payload = {"source": "reservations", "verified": True, **found}
        return ToolOutcome(
            json.dumps(payload, ensure_ascii=False, default=str),
            binding_confirmation=bool(found.get("confirmation_number")),
        )

    async def _tool_get_booking_link(
        self, args: dict[str, Any], session: ToolSession
    ) -> ToolOutcome:
        arrival, departure, adults, children, room_type = _stay(args)
        if self.backends.booking_link is None:
            return ToolOutcome(
                _error("no booking link is configured; offer the reception phone or e-mail"), True
            )
        url = await self.backends.booking_link(arrival, departure, adults, children, room_type)
        if not url:
            return ToolOutcome(
                _error("no booking link is configured; offer the reception phone or e-mail"), True
            )
        return ToolOutcome(
            json.dumps(
                {
                    "source": "booking_link",
                    "url": url,
                    "arrival": arrival.isoformat(),
                    "departure": departure.isoformat(),
                    "adults": adults,
                    "children": children,
                    "instruction": "share the link; say the booking is completed on that page, never that it is booked",
                }
            )
        )

    async def _tool_create_request(self, args: dict[str, Any], session: ToolSession) -> ToolOutcome:
        kind = _enum(args, "kind", REQUEST_KINDS)
        details = _string(args, "details", max_len=600)
        if self.backends.create_request is None:
            return ToolOutcome(
                _error("requests cannot be recorded right now; offer the reception phone"), True
            )
        request_id = await self.backends.create_request(kind, details)
        session.events.append(f"request:{kind}")
        return ToolOutcome(
            json.dumps(
                {
                    "source": "requests",
                    "request_id": request_id,
                    "status": "recorded",
                    "instruction": "tell the guest the request is recorded and not confirmed until staff reply",
                }
            )
        )

    async def _tool_set_eta(self, args: dict[str, Any], session: ToolSession) -> ToolOutcome:
        eta = _time(args, "time")
        if self.backends.set_eta is None:
            return ToolOutcome(
                _error(
                    "arrival time cannot be stored without a verified reservation; ask the guest to verify or tell the reception"
                ),
                True,
            )
        await self.backends.set_eta(eta)
        session.events.append("eta")
        return ToolOutcome(
            json.dumps({"source": "reservations", "eta": eta.strftime("%H:%M"), "status": "stored"})
        )

    async def _tool_create_handoff(self, args: dict[str, Any], session: ToolSession) -> ToolOutcome:
        priority = _enum(args, "priority", PRIORITIES)
        reason = _string(args, "reason", max_len=300)
        if self.backends.create_handoff is None:
            return ToolOutcome(
                _error("handoff is not available; give the reception phone number"), True
            )
        handoff_id = await self.backends.create_handoff(priority, reason)
        session.events.append(f"handoff:{priority}")
        return ToolOutcome(
            json.dumps(
                {
                    "source": "handoff",
                    "handoff_id": handoff_id,
                    "priority": priority,
                    "status": "created",
                }
            )
        )

    async def _tool_request_callback(
        self, args: dict[str, Any], session: ToolSession
    ) -> ToolOutcome:
        reason = _string(args, "reason", max_len=300)
        if self.backends.request_callback is None:
            return ToolOutcome(
                _error("callbacks cannot be recorded; give the reception phone number"), True
            )
        task_id = await self.backends.request_callback(reason)
        session.events.append("callback")
        return ToolOutcome(
            json.dumps({"source": "callback", "task_id": task_id, "status": "recorded"})
        )

    async def _tool_set_language(self, args: dict[str, Any], session: ToolSession) -> ToolOutcome:
        lang = _enum(args, "language", LANG_ENUM)
        session.lang = lang
        session.events.append(f"language:{lang}")
        if self.backends.set_language is not None:
            await self.backends.set_language(lang)
        return ToolOutcome(
            json.dumps({"source": "conversation", "language": lang, "status": "set"})
        )

    async def _tool_transfer_to_human(
        self, args: dict[str, Any], session: ToolSession
    ) -> ToolOutcome:
        reason = _string(args, "reason", max_len=300)
        if self.backends.transfer_to_human is None:
            return ToolOutcome(_error("transfer is not possible now; use request_callback"), True)
        ok = await self.backends.transfer_to_human(reason)
        session.events.append("transfer" if ok else "transfer_failed")
        if not ok:
            return ToolOutcome(
                _error("nobody is available for a transfer; use request_callback"), True
            )
        return ToolOutcome(json.dumps({"source": "voice", "status": "transferring"}))

    async def _tool_end_call(self, args: dict[str, Any], session: ToolSession) -> ToolOutcome:
        reason = _string(args, "reason", max_len=300)
        if self.backends.end_call is not None:
            await self.backends.end_call(reason)
        session.events.append("end_call")
        return ToolOutcome(json.dumps({"source": "voice", "status": "ending"}))


class _BadInput(ValueError):
    pass


def _error(instruction: str) -> str:
    return json.dumps({"error": True, "instruction": instruction})


def _string(args: dict[str, Any], name: str, *, max_len: int) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _BadInput(
            f"{name} must be a non-empty string; ask the guest for it instead of guessing"
        )
    return value.strip()[:max_len]


def _enum(args: dict[str, Any], name: str, allowed: list[str]) -> str:
    value = args.get(name)
    if value not in allowed:
        raise _BadInput(f"{name} must be one of {allowed}")
    return str(value)


def _date(args: dict[str, Any], name: str) -> date:
    value = args.get(name)
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise _BadInput(
            f"{name} must be a date in YYYY-MM-DD; ask the guest for the date instead of guessing"
        )
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError as exc:
        raise _BadInput(f"{name} is not a valid calendar date") from exc


def _time(args: dict[str, Any], name: str) -> time:
    value = args.get(name)
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise _BadInput(f"{name} must be HH:MM on a 24-hour clock; ask the guest for the time")
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _int(args: dict[str, Any], name: str, lo: int, hi: int) -> int:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _BadInput(f"{name} must be an integer between {lo} and {hi}; ask the guest")
    return value


def _stay(args: dict[str, Any]) -> tuple[date, date, int, int, str]:
    arrival = _date(args, "arrival")
    departure = _date(args, "departure")
    if departure <= arrival:
        raise _BadInput("departure must be after arrival; ask the guest for the dates again")
    if (departure - arrival).days > 30:
        raise _BadInput("stays longer than 30 nights need staff; create_handoff with priority P2")
    adults = _int(args, "adults", 1, 10)
    children = _int(args, "children", 0, 10)
    room_type = args.get("room_type", "")
    if not isinstance(room_type, str) or len(room_type) > 32:
        raise _BadInput("room_type must be a short code or an empty string")
    return arrival, departure, adults, children, room_type.strip()
