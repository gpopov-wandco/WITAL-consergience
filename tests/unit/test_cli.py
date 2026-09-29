"""CLI commands through typer's runner (offline, temporary database)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from tests.unit.test_profile import SAMPLE
from typer.testing import CliRunner

from concierge.cli import app
from concierge.settings import reset_settings_cache

pytestmark = pytest.mark.feature("F28")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, master_key: str, clean_env: None) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GC_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'cli.db').as_posix()}")
    monkeypatch.setenv("GC_MASTER_KEYS", json.dumps({"k1": master_key}))
    monkeypatch.setenv("GC_SIGNING_KEY", "s" * 48)
    reset_settings_cache()
    return tmp_path


def test_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert "concierge 0.1.0" in result.output


def test_db_and_tenant_and_secret_commands(env: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    current = runner.invoke(app, ["db", "current"])
    assert current.exit_code == 0 and "head:" in current.output
    profile = env / "tenant.yaml"
    profile.write_text(yaml.safe_dump(SAMPLE), encoding="utf-8")
    added = runner.invoke(app, ["tenant", "add", "--profile", str(profile)])
    assert added.exit_code == 0, added.output
    duplicate = runner.invoke(app, ["tenant", "add", "--profile", str(profile)])
    assert duplicate.exit_code == 1 and "already exists" in duplicate.output
    listed = runner.invoke(app, ["tenant", "list"])
    assert "hotel-beispiel" in listed.output and "pk_demo" in listed.output
    assert runner.invoke(app, ["tenant", "pause", "hotel-beispiel"]).exit_code == 0
    assert "(paused)" in runner.invoke(app, ["tenant", "list"]).output
    assert runner.invoke(app, ["tenant", "unpause", "hotel-beispiel"]).exit_code == 0
    missing = runner.invoke(app, ["tenant", "pause", "nope"])
    assert missing.exit_code == 1 and "unknown tenant" in missing.output
    stored = runner.invoke(
        app,
        ["secrets", "set", "hotel-beispiel", "wa_access_token", "--stdin"],
        input="EAA-secret\n",
    )
    assert stored.exit_code == 0, stored.output
    assert "EAA-secret" not in stored.output
    names = runner.invoke(app, ["secrets", "list", "hotel-beispiel"])
    assert names.output.strip() == "wa_access_token"
    prompted = runner.invoke(
        app, ["secrets", "set", "hotel-beispiel", "wa_app_secret"], input="hidden-value\n"
    )
    assert prompted.exit_code == 0 and "hidden-value" not in prompted.output
    rotated = runner.invoke(app, ["secrets", "rotate"])
    assert rotated.exit_code == 0 and "re-encrypted 0 rows" in rotated.output
    refused = runner.invoke(app, ["tenant", "activate", "hotel-beispiel"])
    assert refused.exit_code == 2 and "activation refused" in refused.output
    assert (
        runner.invoke(app, ["secrets", "delete", "hotel-beispiel", "wa_app_secret"]).output.strip()
        == "deleted"
    )


def test_tenant_add_reports_field_paths(env: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["db", "upgrade"])
    profile = env / "bad.yaml"
    profile.write_text(yaml.safe_dump(dict(SAMPLE, languages=["ru"])), encoding="utf-8")
    result = runner.invoke(app, ["tenant", "add", "--profile", str(profile)])
    assert result.exit_code == 1
    assert "languages:" in result.output


def test_setup_non_interactive_writes_env_and_link(env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "setup",
            "--non-interactive",
            "--env",
            str(env / "generated.env"),
            "--database-url",
            f"sqlite+aiosqlite:///{(env / 'setup.db').as_posix()}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "/admin/bootstrap/" in result.output
    text = (env / "generated.env").read_text(encoding="utf-8")
    assert "GC_MASTER_KEYS=" in text and "GC_SIGNING_KEY=" in text


def test_template_lint_and_list_commands(env: Path) -> None:
    runner = CliRunner()
    lint = runner.invoke(app, ["whatsapp", "templates", "lint"])
    assert lint.exit_code == 0 and "54 templates lint clean" in lint.output
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    profile = env / "tenant.yaml"
    profile.write_text(yaml.safe_dump(SAMPLE, allow_unicode=True), encoding="utf-8")
    assert runner.invoke(app, ["tenant", "add", "--profile", str(profile)]).exit_code == 0
    listed = runner.invoke(app, ["whatsapp", "templates", "list", "hotel-beispiel"])
    assert listed.exit_code == 0 and "no templates" in listed.output
    missing = runner.invoke(app, ["journeys", "preview", "hotel-beispiel", "NOPE"])
    assert missing.exit_code == 1 and "not found" in missing.output
    advance_help = runner.invoke(app, ["demo", "advance", "--help"])
    assert advance_help.exit_code == 0 and "--days" in advance_help.output
