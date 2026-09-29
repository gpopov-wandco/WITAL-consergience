"""``concierge check``: every static gate (10.2) in one table.

Implements: F33, C13

Gates: ruff check, ruff format, mypy (strict packages), bandit, pip-audit, pip check,
secret scan, import boundary, C1 allow-list, forbidden markers, env parity, raw SQL.
Each gate reports pass or fail with the first lines of its output; the exit code is
the number of failed gates.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class GateResult:
    name: str
    ok: bool
    detail: str


def _run(name: str, args: list[str], *, cwd: Path = ROOT, timeout: int = 900) -> GateResult:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return GateResult(name, False, f"{args[0]} is not installed in this environment")
    except subprocess.TimeoutExpired:
        return GateResult(name, False, f"timed out after {timeout}s")
    output = (proc.stdout + proc.stderr).strip()
    tail = "\n".join(output.splitlines()[-6:]) if output else ""
    return GateResult(name, proc.returncode == 0, tail)


def _pytest(name: str, selector: str) -> GateResult:
    return _run(
        name,
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-header",
            "tests/unit/test_repo_rules.py",
            "-k",
            selector,
        ],
    )


def run_all(*, skip_audit: bool = False) -> list[GateResult]:
    python = sys.executable
    results = [
        _run("ruff check", [python, "-m", "ruff", "check", "src", "tests"]),
        _run("ruff format", [python, "-m", "ruff", "format", "--check", "src", "tests"]),
        _run("mypy --strict", [python, "-m", "mypy"]),
        _run("bandit", [python, "-m", "bandit", "-q", "-r", "src", "-c", "pyproject.toml"]),
        _run("pip check", [python, "-m", "pip", "check"]),
        _pytest("secret scan", "test_no_secrets_in_repository"),
        _pytest("import boundary", "test_import_boundary_only_adapters_import_vendor_sdks"),
        _pytest(
            "C1 allow-list",
            "test_c1_only_allowed_non_python_files or test_c1_widget_javascript_limits",
        ),
        _pytest("forbidden markers", "test_no_forbidden_markers_in_shipped_code"),
        _pytest("env parity", "test_env_example_matches_settings_both_ways"),
        _pytest("raw SQL", "test_no_raw_sql_outside_migrations_and_pragmas"),
    ]
    if skip_audit:
        results.append(GateResult("pip-audit", True, "skipped on request (offline)"))
    else:
        results.append(
            _run(
                "pip-audit",
                [
                    python,
                    "-m",
                    "pip_audit",
                    "--skip-editable",
                    "--progress-spinner",
                    "off",
                ],
                timeout=600,
            )
        )
    return results


def render(results: list[GateResult]) -> str:
    width = max(len(r.name) for r in results)
    lines = [f"{'gate'.ljust(width)}  status  detail", "-" * (width + 30)]
    for r in results:
        status = "pass" if r.ok else "FAIL"
        detail = (
            r.detail.splitlines()[0]
            if (r.detail and not r.ok)
            else (r.detail if r.detail.startswith("skipped") else "")
        )
        lines.append(f"{r.name.ljust(width)}  {status:6}  {detail}")
    failed = [r for r in results if not r.ok]
    lines.append("-" * (width + 30))
    lines.append(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    for r in failed:
        lines.append(f"\n[{r.name}]\n{r.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    results = run_all(skip_audit="--skip-audit" in args)
    print(render(results))
    return sum(1 for r in results if not r.ok)


if __name__ == "__main__":
    raise SystemExit(main())
