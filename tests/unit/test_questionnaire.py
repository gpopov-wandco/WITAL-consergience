"""The 85-item questionnaire (F06): catalogue, validation with field paths, YAML round trip,
draft rows per target key."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from concierge.kb.blocks import BLOCKS, validate_key
from concierge.kb.questionnaire import (
    EXPECTED_COUNTS,
    GROUPS,
    ITEMS,
    REQUIRED_ITEMS,
    dump_answers,
    form_value,
    group_counts,
    kb_rows,
    load_answers,
    missing_required,
    validate_answers,
)

pytestmark = pytest.mark.feature("F06")


def test_items_match_appendix_d() -> None:
    assert len(ITEMS) == 85
    assert (
        group_counts()
        == EXPECTED_COUNTS
        == {
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
    )
    assert set(GROUPS) == set(EXPECTED_COUNTS)
    assert len({item.key for item in ITEMS}) == 85
    for item in ITEMS:
        assert item.group in GROUPS and item.key.startswith(item.group.lower() + "_")
        assert item.label_de and item.label_en
        assert item.target or item.field, item.key
        if item.target:
            assert validate_key(item.target) == item.target
            assert item.target.split(".", 1)[0] in BLOCKS
        if item.kind == "choice":
            assert len(item.choices) >= 2
    assert 20 <= len(REQUIRED_ITEMS) <= 40
    assert {i.key for i in REQUIRED_ITEMS} >= {"a_hotel_name", "d_checkin_from", "g_emergency"}
    covered_blocks = {item.target.split(".", 1)[0] for item in ITEMS if item.target}
    required_blocks = {key for key, spec in BLOCKS.items() if spec.required}
    assert required_blocks <= covered_blocks


def test_validate_answers_normalises_and_names_paths() -> None:
    answers, errors = validate_answers(
        {
            "answers": {
                "f_breakfast_price": "16,00",
                "f_pets_fee": 12.5,
                "e_tax_inclusive": "ja",
                "b_website_widget": False,
                "d_checkin_from": "15:00",
                "e_currency": "eur",
                "b_languages": "de, en\nru",
                "a_phone": "+49 (30) 123-456",
                "a_email": "info@hotel-beispiel.invalid",
                "a_website": "https://hotel-beispiel.invalid",
                "f_parking_clearance": "2,10",
                "h_sla_minutes": 15,
                "a_terms_url": "",
                "g_faq": None,
            }
        }
    )
    assert errors == []
    assert answers == {
        "f_breakfast_price": 1600,
        "f_pets_fee": 1250,
        "e_tax_inclusive": True,
        "b_website_widget": False,
        "d_checkin_from": "15:00",
        "e_currency": "EUR",
        "b_languages": ["de", "en", "ru"],
        "a_phone": "+4930123456",
        "a_email": "info@hotel-beispiel.invalid",
        "a_website": "https://hotel-beispiel.invalid",
        "f_parking_clearance": 2.1,
        "h_sla_minutes": 15,
    }
    _, errors = validate_answers(
        {
            "answers": [
                {"key": "a_phone", "value": "030 123"},
                {"key": "d_checkin_from", "value": "3pm"},
                {"key": "g_welcome", "value": "x" * 601},
                {"key": "e_currency", "value": "USD"},
                {"key": "a_website", "value": "hotel-beispiel.invalid"},
                {"key": "f_breakfast_price", "value": "sixteen"},
                {"key": "h_sla_minutes", "value": "-1"},
                {"key": "zzz", "value": 1},
                "not a mapping",
            ]
        }
    )
    assert errors == [
        "answers[8]: expected a mapping with key and value",
        "answers[0]: expected an international number such as +49301234567",
        "answers[1]: expected a time such as 15:00",
        "answers[2]: longer than 600 characters",
        "answers[3]: expected one of EUR, CHF",
        "answers[4]: expected a URL starting with http:// or https://",
        "answers[5]: expected an amount such as 18.00",
        "answers[6]: expected a number of zero or more",
        "answers[7]: unknown item 'zzz'",
    ]
    assert validate_answers(["list"]) == ({}, ["questionnaire_answers.yaml: expected a mapping"])
    assert validate_answers({"answers": 3}) == ({}, ["answers: expected a mapping or a list"])


def test_yaml_round_trip_and_form_values(tmp_path: Path) -> None:
    answers, _ = validate_answers(
        {
            "a_hotel_name": "Hotel Beispiel",
            "f_breakfast_price": "16.00",
            "e_tax_inclusive": True,
            "b_languages": ["de", "en"],
            "f_parking_clearance": 2.1,
        }
    )
    text = dump_answers("hotel-beispiel", answers)
    document = yaml.safe_load(text)
    assert document["tenant"] == "hotel-beispiel"
    assert list(document["answers"]) == [
        "a_hotel_name",
        "b_languages",
        "e_tax_inclusive",
        "f_breakfast_price",
        "f_parking_clearance",
    ]
    assert document["answers"]["f_breakfast_price"] == "16.00"
    path = tmp_path / "questionnaire_answers.yaml"
    path.write_text(text, encoding="utf-8")
    loaded, errors = load_answers(path)
    assert errors == [] and loaded == answers
    (tmp_path / "broken.yaml").write_text("answers: [", encoding="utf-8")
    _, errors = load_answers(tmp_path / "broken.yaml")
    assert len(errors) == 1 and errors[0].startswith("unreadable (")
    by_key = {item.key: item for item in ITEMS}
    assert form_value(by_key["e_tax_inclusive"], True) == "yes"
    assert form_value(by_key["b_languages"], ["de", "en"]) == "de\nen"
    assert form_value(by_key["f_breakfast_price"], 1600) == "16.00"
    assert form_value(by_key["a_hotel_name"], "Hotel Beispiel") == "Hotel Beispiel"


def test_kb_rows_group_answers_per_target() -> None:
    answers, _ = validate_answers(
        {
            "f_breakfast_from": "07:00",
            "f_breakfast_to": "10:00",
            "f_breakfast_price": "16.00",
            "f_breakfast_included": "no",
            "f_wifi_network": "Beispiel-Gast",
            "g_welcome": "Willkommen im Hotel Beispiel.",
            "e_house_rules": "Rauchen ist untersagt.",
            "h_escalation_p0": "+491701234567",
        }
    )
    rows = {row["key"]: row for row in kb_rows(answers, "de")}
    assert set(rows) == {"breakfast", "wifi", "extras.welcome", "house_rules"}
    breakfast = rows["breakfast"]
    assert breakfast["text"].splitlines() == [
        "Frühstück von: 07:00",
        "Frühstück bis: 10:00",
        "Frühstückspreis pro Person: 16,00 EUR",
        "Frühstück im Preis enthalten: nein",
    ]
    assert breakfast["value"] == {
        "from": "07:00",
        "to": "10:00",
        "price_cents": 1600,
        "included": False,
    }
    assert breakfast["status"] == "draft" and breakfast["source"] == "questionnaire"
    assert rows["wifi"]["sensitivity"] == "verified_guest_only"
    assert (
        rows["extras.welcome"]["text"] == "Willkommen im Hotel Beispiel."
        and not rows["extras.welcome"]["value"]
    )
    english = {row["key"]: row for row in kb_rows(answers, "en")}
    assert "Breakfast price per person: EUR 16.00" in english["breakfast"]["text"]
    assert "Breakfast included in the rate: no" in english["breakfast"]["text"]
    missing = {item.key for item in missing_required(answers)}
    assert "a_hotel_name" in missing and "f_breakfast_from" not in missing
    assert kb_rows({}, "de") == []
