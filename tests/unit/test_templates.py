"""Template registry (F14): lint table, standard set, rendering, send and creation payloads."""

from __future__ import annotations

from dataclasses import replace

import pytest

from concierge.templates.service import (
    LANGS,
    STANDARD_NAMES,
    TemplateDefinition,
    lint,
    lint_all,
    load_standard,
    meta_components,
    render,
    send_components,
)

pytestmark = pytest.mark.feature("F14")


def _definition(**kw: object) -> TemplateDefinition:
    base = {
        "name": "gc_test",
        "lang": "de",
        "category": "utility",
        "body": "Hallo {{guest_name}}, Ihr Aufenthalt im {{hotel}}. Antworten Sie mit STOP.",
        "variables": ("guest_name", "hotel"),
        "samples": {"guest_name": "Anna", "hotel": "Hotel Beispiel"},
    }
    base.update(kw)
    return TemplateDefinition(**base)  # type: ignore[arg-type]


def _rules(definition: TemplateDefinition) -> set[str]:
    return {issue.rule for issue in lint(definition)}


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"name": "GC_Test"}, "name"),
        ({"name": "gc-test"}, "name"),
        ({"category": "promo"}, "category"),
        ({"body": "x" * 1025 + " STOP"}, "body_length"),
        ({"body": "   "}, "body_empty"),
        ({"header": "h" * 61}, "header_length"),
        ({"footer": "f" * 61}, "footer_length"),
        ({"buttons": tuple((f"b{i}", f"B{i}") for i in range(4))}, "buttons"),
        ({"buttons": (("b1", "x" * 21),)}, "button_length"),
        ({"buttons": (("Bad Id", "ok"),)}, "button_id"),
        ({"body": "Hallo {{guest_name}} {{extra}} STOP"}, "variables"),
        ({"body": "Hallo {{1}} {{guest_name}} {{hotel}} STOP"}, "variables"),
        ({"samples": {"guest_name": "Anna"}}, "samples"),
        ({"body": "Hallo {{guest_name}}, {{hotel}}: 20 % Rabatt! STOP"}, "promotional"),
        ({"first_contact": True}, "disclosure"),
        ({"body": "Hallo {{guest_name}}, {{hotel}}."}, "stop"),
    ],
)
def test_lint_table(change: dict[str, object], rule: str) -> None:
    assert rule in _rules(_definition(**change))


def test_lint_accepts_marketing_cues_and_own_button_limit() -> None:
    assert _rules(_definition()) == set()
    marketing = _definition(
        category="marketing", body="{{hotel}}: unser Angebot für {{guest_name}}. STOP"
    )
    assert "promotional" not in _rules(marketing)
    twenty = _definition(buttons=(("ok", "x" * 20),))
    assert "button_length" not in _rules(twenty)
    assert "button_length" in _rules(_definition(buttons=(("ok", "x" * 21),)))
    assert "button_length" not in {
        i.rule for i in lint(_definition(buttons=(("ok", "x" * 25),)), own_buttons=False)
    }


def test_standard_set_is_complete_and_clean() -> None:
    definitions = load_standard()
    assert len(definitions) == len(STANDARD_NAMES) * len(LANGS)
    assert {d.name for d in definitions} == set(STANDARD_NAMES)
    assert lint_all(definitions) == []
    first = [d for d in definitions if d.name == "gc_booking_followup"]
    assert all(d.first_contact for d in first) and len(first) == 6
    review = [d for d in definitions if d.name == "gc_review_request"]
    assert all(d.category == "marketing" for d in review)
    for definition in definitions:
        assert "STOP" in definition.body
        assert definition.buttons == () or len(definition.buttons) in {2, 3}
    eta = next(d for d in definitions if d.name == "gc_prearrival_2d" and d.lang == "tr")
    assert [i for i, _ in eta.buttons] == ["eta_before_18", "eta_18_22", "eta_after_22"]


def test_render_and_payload_components() -> None:
    definition = next(d for d in load_standard() if d.name == "gc_prearrival_2d" and d.lang == "en")
    text = render(definition, {"hotel": "Hotel Beispiel", "arrival": "03 Oct 2026"})
    assert text.startswith("Your arrival at Hotel Beispiel is on 03 Oct 2026.") and "{{" not in text
    components = send_components(definition, {"hotel": "Hotel Beispiel", "arrival": "03 Oct 2026"})
    assert components[0] == {
        "type": "body",
        "parameters": [
            {"type": "text", "parameter_name": "hotel", "text": "Hotel Beispiel"},
            {"type": "text", "parameter_name": "arrival", "text": "03 Oct 2026"},
        ],
    }
    assert (
        components[1]["sub_type"] == "quick_reply"
        and components[1]["parameters"][0]["payload"] == "gc:eta_before_18"
    )
    assert [c["index"] for c in components[1:]] == ["0", "1", "2"]
    creation = meta_components(definition)
    assert (
        creation[0]["type"] == "BODY"
        and creation[0]["example"]["body_text_named_params"][0]["param_name"] == "hotel"
    )
    assert creation[1]["buttons"][0] == {"type": "QUICK_REPLY", "text": "Before 18:00"}
    plain = replace(definition, buttons=())
    assert len(meta_components(plain)) == 1 and len(send_components(plain, {})) == 1
