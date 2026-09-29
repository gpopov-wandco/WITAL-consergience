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
    assert lint.exit_code == 0 and "60 templates lint clean" in lint.output
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


def test_pack_export_validate_diff_import(env: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    profile = env / "tenant.yaml"
    profile.write_text(yaml.safe_dump(SAMPLE), encoding="utf-8")
    assert runner.invoke(app, ["tenant", "add", "--profile", str(profile)]).exit_code == 0
    target = env / "pack"
    exported = runner.invoke(app, ["pack", "export", "hotel-beispiel", "--out", str(target)])
    assert exported.exit_code == 0, exported.output
    assert "tenant.yaml" in exported.output and (target / "kb" / "entries.yaml").is_file()
    valid = runner.invoke(app, ["pack", "validate", str(target)])
    assert valid.exit_code == 0 and "pack ok" in valid.output
    diff = runner.invoke(app, ["pack", "diff", str(target)])
    assert diff.exit_code == 0 and "no changes" in diff.output

    data = yaml.safe_load((target / "tenant.yaml").read_text(encoding="utf-8"))
    data["name"] = "Hotel Beispiel Neu"
    (target / "tenant.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    diff = runner.invoke(app, ["pack", "diff", str(target)])
    assert diff.exit_code == 0 and "profile.name: changed" in diff.output
    dry = runner.invoke(app, ["pack", "import", str(target)])
    assert dry.exit_code == 0 and "dry run" in dry.output
    assert "profile.name: changed" in runner.invoke(app, ["pack", "diff", str(target)]).output
    applied = runner.invoke(app, ["pack", "import", str(target), "--apply"])
    assert applied.exit_code == 0 and "applied" in applied.output
    assert "no changes" in runner.invoke(app, ["pack", "diff", str(target)]).output

    archive = env / "hotel-beispiel.zip"
    zipped = runner.invoke(app, ["pack", "export", "hotel-beispiel", "--out", str(archive)])
    assert zipped.exit_code == 0 and archive.is_file()
    assert runner.invoke(app, ["pack", "validate", str(archive)]).exit_code == 0

    data["whatsapp"]["access_token"] = "leaked"
    (target / "tenant.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    invalid = runner.invoke(app, ["pack", "validate", str(target)])
    assert invalid.exit_code == 1 and "secrets never travel in a pack" in invalid.output
    assert runner.invoke(app, ["pack", "import", str(target), "--apply"]).exit_code == 1
    assert runner.invoke(app, ["pack", "validate", str(env / "nowhere")]).exit_code == 1


def test_voice_provision_dry_run(env: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    profile = env / "tenant.yaml"
    data = dict(SAMPLE)
    data["voice"] = {
        **dict(SAMPLE["voice"]),
        "provider": "retell",
        "number": "+4969000009",
        "voice_id": "voice_x",
    }
    profile.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert runner.invoke(app, ["tenant", "add", "--profile", str(profile)]).exit_code == 0
    result = runner.invoke(app, ["voice", "provision", "hotel-beispiel", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "custom-llm" in result.output and "/voice/retell/llm/" in result.output
    assert "dry run" in result.output and "human steps" in result.output
    assert "<hidden>" in result.output and "sip_trunk_auth_password" in result.output
    missing = runner.invoke(app, ["voice", "provision", "no-such-hotel", "--dry-run"])
    assert missing.exit_code == 1


def test_backup_restore_usage_and_gdpr_commands(env: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    profile = env / "tenant.yaml"
    profile.write_text(yaml.safe_dump(SAMPLE), encoding="utf-8")
    assert runner.invoke(app, ["tenant", "add", "--profile", str(profile)]).exit_code == 0
    import base64

    os_key = base64.b64encode(b"\x07" * 32).decode()
    import os

    os.environ["GC_BACKUP_KEY"] = os_key
    reset_settings_cache()
    out = env / "backups"
    made = runner.invoke(app, ["backup", "--out", str(out)])
    assert made.exit_code == 0, made.output
    assert "backup written to" in made.output and list(out.glob("concierge-*.sqlite.enc"))
    restored = runner.invoke(app, ["restore-test", "--out", str(out)])
    assert restored.exit_code == 0, restored.output
    assert "restore test ok" in restored.output and "tenant" in restored.output
    assert "backup" in restored.output and "restored" in restored.output  # both count columns
    from concierge.settings import get_settings
    from concierge.tools import backup as backup_tool

    key = get_settings().backup_key()
    plain, _kind, _manifest = backup_tool.decrypt(next(out.glob("*.enc")).read_bytes(), key)
    forged = out / "concierge-20260929T235959Z.sqlite.enc"
    forged.write_bytes(
        backup_tool.encrypt(plain, key, kind=b"sqlite", manifest={"tables": {"tenant": 3}})
    )
    lost = runner.invoke(app, ["restore-test", "--file", str(forged)])
    assert (
        lost.exit_code == 2
        and "row counts differ" in lost.output
        and "tenant 3 -> 1" in lost.output
    )
    usage = runner.invoke(
        app, ["usage", "export", "--tenant", "hotel-beispiel", "--month", "2026-09"]
    )
    assert usage.exit_code == 0 and '"month": "2026-09"' in usage.output
    csv_out = env / "usage.csv"
    assert (
        runner.invoke(
            app,
            [
                "usage",
                "export",
                "--tenant",
                "hotel-beispiel",
                "--month",
                "2026-09",
                "--format",
                "csv",
                "--out",
                str(csv_out),
            ],
        ).exit_code
        == 0
    )
    assert csv_out.read_text(encoding="utf-8").startswith("month;kind")
    assert (
        runner.invoke(
            app, ["usage", "export", "--tenant", "hotel-beispiel", "--month", "nope"]
        ).exit_code
        == 1
    )
    search = runner.invoke(
        app, ["gdpr", "search", "--tenant", "hotel-beispiel", "--phone", "+49 151 99999999"]
    )
    assert search.exit_code == 1 and "guests 0" in search.output
    retention = runner.invoke(app, ["gdpr", "retention"])
    assert retention.exit_code == 0 and "hotel-beispiel: nothing to delete" in retention.output


@pytest.mark.feature("F06")
def test_kb_intake_and_questionnaire_commands(env: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    profile = env / "tenant.yaml"
    profile.write_text(yaml.safe_dump(SAMPLE), encoding="utf-8")
    assert runner.invoke(app, ["tenant", "add", "--profile", str(profile)]).exit_code == 0

    items = runner.invoke(app, ["kb", "questionnaire", "items", "--lang", "de"])
    assert items.exit_code == 0 and items.output.count("\n") == 85 + 2
    assert "Name des Hauses" in items.output

    answers = env / "answers.yaml"
    answers.write_text(
        yaml.safe_dump({"answers": {"a_phone": "not a number", "nope": 1}}), encoding="utf-8"
    )
    rejected = runner.invoke(
        app, ["kb", "questionnaire", "import", str(answers), "--tenant", "hotel-beispiel"]
    )
    assert rejected.exit_code == 1
    assert "answers.a_phone: expected an international number" in rejected.output
    assert "answers.nope: unknown item" in rejected.output
    answers.write_text(
        yaml.safe_dump(
            {
                "answers": {
                    "a_hotel_name": "Hotel Beispiel",
                    "d_checkin_from": "15:00",
                    "f_breakfast_price": "16,00",
                    "f_wifi_network": "Beispiel-Gast",
                }
            }
        ),
        encoding="utf-8",
    )
    stored = runner.invoke(
        app, ["kb", "questionnaire", "import", str(answers), "--tenant", "hotel-beispiel"]
    )
    assert stored.exit_code == 0, stored.output
    assert "4 answers stored" in stored.output and "required items still open" in stored.output
    out = env / "export.yaml"
    exported = runner.invoke(
        app, ["kb", "questionnaire", "export", "--tenant", "hotel-beispiel", "--out", str(out)]
    )
    assert exported.exit_code == 0 and "4 answers" in exported.output
    document = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert (
        document["answers"]["f_breakfast_price"] == "16.00"
        and document["tenant"] == "hotel-beispiel"
    )
    applied = runner.invoke(app, ["kb", "questionnaire", "apply", "--tenant", "hotel-beispiel"])
    assert applied.exit_code == 0 and "4 draft entries written" in applied.output

    page = env / "hausordnung.md"
    page.write_text(
        "# Hausordnung\n\nRauchen ist im ganzen Haus untersagt. Ruhezeit ab 22 Uhr.\n",
        encoding="utf-8",
    )
    imported = runner.invoke(app, ["kb", "import-file", str(page), "--tenant", "hotel-beispiel"])
    assert imported.exit_code == 0, imported.output
    assert "house_rules/de" in imported.output and "1 drafts written" in imported.output
    keyed = runner.invoke(
        app,
        [
            "kb",
            "import-file",
            str(page),
            "--tenant",
            "hotel-beispiel",
            "--key",
            "extras",
            "--dry-run",
        ],
    )
    assert (
        keyed.exit_code == 0 and "extras/de" in keyed.output and "nothing written" in keyed.output
    )
    missing = runner.invoke(
        app, ["kb", "import-file", str(env / "none.md"), "--tenant", "hotel-beispiel"]
    )
    assert missing.exit_code == 1 and "is not a file" in missing.output

    refused = runner.invoke(
        app, ["kb", "import-url", "http://127.0.0.1/", "--tenant", "hotel-beispiel"]
    )
    assert refused.exit_code == 1, refused.output
    assert "refused" in refused.output and "non-public" in refused.output
    scheme = runner.invoke(
        app, ["kb", "import-url", "ftp://hotel.invalid/", "--tenant", "hotel-beispiel"]
    )
    assert scheme.exit_code == 1 and "only http and https" in scheme.output

    coverage = runner.invoke(app, ["kb", "coverage", "--tenant", "hotel-beispiel"])
    assert coverage.exit_code == 1 and "required keys approved in every language" in coverage.output
    assert "checkin" in coverage.output and "draft" in coverage.output

    no_model = runner.invoke(app, ["kb", "translate", "--tenant", "hotel-beispiel", "--to", "ru"])
    assert no_model.exit_code == 1 and "no language model configured" in no_model.output
    bad_lang = runner.invoke(
        app, ["kb", "translate", "--tenant", "hotel-beispiel", "--to", "xx", "--dry-run"]
    )
    assert bad_lang.exit_code == 1 and "--to expects languages" in bad_lang.output
    dry = runner.invoke(
        app, ["kb", "translate", "--tenant", "hotel-beispiel", "--to", "ru,uk", "--dry-run"]
    )
    assert dry.exit_code == 0 and "nothing sent" in dry.output


@pytest.mark.feature("F24")
def test_prices_model_set_and_list(env: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    listed = runner.invoke(app, ["prices", "model", "list"])
    assert (
        listed.exit_code == 0
        and "claude-sonnet-5-5" in listed.output
        and "default" in listed.output
    )
    stored = runner.invoke(
        app,
        [
            "prices",
            "model",
            "set",
            "--model",
            "claude-sonnet-5-5",
            "--from",
            "2026-10-01",
            "--input",
            "3",
            "--output",
            "15",
        ],
    )
    assert stored.exit_code == 0 and "model price stored" in stored.output
    listed = runner.invoke(app, ["prices", "model", "list"])
    assert "configured" in listed.output and "3.00" in listed.output and "15.00" in listed.output
    bad = runner.invoke(
        app,
        [
            "prices",
            "model",
            "set",
            "--model",
            "x",
            "--from",
            "next week",
            "--input",
            "1",
            "--output",
            "1",
        ],
    )
    assert bad.exit_code == 1 and "YYYY-MM-DD" in bad.output


@pytest.mark.feature("F28")
def test_more_commands_reach_their_guards_and_outputs(env: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    profile = env / "tenant.yaml"
    data = dict(SAMPLE)
    data["whatsapp"] = {"waba_id": "", "phone_number_id": "", "display_phone_number": ""}
    profile.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert runner.invoke(app, ["tenant", "add", "--profile", str(profile)]).exit_code == 0

    built = runner.invoke(app, ["handout", "build", "--out", str(tmp_path / "handout.pdf")])
    assert built.exit_code == 0 and "handout written" in built.output
    pack = runner.invoke(
        app, ["compliance", "pack", "--tenant", "hotel-beispiel", "--out", str(tmp_path / "pack")]
    )
    assert pack.exit_code == 0 and (tmp_path / "pack" / "README.md").is_file()
    assert "Draft for lawyer review" in pack.output
    assert runner.invoke(app, ["compliance", "pack", "--tenant", "nope"]).exit_code == 1

    guard = runner.invoke(app, ["wa-sim", "send", "--tenant", "hotel-beispiel", "--text", "Hallo"])
    assert guard.exit_code == 1 and "wa_app_secret" in guard.output
    unsynced = runner.invoke(app, ["whatsapp", "templates", "sync", "hotel-beispiel"])
    assert unsynced.exit_code == 1 and "waba_id" in unsynced.output
    no_key = runner.invoke(app, ["eval", "live"])
    assert no_key.exit_code == 2 and "ANTHROPIC_API_KEY" in no_key.output
    assert runner.invoke(app, ["tenant", "kill-switch", "hotel-beispiel", "--on"]).exit_code == 0
    assert "demo KILL" in runner.invoke(app, ["tenant", "list"]).output
    assert runner.invoke(app, ["tenant", "kill-switch", "hotel-beispiel", "--off"]).exit_code == 0
    assert runner.invoke(app, ["tenant", "kill-switch", "nope", "--on"]).exit_code == 1
    nobody = runner.invoke(
        app, ["gdpr", "erase", "--tenant", "hotel-beispiel", "--phone", "+4915199999999", "--yes"]
    )
    assert nobody.exit_code == 1 and "no data" in nobody.output
    assert (
        runner.invoke(app, ["gdpr", "erase", "--tenant", "hotel-beispiel", "--yes"]).exit_code == 1
    )
    preview = runner.invoke(app, ["journeys", "preview", "hotel-beispiel", "RES-NOPE"])
    assert preview.exit_code == 1
    bad_time = runner.invoke(app, ["demo", "advance", "--days", "1", "--at", "25:99"])
    assert bad_time.exit_code == 1
    assert runner.invoke(app, ["demo", "advance", "--days", "0"]).exit_code == 0  # nothing due
    data["voice"] = {**dict(SAMPLE["voice"]), "provider": "twilio_cr", "number": "+4969000009"}
    profile.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert runner.invoke(app, ["pack", "import", str(env), "--apply"]).exit_code in {0, 1}
    twilio = runner.invoke(app, ["voice", "provision", "hotel-beispiel", "--dry-run"])
    assert twilio.exit_code in {0, 1}
