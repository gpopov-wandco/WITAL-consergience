"""Reports helpers (F27, F25): charts, redaction, sampling, PDF pages."""

from __future__ import annotations

import pytest

from concierge.reports.charts import bar_chart
from concierge.reports.pdf import simple_pdf
from concierge.reports.qa import kb_key_for, sampled
from concierge.reports.redact import redact

pytestmark = pytest.mark.feature("F27")


def test_bar_chart_is_inline_svg_with_escaped_labels() -> None:
    svg = bar_chart(["de", "en<script>"], [3, 1], title="Sprachen & Co")
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "Sprachen &amp; Co" in svg and "en&lt;script&gt;" in svg and "<script>" not in svg
    assert svg.count("<rect") == 2
    empty = bar_chart([], [], title="leer")
    assert "<rect" not in empty and "leer" in empty


def test_redaction_removes_identifiers_and_known_names() -> None:
    text = "Anna Muster, +49 151 12345678, anna@example.invalid, DE89 3704 0044 0532 0130 00, Zimmer 12"
    out = redact(text, names=["Anna Muster"])
    assert out == "[name], [phone], [e-mail], [iban], Zimmer 12"
    assert redact("call 0151-1234567 now", names=[]) == "call [phone] now"
    assert redact("Room 305 at 15:00", names=[]) == "Room 305 at 15:00"


def test_sampling_is_deterministic_near_five_percent() -> None:
    ids = [f"conv_{i:05d}" for i in range(4000)]
    chosen = [i for i in ids if sampled(i)]
    assert 140 <= len(chosen) <= 260  # about 5 percent
    assert [sampled(i) for i in ids[:50]] == [sampled(i) for i in ids[:50]]
    assert (
        kb_key_for("A:parking") == "parking" and kb_key_for("Q:late_arrival.key") == "late_arrival"
    )
    assert kb_key_for(None) == "" and kb_key_for("banner") == ""


def test_simple_pdf_renders_cyrillic_and_paginates() -> None:
    lines = [f"Zeile {i} — Привіт, Merhaba, Здравствуйте" for i in range(150)]
    pdf = simple_pdf(
        "Test", [("Abschnitt", lines)], subtitle="sub", table=[["a", "b"], ["1", "2"]], footer="f"
    )
    assert pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page") >= 3
