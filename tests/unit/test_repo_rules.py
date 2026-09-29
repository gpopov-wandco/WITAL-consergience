"""Repository rules: C1 file allow-list, import boundary, forbidden markers, secrets, env parity.

These tests are also run by ``concierge check``.
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from concierge.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "concierge"
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    "var",
    "dist",
    "build",
    "test-results",
    ".playwright",
    "node_modules",
}

pytestmark = pytest.mark.feature("F33")


def repo_files() -> list[Path]:
    """Files git would commit: tracked plus untracked but not ignored (runtime output such
    as ``.env``, packs and compliance folders is ignored and never part of the repo)."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        listed = b""
    if listed:
        return [
            ROOT / name
            for name in listed.decode("utf-8").split("\0")
            if name and (ROOT / name).is_file()
        ]
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(
            part in EXCLUDED_DIRS or part.endswith(".egg-info")
            for part in path.relative_to(ROOT).parts
        ):
            continue
        if path.is_file():
            files.append(path)
    return files


ALLOWED_EXACT = {
    ".env.example",
    ".gitignore",
    ".gitattributes",
    "Dockerfile",
    "compose.yaml",
    "Caddyfile",
    "requirements.lock",
    "pyproject.toml",
}
ALLOWED_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".json", ".j2", ".html", ".ttf"}
ALLOWED_RELATIVE = {
    "src/concierge/static/widget/loader.js",
    "src/concierge/static/widget/gc-widget.js",
    "src/concierge/static/widget/gc-widget.css",
    "src/concierge/static/vendor/htmx.min.js",
    "src/concierge/static/vendor/htmx-LICENSE.txt",
    "src/concierge/static/admin.css",
    "src/concierge/static/fonts/Inter-OFL.txt",
    "src/concierge/static/fonts/Syne-OFL.txt",
    "deploy/systemd/concierge.service",
}


def test_c1_only_allowed_non_python_files() -> None:
    offenders = []
    for path in repo_files():
        rel = path.relative_to(ROOT).as_posix()
        if path.name in ALLOWED_EXACT or rel in ALLOWED_RELATIVE or path.suffix in ALLOWED_SUFFIXES:
            continue
        offenders.append(rel)
    assert offenders == [], f"files outside the C1 allow-list: {offenders}"


def test_c1_widget_javascript_limits() -> None:
    widget = SRC / "static" / "widget"
    total = 0
    for name in ("loader.js", "gc-widget.js", "gc-widget.css"):
        path = widget / name
        if path.exists():
            total += len(path.read_text(encoding="utf-8").splitlines())
    assert total <= 700, f"widget files have {total} lines, limit 700"


def test_no_gc_package_shadowing_stdlib() -> None:
    assert not (ROOT / "src" / "gc").exists()
    import gc

    assert callable(gc.collect)


ADAPTER_MODULES = {
    "llm/anthropic_adapter.py": {"anthropic", "httpx2"},
    "channels/whatsapp/graph.py": {"httpx2"},
    "availability/vendor_http.py": {"httpx2"},
    "availability/beds24.py": {"httpx2"},
    "availability/apaleo.py": {"httpx2"},
    "availability/mews.py": {"httpx2"},
    "availability/ical.py": {"httpx2"},
    "availability/smoobu.py": {"httpx2"},
    "availability/ibelsa.py": {"httpx2"},
    "notify/email.py": {"httpx2"},
    "registration/stripe_proof.py": {"httpx2"},
    "channels/voice/retell.py": {"httpx2"},
    "channels/voice/twilio_cr.py": {"httpx2"},
    "channels/voice/openai_compat.py": {"httpx2"},
    "sinks/hubspot.py": {"httpx2"},
    "sinks/stripe_usage.py": {"httpx2"},
    "security/ssrf.py": {"httpx2"},
    "tools/livechecks.py": {"httpx2", "anthropic"},
    "demo/wa_sim.py": {"httpx2"},
    "demo/voice_sim.py": {"httpx2"},
}
VENDOR_ROOTS = {
    "anthropic",
    "httpx2",
    "httpx",
    "retell",
    "twilio",
    "stripe",
    "hubspot",
    "requests",
    "aiohttp",
    "urllib3",
}


