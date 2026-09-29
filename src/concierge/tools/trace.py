"""Traceability (10.8): constraints and features mapped to modules and tests.

Implements: 10.8, F33

``python -m concierge.tools.trace`` writes ``docs/TRACEABILITY.md`` from the
``Implements:`` lines of every module docstring, the ``pytest.mark.feature`` markers of
every test module and function, and the ``feature`` field of every golden case. It
exits with 1 when a feature has no implementing module or no test, except the (S)
items declared as not built with a reason.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "concierge"
TESTS = ROOT / "tests"
CASES = SRC / "eval" / "cases"
OUTPUT = ROOT / "docs" / "TRACEABILITY.md"

FEATURES: dict[str, str] = {
    "F01": "Conversation engine",
    "F02": "Agent tools",
    "F03": "Guardrails",
    "F04": "Deterministic responder",
    "F05": "Knowledge base",
    "F06": "KB intake",
    "F07": "Availability core",
    "F08": "PMS adapters",
    "F09": "Fallback sources",
    "F10": "Booking handoff",
    "F11": "Arrival practicalities",
    "F12": "Reservation intake",
    "F13": "Journey engine",
    "F14": "WhatsApp templates",
    "F15": "Registration module",
    "F16": "Honest upsells",
    "F17": "Handoff and staff inbox",
    "F18": "E-mail and notifications",
    "F19": "Hotel admin UI",
    "F20": "Operator console",
    "F21": "Tenant config and client packs",
    "F22": "Widget and branding",
    "F23": "Multilingual behaviour",
    "F24": "Metering and cost",
    "F25": "GDPR tooling",
    "F26": "Voice fallback",
    "F27": "Analytics and QA",
    "F28": "Doctor and setup wizard",
    "F29": "Demo and simulator",
    "F30": "Backup and restore",
    "F31": "Security foundation",
    "F32": "Observability",
    "F33": "Deployment and docs",
    "F34": "Handout and compliance pack",
    "F35": "Evaluation harness",
    "F36": "(S) Optional sinks",
}
CONSTRAINTS: dict[str, str] = {
    "C1": "Python only; explicit non-Python allow-list",
    "C2": "Configuration through environment and tenant profile",
    "C3": "Secrets never in git, logs, prompts, fixtures, errors or chat",
    "C4": "No live third-party call with real guest data during the build",
    "C5": "No invented hotel facts",
    "C6": "Grounded answers only",
    "C7": "AI disclosure inserted by code",
    "C8": "No card or ID data through chat",
    "C9": "Opt-in, quiet hours, STOP words",
    "C10": "Legal texts ship as drafts; never 'compliant'",
    "C11": "No n8n or WITALCO-internal dependency",
    "C12": "No unverifiable claims in client-facing text",
    "C13": "Pinned, hash-locked dependencies",
    "C14": "UTF-8 everywhere; package named concierge",
    "C15": "Tenant isolation",
    "C16": "Append-only usage, audit and deletion logs; injected clock",
    "C17": "Never claims to be human; never confirms bookings or prices",
}
NOT_BUILT: dict[str, str] = {
    "F36": "(S) HubSpot and Stripe usage sinks were not built in this iteration; "
    "listed with reasons in docs/BUILD_STATUS.md",
}
# constraints guarded by tests that do not name them in a marker
CONSTRAINT_TESTS: dict[str, list[str]] = {
    "C1": ["tests/unit/test_repo_rules.py"],
    "C2": [
        "tests/unit/test_repo_rules.py",
        "tests/unit/test_profile.py",
        "tests/unit/test_foundation.py",
    ],
    "C3": [
        "tests/unit/test_repo_rules.py",
        "tests/unit/test_security.py",
        "tests/integration/test_packs.py",
    ],
    "C4": ["tests/conftest.py"],
    "C5": ["tests/integration/test_kb.py", "tests/unit/test_tools_validators.py"],
    "C6": ["tests/unit/test_tools_validators.py", "tests/golden/test_golden.py"],
    "C7": [
        "tests/integration/test_brain.py",
        "tests/integration/test_widget.py",
        "tests/integration/test_voice.py",
    ],
    "C8": ["tests/integration/test_brain.py", "tests/golden/test_golden.py"],
    "C9": ["tests/integration/test_journeys.py", "tests/integration/test_brain.py"],
    "C10": ["tests/unit/test_legal.py"],
    "C11": ["tests/unit/test_repo_rules.py"],
    "C12": ["tests/unit/test_legal.py", "tests/unit/test_tools_validators.py"],
    "C13": ["tests/unit/test_repo_rules.py"],
    "C14": ["tests/unit/test_repo_rules.py", "tests/unit/test_foundation.py"],
    "C15": ["tests/integration/test_db.py", "tests/property/test_properties.py"],
    "C16": ["tests/integration/test_db.py", "tests/unit/test_metering.py"],
    "C17": ["tests/unit/test_tools_validators.py", "tests/golden/test_golden.py"],
}
_IMPLEMENTS = re.compile(r"Implements:\s*(.+)")
_MARK = re.compile(r"pytest\.mark\.feature\(\s*[\"'](F\d\d)[\"']\s*\)")
_ID = re.compile(r"^(F\d\d|C\d{1,2})$")


@dataclass
class Trace:
    modules: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    tests: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


def scan_modules(src: Path = SRC) -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for path in sorted(src.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        doc = ast.get_docstring(tree) or ""
        match = _IMPLEMENTS.search(doc)
        if not match:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for token in match.group(1).split(","):
            token = token.strip().rstrip(".")
            if _ID.match(token):
                found[token].add(rel)
    return found


def scan_tests(tests: Path = TESTS, cases: Path = CASES) -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for path in sorted(tests.rglob("test_*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for feature in _MARK.findall(path.read_text(encoding="utf-8")):
            found[feature].add(rel)
    golden = tests / "golden" / "test_golden.py"
    if golden.exists() and cases.is_dir():
        rel = golden.relative_to(ROOT).as_posix()
        for case in sorted(cases.glob("*.yaml")):
            data = yaml.safe_load(case.read_text(encoding="utf-8")) or {}
            feature = str(data.get("feature", ""))
            if _ID.match(feature):
                found[feature].add(f"{rel} ({case.name})")
    for constraint, files in CONSTRAINT_TESTS.items():
        for name in files:
            if (ROOT / name).exists():
                found[constraint].add(name)
    return found


def build(modules: dict[str, set[str]], tests: dict[str, set[str]]) -> tuple[str, list[str]]:
    """The Markdown document and the list of gaps (features without code or test)."""
    gaps: list[str] = []
    lines = [
        "# Traceability",
        "",
        "Generated by `python -m concierge.tools.trace` from the `Implements:` lines of the",
        "module docstrings, the `pytest.mark.feature` markers and the golden case files.",
        "Do not edit by hand.",
        "",
        "## Constraints",
        "",
        "| Id | Constraint | Modules | Tests | Status |",
        "|---|---|---|---|---|",
    ]
    for cid, title in CONSTRAINTS.items():
        mods = sorted(modules.get(cid, set()))
        tsts = sorted(tests.get(cid, set()))
        status = "guarded" if mods and tsts else "gap"
        if status == "gap":
            gaps.append(f"{cid}: modules {len(mods)}, tests {len(tsts)}")
        lines.append(f"| {cid} | {title} | {_fmt(mods)} | {_fmt(tsts)} | {status} |")
    lines += [
        "",
        "## Features",
        "",
        "| Id | Feature | Modules | Tests | Status |",
        "|---|---|---|---|---|",
    ]
    for fid, title in FEATURES.items():
        mods = sorted(modules.get(fid, set()))
        tsts = sorted(tests.get(fid, set()))
        if fid in NOT_BUILT:
            status = "not built: " + NOT_BUILT[fid]
        elif mods and tsts:
            status = "built and tested"
        else:
            status = "gap"
            gaps.append(f"{fid}: modules {len(mods)}, tests {len(tsts)}")
        lines.append(f"| {fid} | {title} | {_fmt(mods)} | {_fmt(tsts)} | {status} |")
    lines += [
        "",
        f"{len(FEATURES) - len(NOT_BUILT)} features built and tested, {len(NOT_BUILT)} not built, "
        f"{len(gaps)} gaps.",
        "",
    ]
    return "\n".join(lines), gaps


def _fmt(items: list[str], limit: int = 8) -> str:
    if not items:
        return "—"
    shown = [f"`{i}`" for i in items[:limit]]
    if len(items) > limit:
        shown.append(f"and {len(items) - limit} more")
    return ", ".join(shown)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output = OUTPUT
    if "--out" in args:
        output = Path(args[args.index("--out") + 1])
    text, gaps = build(scan_modules(), scan_tests())
    output.write_text(text, encoding="utf-8")
    print(f"wrote {output.relative_to(ROOT) if output.is_relative_to(ROOT) else output}")
    for gap in gaps:
        print(f"gap: {gap}")
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
