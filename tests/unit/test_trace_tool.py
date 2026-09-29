"""The traceability tool (10.8): scans, the generated document and the gap exit code."""

from __future__ import annotations

from pathlib import Path

import pytest

from concierge.tools import trace

pytestmark = pytest.mark.feature("F33")


def test_scans_cover_every_feature_and_constraint(tmp_path: Path) -> None:
    modules = trace.scan_modules()
    tests = trace.scan_tests()
    assert set(trace.FEATURES) - set(trace.NOT_BUILT) <= set(modules)
    assert all(cid in modules for cid in trace.CONSTRAINTS)
    assert "tests/unit/test_trace_tool.py" in tests["F33"]
    text, gaps = trace.build(modules, tests)
    assert gaps == [] and "| F06 | KB intake |" in text and "0 gaps" in text
    out = tmp_path / "TRACEABILITY.md"
    assert trace.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == text


def test_gaps_and_not_built_items_are_reported() -> None:
    text, gaps = trace.build({"F01": {"src/x.py"}}, {"F01": {"tests/x.py"}, "C1": {"t"}})
    assert any(gap.startswith("F02:") for gap in gaps) and any(
        gap.startswith("C1:") for gap in gaps
    )
    assert "| F01 | Conversation engine | `src/x.py` | `tests/x.py` | built and tested |" in text
    assert "not built: (S) HubSpot" in text
    assert trace._fmt([]) == "—" and trace._fmt([f"m{i}" for i in range(10)]).endswith("and 2 more")