def test_import_boundary_only_adapters_import_vendor_sdks() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        vendor = imported & VENDOR_ROOTS
        if vendor and not vendor <= ADAPTER_MODULES.get(rel, set()):
            offenders.append((rel, sorted(vendor)))
    assert offenders == [], f"core modules importing vendor SDKs: {offenders}"


FORBIDDEN_MARKERS = re.compile(r"\b(TODO|FIXME|XXX)\b")


def test_no_forbidden_markers_in_shipped_code() -> None:
    offenders = []
    for base in ("src", "deploy", "examples"):
        for path in (ROOT / base).rglob("*"):
            if path.is_file() and path.suffix in {
                ".py",
                ".md",
                ".yaml",
                ".j2",
                ".html",
                ".js",
                ".css",
                ".toml",
                ".json",
                ".service",
            }:
                text = path.read_text(encoding="utf-8", errors="ignore")
                if FORBIDDEN_MARKERS.search(text):
                    offenders.append(path.relative_to(ROOT).as_posix())
                if re.search(r"raise NotImplementedError\s*(\(\s*\))?\s*$", text, re.M):
                    offenders.append(
                        path.relative_to(ROOT).as_posix() + " (bare NotImplementedError)"
                    )
    assert offenders == [], offenders


SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{20,}"),
    re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bEAA[A-Za-z0-9]{40,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bxox[bp]-[A-Za-z0-9\-]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"\bre_[A-Za-z0-9]{20,}"),
    re.compile(r"\bkey_[0-9a-f]{32}\b"),
]


def test_no_secrets_in_repository() -> None:
    offenders = []
    for path in repo_files():
        if path.suffix in {".ttf", ".png", ".pdf"} or path.name == "requirements.lock":
            continue
        if path == Path(__file__):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                offenders.append((path.relative_to(ROOT).as_posix(), pattern.pattern))
    assert offenders == [], offenders


def test_env_example_matches_settings_both_ways() -> None:
    keys = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    fields = set(Settings(_env_file=None).env_var_names())
    assert keys == fields, (
        f"only in .env.example: {sorted(keys - fields)}; only in Settings: {sorted(fields - keys)}"
    )


def test_no_raw_sql_outside_migrations_and_pragmas() -> None:
    offenders = []
    allowed = {"db/migrations", "db/session.py"}
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if any(rel.startswith(a) for a in allowed):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(
            r"\btext\(\s*[\"']\s*(SELECT|INSERT|UPDATE|DELETE|PRAGMA|CREATE|DROP|ALTER)\b",
            text,
            re.I,
        ):
            offenders.append(rel)
        if re.search(r"exec_driver_sql\(", text):
            offenders.append(rel)
    assert offenders == [], offenders


def test_htmx_vendored_file_matches_verified_digest() -> None:
    path = SRC / "static" / "vendor" / "htmx.min.js"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
    assert (
        (SRC / "static" / "vendor" / "htmx-LICENSE.txt")
        .read_text(encoding="utf-8")
        .startswith("Zero-Clause BSD")
    )


@pytest.mark.parametrize(
    ("font", "chars", "expected"),
    [
        ("Inter-Regular.ttf", "İıışğçöüŞĞÇÖÜ", True),
        ("Inter-Regular.ttf", "іїєґІЇЄҐ", True),
        ("Inter-Bold.ttf", "ЖжЩщЪъЫыЭэЮюЯя", True),
        ("Syne-Bold.ttf", "İıışğçöüŞĞÇÖÜ", True),
        ("Syne-Bold.ttf", "іїєґ", False),
    ],
)
def test_font_glyph_coverage(font: str, chars: str, expected: bool) -> None:
    cmap = TTFont(SRC / "static" / "fonts" / font).getBestCmap()
    assert all(ord(c) in cmap for c in chars) is expected


def test_every_module_docstring_declares_implements() -> None:
    missing = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if path.name == "__init__.py" or rel.startswith("db/migrations"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        doc = ast.get_docstring(tree) or ""
        if "Implements:" not in doc:
            missing.append(rel)
    assert missing == [], f"modules without 'Implements:' in their docstring: {missing}"
