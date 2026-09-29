"""Golden suite (F35, 8.6): every case file through the Brain with the scripted model."""

from __future__ import annotations

from pathlib import Path

import pytest

from concierge.eval.golden import (
    APPENDIX_E,
    CROSS_CUTTING,
    LANGUAGES,
    CaseError,
    GoldenCase,
    load_cases,
    run_case,
    summary,
)

CASES = load_cases()


def test_case_counts_meet_the_minimum() -> None:
    scenarios = {c.scenario for c in CASES if c.group == APPENDIX_E}
    assert len(scenarios) == 14
    assert len([c for c in CASES if c.group == APPENDIX_E]) == 14 * len(LANGUAGES)
    assert len({c.scenario for c in CASES if c.group == CROSS_CUTTING}) == 20
    assert len(CASES) == 104 and len({c.id for c in CASES}) == 104
    assert all(c.feature.startswith("F") for c in CASES)


def test_loader_rejects_a_missing_language_variant(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(
        "turns:\n  - guest: {de: Hallo}\n    model: []\n", encoding="utf-8"
    )
    with pytest.raises(CaseError, match="no en variant"):
        load_cases(tmp_path)


@pytest.mark.feature("F35")
async def test_summary_counts_groups(tmp_path: Path) -> None:
    case = next(c for c in CASES if c.id == "e10_emergency:de")
    report = await run_case(case, tmp_path / "one.db")
    assert report.ok, report.problems
    totals = summary([report])
    assert totals == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "groups": {APPENDIX_E: {"total": 1, "passed": 1}},
    }
    assert report.to_dict()["turns"][0]["mode"] == "static"


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.id, marks=pytest.mark.feature(c.feature)) for c in CASES],
)
async def test_golden_case(case: GoldenCase, tmp_path: Path) -> None:
    report = await run_case(case, tmp_path / "golden.db")
    assert report.ok, "\n".join(report.problems)
