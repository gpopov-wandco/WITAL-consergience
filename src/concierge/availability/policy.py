"""The availability policy layer (F07): what the guest and the model may see.

Implements: F07, C6, C17, 8.4

Rules: (1) the model sees only quote numbers, VAT wording only when ``tax_inclusive``
is known; (2) wording by source; (3) quotes are never binding; (4) ``units_left``
only when the guest asked; (6) extras (children, pets, parking, breakfast, city tax,
cancellation) only from configured or PMS text; (7) a violated restriction names the
rule and an alternative. Every number in the rendered summary comes from the answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from concierge.agent.texts import GuestTexts
from concierge.availability.models import AvailabilityAnswer, Money, Quote, Restriction
from concierge.clock import format_date, to_local

_UNITS_QUESTION = re.compile(
    r"wie viele|how many|сколько|скільки|kaç|quant[ei]|noch frei|still free|left|übrig",
    re.I,
)
WORDING_KEY: dict[str, str] = {
    "live_pms": "availability_fresh",
    "live_pms_cached": "availability_cached",
    "ical": "availability_calendar",
    "manual": "availability_calendar",
    "none": "availability_unknown",
}


@dataclass
class Summary:
    source: str
    summary: str
    fetched_time: str
    updated_date: str
    status: str
    quotes: list[dict[str, Any]] = field(default_factory=list)
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "summary": self.summary,
            "fetched_time": self.fetched_time,
            "updated_date": self.updated_date,
            "status": self.status,
            "quotes": self.quotes,
            "stale": self.stale,
        }


def asks_for_units(text: str) -> bool:
    return bool(_UNITS_QUESTION.search(text))


def money_text(money: Money | None, lang: str) -> str:
    return money.format(lang) if money is not None else ""


def restriction_text(restriction: Restriction, lang: str, texts: GuestTexts, *, nights: int) -> str:
    kind = restriction.kind
    if kind == "min_stay":
        rule = texts.render("avail_rule_min_stay", lang, nights=restriction.value)
    elif kind == "closed_arrival":
        rule = texts.render(
            "avail_rule_closed_arrival", lang, date=_fmt_iso(restriction.value, lang)
        )
    elif kind == "closed_departure":
        rule = texts.render(
            "avail_rule_closed_departure", lang, date=_fmt_iso(restriction.value, lang)
        )
    elif kind == "max_occupancy":
        rule = texts.render("avail_rule_max_occupancy", lang, max=restriction.value)
    else:
        rule = texts.render("avail_rule_closed", lang)
    alternative = ""
    if restriction.alternative:
        if kind == "min_stay":
            alternative = texts.render(
                "avail_alt_until", lang, date=_fmt_iso(restriction.alternative, lang)
            )
        elif kind == "closed_arrival":
            alternative = texts.render(
                "avail_alt_arrival", lang, date=_fmt_iso(restriction.alternative, lang)
            )
        elif kind == "closed_departure":
            alternative = texts.render(
                "avail_alt_departure", lang, date=_fmt_iso(restriction.alternative, lang)
            )
        elif kind == "max_occupancy":
            alternative = texts.render("avail_alt_rooms", lang, rooms=restriction.alternative)
    return f"{rule}. {alternative}".strip() if alternative else rule


def quote_line(quote: Quote, lang: str, texts: GuestTexts, *, nights: int, ask_units: bool) -> str:
    room = quote.room_name or quote.room_type
    if quote.restrictions and (quote.units_left is None or quote.units_left > 0):
        rules = "; ".join(
            restriction_text(r, lang, texts, nights=nights) for r in quote.restrictions
        )
        return texts.render("avail_room_restricted", lang, room=room, rule=rules)
    if quote.units_left is not None and quote.units_left <= 0:
        return texts.render("avail_room_none", lang, room=room)
    if quote.per_night is not None:
        line = texts.render(
            "avail_room_free_price",
            lang,
            room=room,
            price=money_text(quote.per_night, lang),
            total=money_text(quote.total, lang),
            nights=nights,
        )
        if quote.tax_inclusive is True:
            line += " " + texts.render("avail_tax_incl", lang)
        elif quote.tax_inclusive is False:
            line += " " + texts.render("avail_tax_excl", lang)
    else:
        line = texts.render("avail_room_free", lang, room=room)
    if ask_units and quote.units_left is not None:
        line += " " + texts.render("avail_units_left", lang, units=quote.units_left)
    return line


def summarize(
    answer: AvailabilityAnswer,
    lang: str,
    texts: GuestTexts,
    *,
    tz: str,
    ask_units: bool = False,
) -> Summary:
    source = answer.source
    fetched_time = to_local(answer.fetched_at, tz).strftime("%H:%M")
    updated = answer.updated_at or answer.fetched_at
    updated_date = format_date(to_local(updated, tz).date(), lang)
    lines = [
        quote_line(q, lang, texts, nights=answer.stay.nights, ask_units=ask_units)
        for q in answer.quotes
    ]
    if answer.status == "unavailable" or (source != "none" and not lines):
        lines = [texts.render("avail_no_rooms", lang)]
    if "high_demand_no_live_price" in answer.reasons:
        lines.append(texts.render("avail_high_demand", lang))
    if source in {"ical", "manual"}:
        lines.append(texts.render("avail_confirm_reception", lang))
    summary = " ".join(line.rstrip(".") + "." if not line.endswith(".") else line for line in lines)
    return Summary(
        source=source,
        summary=summary.strip(),
        fetched_time=fetched_time,
        updated_date=updated_date,
        status=answer.status,
        quotes=[_quote_dict(q, answer.stay.nights, ask_units) for q in answer.quotes],
        stale=answer.stale,
    )


def wording(
    answer: AvailabilityAnswer, lang: str, texts: GuestTexts, *, tz: str, ask_units: bool = False
) -> str:
    """The full guest sentence: source wording plus the summary plus the indicative note."""
    summary = summarize(answer, lang, texts, tz=tz, ask_units=ask_units)
    key = WORDING_KEY[answer.source]
    text = texts.render(
        key,
        lang,
        summary=summary.summary,
        time=summary.fetched_time,
        date=summary.updated_date,
    )
    if answer.source != "none":
        text += " " + texts.render("availability_indicative", lang)
    return text


def tool_result(
    answer: AvailabilityAnswer,
    lang: str,
    texts: GuestTexts,
    *,
    tz: str,
    ask_units: bool = False,
    booking_url: str | None = None,
) -> dict[str, Any]:
    """What the model sees: quote numbers, the wording to use, an instruction; nothing else."""
    summary = summarize(answer, lang, texts, tz=tz, ask_units=ask_units)
    instruction = {
        "live_pms": "quote only these numbers; open with the wording; add that prices are indicative and the booking is made through the booking link or the reception",
        "live_pms_cached": "quote only these numbers and say 'as of {time}'; add that prices are indicative".replace(
            "{time}", summary.fetched_time
        ),
        "ical": "say 'according to our calendar (updated {date})' and ask the guest to confirm with the reception; no prices".replace(
            "{date}", summary.updated_date
        ),
        "manual": "say 'according to our calendar (updated {date})' and ask the guest to confirm with the reception; quote only these numbers".replace(
            "{date}", summary.updated_date
        ),
        "none": "no live data: say the reception will check and create_handoff with priority P2",
    }[answer.source]
    if answer.stale:
        instruction += "; the data is older than the threshold, so give availability only 'as of' the date, no prices"
    result: dict[str, Any] = {
        "source": answer.source,
        "status": answer.status,
        "wording": wording(answer, lang, texts, tz=tz, ask_units=ask_units),
        "arrival": answer.stay.arrival.isoformat(),
        "departure": answer.stay.departure.isoformat(),
        "nights": answer.stay.nights,
        "adults": answer.occupancy.adults,
        "children": answer.occupancy.children,
        "fetched_at": answer.fetched_at.isoformat(timespec="minutes"),
        "valid_until": answer.valid_until.isoformat(timespec="minutes"),
        "quotes": summary.quotes,
        "binding": False,
        "instruction": instruction,
    }
    if booking_url:
        result["booking_url"] = booking_url
    extras = {k: v for q in answer.quotes for k, v in q.extras.items()}
    if extras:
        result["untrusted_text"] = extras
    return result


def _quote_dict(quote: Quote, nights: int, ask_units: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "room": quote.room_name or quote.room_type,
        "room_code": quote.room_type,
        "available": quote.available,
        "restrictions": [
            {"kind": r.kind, "value": r.value, "alternative": r.alternative}
            for r in quote.restrictions
        ],
    }
    if quote.per_night is not None:
        data["per_night"] = _amount(quote.per_night)
        data["currency"] = quote.per_night.currency
    if quote.total is not None:
        data["total"] = _amount(quote.total)
        data["nights"] = nights
    if quote.tax_inclusive is not None and quote.per_night is not None:
        data["tax_inclusive"] = quote.tax_inclusive
    if ask_units and quote.units_left is not None:
        data["units_left"] = quote.units_left
    if quote.max_occupancy is not None:
        data["max_occupancy"] = quote.max_occupancy
    return data


def _amount(money: Money) -> str:
    whole, frac = divmod(money.cents, 100)
    return f"{whole}" if frac == 0 else f"{whole}.{frac:02d}"


def _fmt_iso(value: str, lang: str) -> str:
    try:
        return format_date(date.fromisoformat(value), lang)
    except ValueError:
        return value
