"""The handout and the compliance pack (F34): page count, fonts, text, no leaks."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader

from concierge.eval.golden import GoldenStack
from concierge.tools.compliance import STAMP, build_pack_files, pack_pdf, write_pack
from concierge.tools.handout import HANDOUT_COMMANDS, MAX_PAGES, build_handout

pytestmark = pytest.mark.feature("F34")


def test_handout_pages_fonts_and_text(tmp_path: Path) -> None:
    out = build_handout(tmp_path / "handout.pdf", handout_date=date(2026, 9, 29))
    reader = PdfReader(str(out))
    assert 1 <= len(reader.pages) <= MAX_PAGES
    text = "\n".join(page.extract_text() for page in reader.pages)
    for heading in (
        "Part A. How to test",
        "Part B. How to hand it over",
        "Part C. How to customise per client",
        "Appendix",
    ):
        assert heading in text
    assert "{" not in text and "}" not in text  # no placeholders
    assert "VERIFY" not in text and "!" not in text
    assert "compliant" not in text.lower() and "legally safe" not in text.lower()
    for command in HANDOUT_COMMANDS:
        assert command.split(" --")[0] in text.replace("\n", " "), command
    fonts: dict[str, bool] = {}
    for page in reader.pages:
        resources = page.get("/Resources", {})
        for _name, ref in (resources.get("/Font") or {}).items():
            font = ref.get_object()
            base = str(font.get("/BaseFont"))
            descriptor = font.get("/FontDescriptor")
            fonts[base] = descriptor is not None and "/FontFile2" in descriptor.get_object()
    assert any("Inter" in name and embedded for name, embedded in fonts.items())
    assert any("Syne" in name and embedded for name, embedded in fonts.items())
    assert reader.metadata is not None and "WITALCO" in str(reader.metadata.title)


async def test_compliance_pack_files_and_pdf(tmp_path: Path) -> None:
    stack = await GoldenStack.create(tmp_path / "pack.db", responder_only=True)
    try:
        services = stack.services
        profile = services.tenants.profile_of(stack.tenant)
        rows = await services.legal.rows(stack.tenant.id)
        files = build_pack_files(profile, rows, today=date(2026, 9, 29))
        names = [f.name for f in files]
        assert names == [
            "README.md",
            "subprocessors.md",
            "retention.md",
            "tom_checklist.md",
            "ai_disclosures.md",
            "record_of_processing.md",
            "legal_texts.md",
        ]
        by_name = {f.name: f.text for f in files}
        assert all(STAMP in text for text in by_name.values())
        assert (
            "Meta Platforms Ireland" in by_name["subprocessors.md"]
            and "VERIFY" in by_name["subprocessors.md"]
        )
        assert "Retell" not in by_name["subprocessors.md"]  # voice off in the fixture
        assert "30 days after the message" in by_name["retention.md"]
        for lang in profile.languages:
            assert f"### {lang}" in by_name["ai_disclosures.md"]
        assert "Hotel Beispiel" in by_name["ai_disclosures.md"]
        assert "draft_unreviewed" in by_name["legal_texts.md"]
        assert "compliant" not in " ".join(by_name.values()).lower().replace("compliance pack", "")
        pdf = pack_pdf(files, profile, today=date(2026, 9, 29))
        assert pdf.startswith(b"%PDF")
        written = write_pack(tmp_path / "out", files, pdf, "hotel-beispiel")
        assert (tmp_path / "out" / "compliance-pack-hotel-beispiel.pdf").exists()
        assert len(written) == 8
        text = "\n".join(p.extract_text() for p in PdfReader(str(written[-1])).pages)
        assert STAMP in text and "Sub-processor" in text
    finally:
        await stack.close()
