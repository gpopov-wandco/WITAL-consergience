"""The 85-item onboarding questionnaire (Appendix D, groups A to I) and its answers.

Implements: F06, F21, C5

Every item carries a key, a kind, the knowledge-base key it feeds (or the profile
field it informs), the required flag and German and English labels. Answers are
stored per tenant, travel in a client pack as ``questionnaire_answers.yaml`` and
become draft knowledge-base entries that the owner approves; no answer reaches a
guest before that approval.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from sqlalchemy import delete, select

from concierge import audit
from concierge.db.models import QuestionnaireAnswer
from concierge.db.session import Database
from concierge.kb.blocks import block_of
from concierge.kb.store import KbService

Kind = Literal["text", "phone", "email", "url", "time", "bool", "choice", "number", "money", "list"]

GROUPS: dict[str, tuple[str, str]] = {
    "A": ("Betrieb und Kontakte", "Business and contacts"),
    "B": ("Kanäle", "Channels"),
    "C": ("PMS und Verfügbarkeit", "PMS and availability"),
    "D": ("Zeiten und Anreise", "Times and arrival"),
    "E": ("Regeln und Geld", "Policies and money"),
    "F": ("Ausstattung", "Facilities"),
    "G": ("Gästeinformation", "Guest information"),
    "H": ("Übergabe und Mitarbeiter", "Handoff and staff"),
    "I": ("Marke, Recht, Einwilligung", "Brand, legal, consent"),
}
EXPECTED_COUNTS: dict[str, int] = {
    "A": 10,
    "B": 8,
    "C": 8,
    "D": 10,
    "E": 12,
    "F": 14,
    "G": 8,
    "H": 7,
    "I": 8,
}
FREE_TEXT_MAX = 600
"""Appendix D: free text for guests (welcome, neighbourhood, FAQ) at most 600 characters."""


@dataclass(frozen=True)
class Item:
    key: str
    kind: Kind
    label_de: str
    label_en: str
    target: str = ""
    """Knowledge-base key the answer feeds; empty when the answer informs the profile."""
    field: str = ""
    """Field inside a structured block, or the profile path for profile-only items."""
    required: bool = False
    choices: tuple[str, ...] = ()
    max_len: int = 2000

    @property
    def group(self) -> str:
        return self.key[0].upper()

    def label(self, lang: str) -> str:
        return self.label_de if lang == "de" else self.label_en


def _i(
    key: str,
    kind: Kind,
    de: str,
    en: str,
    target: str = "",
    field: str = "",
    *,
    required: bool = False,
    choices: tuple[str, ...] = (),
    max_len: int = 2000,
) -> Item:
    return Item(key, kind, de, en, target, field, required, choices, max_len)


ITEMS: tuple[Item, ...] = (
    # A. business and contacts (10)
    _i("a_hotel_name", "text", "Name des Hauses", "Hotel name", "identity", "name", required=True),
    _i(
        "a_legal_name",
        "text",
        "Rechtsträger (Firma)",
        "Legal entity",
        "identity",
        "legal_name",
        required=True,
    ),
    _i("a_address", "text", "Anschrift", "Address", "identity", "address", required=True),
    _i(
        "a_phone",
        "phone",
        "Telefon der Rezeption",
        "Reception phone",
        "identity",
        "phone",
        required=True,
    ),
    _i("a_email", "email", "E-Mail für Gäste", "Guest e-mail", "identity", "email", required=True),
    _i("a_website", "url", "Website", "Website", "identity", "website", required=True),
    _i(
        "a_imprint_url",
        "url",
        "Impressum (URL)",
        "Imprint URL",
        "legal",
        "imprint_url",
        required=True,
    ),
    _i(
        "a_privacy_url",
        "url",
        "Datenschutzerklärung (URL)",
        "Privacy policy URL",
        "legal",
        "privacy_url",
        required=True,
    ),
    _i("a_terms_url", "url", "AGB (URL)", "Terms URL", "legal", "terms_url"),
    _i(
        "a_reception_hours",
        "text",
        "Rezeptionszeiten",
        "Reception hours",
        "contact_hours",
        "reception_hours",
        required=True,
    ),
    # B. channels (8)
    _i(
        "b_whatsapp_number",
        "phone",
        "WhatsApp-Nummer des Hauses",
        "WhatsApp number of the hotel",
        field="whatsapp.display_number",
        required=True,
    ),
    _i(
        "b_whatsapp_owner",
        "choice",
        "Wer besitzt das WhatsApp-Business-Konto",
        "Who owns the WhatsApp Business account",
        field="whatsapp.owner",
        required=True,
        choices=("hotel", "agency"),
    ),
    _i(
        "b_website_widget",
        "bool",
        "Chat-Widget auf der Website",
        "Chat widget on the website",
        field="widget.enabled",
        required=True,
    ),
    _i(
        "b_widget_origins",
        "list",
        "Website-Adressen für das Widget",
        "Website origins for the widget",
        field="widget.allowed_origins",
    ),
    _i("b_voice_line", "bool", "Telefonische Auskunft", "Phone line", field="voice.provider"),
    _i(
        "b_voice_number",
        "phone",
        "Telefonnummer der Auskunft",
        "Phone line number",
        field="voice.number",
    ),
    _i(
        "b_languages",
        "list",
        "Sprachen (de, en, ru, uk, tr, it)",
        "Languages (de, en, ru, uk, tr, it)",
        field="languages",
        required=True,
    ),
    _i(
        "b_default_language",
        "choice",
        "Standardsprache",
        "Default language",
        field="default_language",
        required=True,
        choices=("de", "en"),
    ),
    # C. PMS and availability (8)
    _i(
        "c_pms",
        "choice",
        "Hotelsoftware",
        "Property management system",
        field="availability.chain",
        required=True,
        choices=("beds24", "apaleo", "mews", "ical", "manual", "none"),
    ),
    _i(
        "c_pms_property",
        "text",
        "Objekt- oder Property-Kennung im PMS (keine Zugangsdaten)",
        "Property id in the PMS (no credentials)",
        field="availability.property_id",
    ),
    _i(
        "c_ical_urls",
        "list",
        "iCal-Adressen je Einheit",
        "iCal feed URLs per unit",
        field="availability.ical",
    ),
    _i(
        "c_booking_url",
        "url",
        "Buchungsseite (URL)",
        "Booking page URL",
        field="availability.booking_url",
        required=True,
    ),
    _i("c_room_types", "list", "Zimmerkategorien", "Room types", "rooms", "types", required=True),
    _i(
        "c_max_occupancy",
        "text",
        "Maximale Belegung je Kategorie",
        "Maximum occupancy per type",
        "rooms",
        "occupancy",
    ),
    _i(
        "c_children_policy",
        "text",
        "Kinder und Zustellbetten",
        "Children and cots",
        "rates_policy",
        "children",
    ),
    _i("c_extra_bed", "text", "Zusatzbett", "Extra bed", "rates_policy", "extra_bed"),
    # D. times and arrival (10)
    _i("d_checkin_from", "time", "Check-in ab", "Check-in from", "checkin", "from", required=True),
    _i("d_checkin_until", "time", "Check-in bis", "Check-in until", "checkin", "until"),
    _i("d_early_checkin", "text", "Früher Check-in", "Early check-in", "checkin", "early_checkin"),
    _i(
        "d_checkout_until",
        "time",
        "Check-out bis",
        "Check-out until",
        "checkout",
        "until",
        required=True,
    ),
    _i(
        "d_late_checkout", "text", "Später Check-out", "Late check-out", "checkout", "late_checkout"
    ),
    _i(
        "d_latest_reception",
        "time",
        "Rezeption besetzt bis",
        "Reception staffed until",
        "late_arrival",
        "latest_reception",
        required=True,
    ),
    _i(
        "d_key_safe",
        "text",
        "Ablauf bei später Anreise (Schlüsselsafe, ohne Code)",
        "Late-arrival procedure (key safe, without the code)",
        "late_arrival",
        "key_safe",
        required=True,
    ),
    _i(
        "d_after_hours_contact",
        "phone",
        "Kontakt außerhalb der Rezeptionszeiten",
        "After-hours contact",
        "late_arrival",
        "after_hours_contact",
    ),
    _i(
        "d_directions_airport",
        "text",
        "Anfahrt vom Flughafen",
        "Directions from the airport",
        "directions.airport",
        required=True,
    ),
    _i(
        "d_directions_station",
        "text",
        "Anfahrt vom Hauptbahnhof",
        "Directions from the main station",
        "directions.station",
    ),
    # E. policies and money (12)
    _i(
        "e_cancellation_flex",
        "text",
        "Stornobedingungen flexible Rate",
        "Cancellation terms, flexible rate",
        "cancellation.flex",
        required=True,
    ),
    _i(
        "e_cancellation_nonref",
        "text",
        "Stornobedingungen nicht erstattbare Rate",
        "Cancellation terms, non-refundable rate",
        "cancellation.non_refundable",
    ),
    _i(
        "e_payment_methods",
        "list",
        "Zahlungsarten",
        "Payment methods",
        "payment",
        "methods",
        required=True,
    ),
    _i("e_deposit", "text", "Anzahlung", "Deposit", "payment", "deposit"),
    _i("e_prepayment", "text", "Vorauszahlung", "Prepayment", "payment", "prepayment"),
    _i(
        "e_tax_inclusive",
        "bool",
        "Preise inklusive Mehrwertsteuer",
        "Prices include VAT",
        "rates_policy",
        "tax_inclusive",
        required=True,
    ),
    _i(
        "e_currency",
        "choice",
        "Währung",
        "Currency",
        "rates_policy",
        "currency",
        required=True,
        choices=("EUR", "CHF"),
    ),
    _i(
        "e_city_tax_text",
        "text",
        "Übernachtungssteuer (Satz laut Kommune)",
        "City tax (rate as set by the municipality)",
        "city_tax",
        "rate_text",
    ),
    _i(
        "e_city_tax_exemptions",
        "text",
        "Ausnahmen von der Übernachtungssteuer",
        "City-tax exemptions",
        "city_tax",
        "exemptions",
    ),
    _i(
        "e_invoicing",
        "text",
        "Rechnungen und Firmenadressen",
        "Invoices and company addresses",
        "invoicing",
    ),
    _i("e_group_terms", "text", "Gruppenbedingungen", "Group terms", "cancellation.groups"),
    _i(
        "e_house_rules",
        "text",
        "Hausordnung (Rauchen, Ruhezeiten, Besucher)",
        "House rules (smoking, quiet times, visitors)",
        "house_rules",
    ),
    # F. facilities (14)
    _i(
        "f_breakfast_from",
        "time",
        "Frühstück von",
        "Breakfast from",
        "breakfast",
        "from",
        required=True,
    ),
    _i(
        "f_breakfast_to",
        "time",
        "Frühstück bis",
        "Breakfast until",
        "breakfast",
        "to",
        required=True,
    ),
    _i(
        "f_breakfast_price",
        "money",
        "Frühstückspreis pro Person",
        "Breakfast price per person",
        "breakfast",
        "price_cents",
    ),
    _i(
        "f_breakfast_included",
        "bool",
        "Frühstück im Preis enthalten",
        "Breakfast included in the rate",
        "breakfast",
        "included",
    ),
    _i(
        "f_breakfast_diet",
        "text",
        "Vegan, glutenfrei, Allergien",
        "Vegan, gluten-free, allergies",
        "breakfast",
        "diet",
    ),
    _i(
        "f_parking_type",
        "choice",
        "Parken",
        "Parking",
        "parking",
        "type",
        required=True,
        choices=("garage", "outdoor", "street", "none"),
    ),
    _i(
        "f_parking_price",
        "money",
        "Parkpreis pro Nacht",
        "Parking price per night",
        "parking",
        "price_cents_per_night",
    ),
    _i(
        "f_parking_clearance",
        "number",
        "Einfahrtshöhe in Metern",
        "Clearance height in metres",
        "parking",
        "clearance_m",
    ),
    _i(
        "f_parking_reservation",
        "text",
        "Parkplatz reservierbar",
        "Parking reservation",
        "parking",
        "reservation",
    ),
    _i("f_pets_allowed", "bool", "Haustiere erlaubt", "Pets allowed", "pets", "allowed"),
    _i(
        "f_pets_fee",
        "money",
        "Gebühr für Haustiere pro Nacht",
        "Pet fee per night",
        "pets",
        "fee_cents_per_night",
    ),
    _i("f_wifi_network", "text", "WLAN-Name", "Wi-Fi network name", "wifi", "network"),
    _i("f_accessibility", "text", "Barrierefreiheit", "Accessibility", "accessibility"),
    _i("f_luggage", "text", "Gepäckaufbewahrung", "Luggage storage", "luggage"),
    # G. guest information (8)
    _i(
        "g_nearby",
        "list",
        "Umgebung (Name, Art, Entfernung)",
        "Nearby (name, kind, distance)",
        "nearby",
        "places",
    ),
    _i(
        "g_events",
        "text",
        "Messen und Veranstaltungen mit hoher Nachfrage",
        "Fairs and high-demand events",
        "events",
        "notes",
    ),
    _i("g_extras", "text", "Extras und Leistungen", "Extras and services", "extras"),
    _i(
        "g_welcome",
        "text",
        "Willkommenstext",
        "Welcome text",
        "extras.welcome",
        max_len=FREE_TEXT_MAX,
    ),
    _i(
        "g_neighbourhood",
        "text",
        "Tipps zur Nachbarschaft",
        "Neighbourhood advice",
        "extras.neighbourhood",
        max_len=FREE_TEXT_MAX,
    ),
    _i(
        "g_faq",
        "text",
        "Häufige Fragen und Antworten",
        "Frequent questions and answers",
        "extras.faq",
        max_len=FREE_TEXT_MAX,
    ),
    _i("g_lost_property", "text", "Fundsachen", "Lost property", "lost_property"),
    _i(
        "g_emergency",
        "text",
        "Notfallhinweise (112, 110, Hausnotruf)",
        "Emergency notes (112, 110, in-house number)",
        "emergency",
        required=True,
    ),
    # H. handoff and staff (7)
    _i(
        "h_staffed_hours",
        "text",
        "Besetzte Zeiten je Wochentag",
        "Staffed hours per weekday",
        "handoff",
        "staffed_hours",
        required=True,
    ),
    _i(
        "h_sla_minutes",
        "number",
        "Antwortzeit für Übergaben in Minuten",
        "Response time for handoffs in minutes",
        "handoff",
        "sla",
    ),
    _i(
        "h_escalation_p0",
        "phone",
        "Rufbereitschaft für Notfälle (P0)",
        "On-call number for emergencies (P0)",
        field="handoff.contacts.p0",
        required=True,
    ),
    _i(
        "h_escalation_email",
        "email",
        "E-Mail für Übergaben",
        "E-mail for handoffs",
        field="handoff.contacts.email",
        required=True,
    ),
    _i(
        "h_alert_whatsapp",
        "phone",
        "WhatsApp-Nummer für Mitarbeiter-Alarme",
        "WhatsApp number for staff alerts",
        field="handoff.contacts.whatsapp",
    ),
    _i(
        "h_kill_switch_contact",
        "text",
        "Wer darf den Assistenten abschalten",
        "Who may switch the assistant off",
        field="handoff.kill_switch",
    ),
    _i(
        "h_response_window",
        "text",
        "Antwortfenster ohne Live-Daten (z. B. 2 Stunden)",
        "Response window without live data (for example 2 hours)",
        field="availability.response_window",
    ),
    # I. brand, legal, consent (8)
    _i(
        "i_tone",
        "text",
        "Tonalität (Du oder Sie, Anrede, Stil)",
        "Tone (formal or informal, style)",
        "tone",
    ),
    _i(
        "i_refusals",
        "text",
        "Was der Assistent ablehnen soll",
        "What the assistant must decline",
        "refusals",
    ),
    _i(
        "i_brand_colour", "text", "Markenfarbe (Hex)", "Brand colour (hex)", field="branding.colour"
    ),
    _i(
        "i_logo_text",
        "text",
        "Logotext im Widget",
        "Logo text in the widget",
        field="branding.logo_text",
    ),
    _i(
        "i_dpa_signed",
        "bool",
        "Auftragsverarbeitungsvertrag unterschrieben",
        "Processor agreement signed",
        field="legal.dpa_signed",
        required=True,
    ),
    _i(
        "i_us_transfer_ack",
        "bool",
        "Übermittlung in die USA (Anthropic, Meta) bestätigt",
        "US transfers (Anthropic, Meta) acknowledged",
        field="legal.us_transfer_ack",
        required=True,
    ),
    _i(
        "i_marketing_optin_text",
        "text",
        "Formulierung der Werbeeinwilligung",
        "Marketing opt-in wording",
        field="journeys.marketing_optin",
    ),
    _i(
        "i_legal_reviewer",
        "text",
        "Wer prüft die Rechtstexte (Name)",
        "Who reviews the legal texts (name)",
        field="legal.reviewer",
        required=True,
    ),
)
ITEMS_BY_KEY: dict[str, Item] = {item.key: item for item in ITEMS}
REQUIRED_ITEMS: tuple[Item, ...] = tuple(item for item in ITEMS if item.required)

_PHONE = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MONEY = re.compile(r"^(?:(?:EUR|CHF|€)\s*)?(\d{1,6})(?:[.,](\d{1,2}))?\s*(?:€|EUR|CHF)?$")
_TRUE = {"true", "yes", "ja", "y", "1", "on"}
_FALSE = {"false", "no", "nein", "n", "0", "off"}


def items_of(group: str) -> list[Item]:
    return [item for item in ITEMS if item.group == group]


def group_counts() -> dict[str, int]:
    return dict(Counter(item.group for item in ITEMS))


# ------------------------------------------------------------------ validation
def normalise(item: Item, raw: Any) -> Any:
    """The stored form of one answer, or raise ``ValueError`` with a plain reason."""
    if item.kind == "bool":
        if isinstance(raw, bool):
            return raw
        token = str(raw).strip().casefold()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
        raise ValueError("expected yes or no")
    if item.kind == "list":
        parts = raw if isinstance(raw, list) else re.split(r"[\n,]", str(raw))
        values = [str(part).strip() for part in parts if str(part).strip()]
        if not values:
            raise ValueError("expected at least one entry")
        if any(len(value) > item.max_len for value in values):
            raise ValueError(f"an entry is longer than {item.max_len} characters")
        return values
    if item.kind == "number":
        if isinstance(raw, bool):
            raise ValueError("expected a number")
        if isinstance(raw, int | float):
            number = float(raw)
        else:
            try:
                number = float(str(raw).strip().replace(",", "."))
            except ValueError as exc:
                raise ValueError("expected a number") from exc
        if number < 0:
            raise ValueError("expected a number of zero or more")
        return int(number) if number.is_integer() else number
    if item.kind == "money":
        if isinstance(raw, bool):
            raise ValueError("expected an amount such as 18.00")
        if isinstance(raw, int | float):
            return round(float(raw) * 100)
        match = _MONEY.match(str(raw).strip())
        if not match:
            raise ValueError("expected an amount such as 18.00")
        whole, fraction = match.group(1), (match.group(2) or "0").ljust(2, "0")
        return int(whole) * 100 + int(fraction)
    text = str(raw).strip()
    if not text:
        raise ValueError("expected a value")
    if item.kind == "choice":
        for choice in item.choices:
            if choice.casefold() == text.casefold():
                return choice
        raise ValueError("expected one of " + ", ".join(item.choices))
    if item.kind == "phone":
        digits = re.sub(r"[\s()/-]", "", text)
        if not _PHONE.match(digits):
            raise ValueError("expected an international number such as +49301234567")
        return digits
    if item.kind == "email":
        if not _EMAIL.match(text):
            raise ValueError("expected an e-mail address")
        return text
    if item.kind == "url":
        if not text.startswith(("http://", "https://")) or " " in text:
            raise ValueError("expected a URL starting with http:// or https://")
        return text
    if item.kind == "time":
        if not _TIME.match(text):
            raise ValueError("expected a time such as 15:00")
        return text
    if len(text) > item.max_len:
        raise ValueError(f"longer than {item.max_len} characters")
    return text


def _is_blank(raw: Any) -> bool:
    return raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []


def validate_answers(data: Any) -> tuple[dict[str, Any], list[str]]:
    """Normalise an answers document (mapping or list form); errors carry field paths.

    Accepted shapes: ``{"answers": {key: value}}``, ``{"answers": [{"key": k,
    "value": v}]}`` or a bare ``{key: value}`` mapping. Blank answers are dropped.
    """
    errors: list[str] = []
    answers: dict[str, Any] = {}
    if isinstance(data, dict) and "answers" in data:
        raw = data["answers"]
    elif isinstance(data, dict):
        raw = data
    else:
        return {}, ["questionnaire_answers.yaml: expected a mapping"]
    pairs: list[tuple[str, str, Any]] = []
    if isinstance(raw, dict):
        pairs = [(f"answers.{key}", str(key), value) for key, value in raw.items()]
    elif isinstance(raw, list):
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict) or "key" not in entry:
                errors.append(f"answers[{index}]: expected a mapping with key and value")
                continue
            pairs.append((f"answers[{index}]", str(entry["key"]), entry.get("value")))
    elif raw is not None:
        errors.append("answers: expected a mapping or a list")
    for path, key, value in pairs:
        item = ITEMS_BY_KEY.get(key)
        if item is None:
            errors.append(f"{path}: unknown item {key!r}")
            continue
        if _is_blank(value):
            continue
        try:
            answers[key] = normalise(item, value)
        except ValueError as exc:
            errors.append(f"{path}: {exc}")
    return answers, errors


def missing_required(answers: dict[str, Any]) -> list[Item]:
    return [item for item in REQUIRED_ITEMS if item.key not in answers]


# ------------------------------------------------------------------ rendering
def render_value(item: Item, value: Any, lang: str) -> str:
    """The answer as a reader sees it (booleans and money in the language's form)."""
    if item.kind == "bool":
        if lang == "de":
            return "ja" if value else "nein"
        return "yes" if value else "no"
    if item.kind == "money":
        cents = int(value)
        return f"{cents // 100},{cents % 100:02d} EUR" if lang == "de" else f"EUR {cents / 100:.2f}"
    if item.kind == "list":
        return ", ".join(str(v) for v in value)
    return str(value)


def kb_rows(
    answers: dict[str, Any], lang: str, *, source: str = "questionnaire"
) -> list[dict[str, Any]]:
    """Draft knowledge-base rows from the answers, one per target key (Appendix D)."""
    by_target: dict[str, list[Item]] = {}
    for item in ITEMS:
        if item.target and item.key in answers:
            by_target.setdefault(item.target, []).append(item)
    rows: list[dict[str, Any]] = []
    for target, items in by_target.items():
        spec = block_of(target)
        if len(items) == 1 and not items[0].field:
            text = render_value(items[0], answers[items[0].key], lang)
            value: dict[str, Any] = {}
        else:
            lines = [
                f"{item.label(lang)}: {render_value(item, answers[item.key], lang)}"
                for item in items
            ]
            text = "\n".join(lines)
            value = {item.field or item.key: answers[item.key] for item in items}
        rows.append(
            {
                "key": target,
                "lang": lang,
                "text": text,
                "value": value,
                "status": "draft",
                "truth": "authoritative_kb",
                "sensitivity": spec.sensitivity,
                "source": source,
            }
        )
    return rows


# ------------------------------------------------------------------ YAML
def export_value(item: Item, value: Any) -> Any:
    """The YAML form of a stored answer (money as a decimal string)."""
    if item.kind == "money":
        return f"{int(value) / 100:.2f}"
    return value


def form_value(item: Item, value: Any) -> str:
    """The answer as a form field shows it (booleans as yes or no, lists one per line)."""
    if item.kind == "bool":
        return "yes" if value else "no"
    if item.kind == "list":
        return "\n".join(str(v) for v in value)
    return str(export_value(item, value))


def answers_document(slug: str, answers: dict[str, Any]) -> dict[str, Any]:
    """The ``questionnaire_answers.yaml`` document in questionnaire order."""
    ordered = {
        item.key: export_value(item, answers[item.key]) for item in ITEMS if item.key in answers
    }
    return {"tenant": slug, "answers": ordered}


def dump_answers(slug: str, answers: dict[str, Any]) -> str:
    return yaml.safe_dump(
        answers_document(slug, answers), allow_unicode=True, sort_keys=False, width=100
    )


def load_answers(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Answers and field-path errors of a YAML file (an unreadable file is one error)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return {}, [f"unreadable ({type(exc).__name__})"]
    return validate_answers(data if data is not None else {})


# ------------------------------------------------------------------ storage
class QuestionnaireService:
    """Answers per tenant in ``questionnaire_answer`` (one row per item)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def answers(self, tenant_id: str) -> dict[str, Any]:
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(QuestionnaireAnswer).where(QuestionnaireAnswer.tenant_id == tenant_id)
                )
            ).scalars()
            stored = {row.item_key: row.value.get("v") for row in rows}
        return {item.key: stored[item.key] for item in ITEMS if item.key in stored}

    async def save(
        self, tenant_id: str, answers: dict[str, Any], *, actor: str, replace: bool = False
    ) -> int:
        """Upsert the given answers; ``replace`` also removes answers not given."""
        unknown = [key for key in answers if key not in ITEMS_BY_KEY]
        if unknown:
            raise ValueError(f"unknown questionnaire item {unknown[0]!r}")
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(QuestionnaireAnswer).where(QuestionnaireAnswer.tenant_id == tenant_id)
                )
            ).scalars()
            existing = {row.item_key: row for row in rows}
            for key, value in answers.items():
                row = existing.get(key)
                if row is None:
                    session.add(
                        QuestionnaireAnswer(tenant_id=tenant_id, item_key=key, value={"v": value})
                    )
                elif row.value.get("v") != value:
                    row.value = {"v": value}
            if replace:
                stale = [key for key in existing if key not in answers]
                if stale:
                    await session.execute(
                        delete(QuestionnaireAnswer).where(
                            QuestionnaireAnswer.tenant_id == tenant_id,
                            QuestionnaireAnswer.item_key.in_(stale),
                        )
                    )
            audit.record(
                session,
                actor=actor,
                action="questionnaire.save",
                tenant_id=tenant_id,
                details={"items": len(answers), "replace": replace},
            )
            await session.commit()
        return len(answers)

    async def apply(self, tenant_id: str, kb: KbService, *, lang: str, actor: str) -> int:
        """Write one draft KB row per answered target key; returns the row count."""
        rows = kb_rows(await self.answers(tenant_id), lang)
        if not rows:
            return 0
        return await kb.bulk_upsert(tenant_id, rows, actor=actor)
