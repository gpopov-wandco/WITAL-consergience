"""The KB block catalogue (Appendix D): keys, kinds, defaults and go-live requirements.

Implements: F05, F06, C5

Keys are dotted: ``parking``, ``directions.airport``, ``cancellation.flex``,
``upsells.late_checkout``. The first segment must be a catalogue block. City-tax
rates differ per municipality and are always tenant-confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["structured", "text", "list"]


@dataclass(frozen=True)
class BlockSpec:
    key: str
    kind: Kind
    label_de: str
    label_en: str
    required: bool = False
    volatile: bool = False
    sensitivity: str = "public"
    fields: tuple[str, ...] = ()


BLOCKS: dict[str, BlockSpec] = {
    spec.key: spec
    for spec in (
        BlockSpec(
            "identity",
            "structured",
            "Identität des Hauses",
            "Hotel identity",
            True,
            fields=("name", "legal_name", "address", "phone", "email", "website", "imprint_url"),
        ),
        BlockSpec(
            "contact_hours",
            "structured",
            "Kontakt und Rezeptionszeiten",
            "Contact and reception hours",
            True,
            True,
            fields=("reception_hours", "phone", "email", "after_hours"),
        ),
        BlockSpec(
            "checkin",
            "structured",
            "Check-in",
            "Check-in",
            True,
            fields=("from", "until", "early_checkin", "early_fee_cents"),
        ),
        BlockSpec(
            "checkout",
            "structured",
            "Check-out",
            "Check-out",
            True,
            fields=("until", "late_checkout", "late_fee_cents"),
        ),
        BlockSpec(
            "late_arrival",
            "structured",
            "Späte Anreise",
            "Late arrival",
            True,
            fields=("latest_reception", "key_safe", "after_hours_contact"),
        ),
        BlockSpec(
            "rooms", "list", "Zimmer", "Rooms", fields=("code", "name", "max_occupancy", "features")
        ),
        BlockSpec(
            "rates_policy",
            "structured",
            "Preisregeln",
            "Rate policy",
            fields=("tax_inclusive", "currency", "children", "extra_bed"),
        ),
        BlockSpec(
            "cancellation",
            "list",
            "Stornobedingungen",
            "Cancellation terms",
            True,
            fields=("rate_plan", "text"),
        ),
        BlockSpec(
            "payment",
            "structured",
            "Zahlung",
            "Payment",
            True,
            fields=("methods", "deposit", "prepayment"),
        ),
        BlockSpec(
            "city_tax",
            "structured",
            "Übernachtungssteuer",
            "City tax",
            volatile=True,
            fields=("rate_text", "exemptions"),
        ),
        BlockSpec(
            "breakfast",
            "structured",
            "Frühstück",
            "Breakfast",
            True,
            True,
            fields=("from", "to", "price_cents", "included", "diet"),
        ),
        BlockSpec(
            "parking",
            "structured",
            "Parken",
            "Parking",
            True,
            True,
            fields=("type", "price_cents_per_night", "clearance_m", "reservation", "spaces_total"),
        ),
        BlockSpec(
            "pets",
            "structured",
            "Haustiere",
            "Pets",
            fields=("allowed", "fee_cents_per_night", "conditions"),
        ),
        BlockSpec("extras", "text", "Extras", "Extras"),
        BlockSpec("accessibility", "text", "Barrierefreiheit", "Accessibility"),
        BlockSpec(
            "wifi",
            "structured",
            "WLAN",
            "Wi-Fi",
            sensitivity="verified_guest_only",
            fields=("network", "password"),
        ),
        BlockSpec("luggage", "text", "Gepäck", "Luggage"),
        BlockSpec(
            "directions", "structured", "Anfahrt", "Directions", True, fields=("origin", "text")
        ),
        BlockSpec("nearby", "list", "Umgebung", "Nearby", fields=("name", "kind", "distance")),
        BlockSpec(
            "events",
            "list",
            "Veranstaltungen und Messen",
            "Events and fairs",
            volatile=True,
            fields=("name", "from", "to", "high_demand"),
        ),
        BlockSpec("house_rules", "text", "Hausordnung", "House rules"),
        BlockSpec("invoicing", "text", "Rechnungen", "Invoicing"),
        BlockSpec("lost_property", "text", "Fundsachen", "Lost property"),
        BlockSpec("emergency", "text", "Notfall", "Emergency", True),
        BlockSpec(
            "handoff",
            "structured",
            "Übergabe an Mitarbeiter",
            "Handoff",
            True,
            fields=("sla", "staffed_hours"),
        ),
        BlockSpec(
            "upsells",
            "list",
            "Zusatzangebote",
            "Upsells",
            fields=("id", "name", "price_cents", "includes", "conditions", "availability_check"),
        ),
        BlockSpec("tone", "text", "Tonalität", "Tone", sensitivity="staff_only"),
        BlockSpec("refusals", "text", "Ablehnungen", "Refusals", sensitivity="staff_only"),
        BlockSpec(
            "legal",
            "structured",
            "Rechtliches",
            "Legal",
            True,
            fields=("privacy_url", "imprint_url", "terms_url"),
        ),
    )
}

REQUIRED_KEYS: tuple[str, ...] = tuple(k for k, spec in BLOCKS.items() if spec.required)
VOLATILE_KEYS: tuple[str, ...] = tuple(k for k, spec in BLOCKS.items() if spec.volatile)
DEFAULT_REVIEW_DAYS = 180


def block_of(key: str) -> BlockSpec:
    root = key.split(".", 1)[0]
    spec = BLOCKS.get(root)
    if spec is None:
        raise ValueError(f"unknown KB block {root!r} (key {key!r}); see Appendix D")
    return spec


def validate_key(key: str) -> str:
    key = key.strip()
    if not key or any(
        not part.replace("_", "").replace("-", "").isalnum() for part in key.split(".")
    ):
        raise ValueError(f"invalid KB key {key!r}")
    block_of(key)
    return key
