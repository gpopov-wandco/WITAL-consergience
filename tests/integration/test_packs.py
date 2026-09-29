"""Client packs (F21): export, validate, diff, import, zip round trip."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tests.integration.conftest import DemoStack

from concierge.tenants.packs import PACK_FILES, open_pack, pack_to_zip, validate_pack

pytestmark = pytest.mark.feature("F21")


async def test_export_validate_and_idempotent_import(demo_stack: DemoStack, tmp_path: Path) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    target = tmp_path / "pack"
    written = await services.packs.export(tenant, target)
    assert sorted(written) == sorted(PACK_FILES)
    for rel in PACK_FILES:
        assert (target / rel).is_file()
    text = "\n".join((target / rel).read_text(encoding="utf-8") for rel in PACK_FILES)
    assert "demo-access-token" not in text and "demo-app-secret" not in text  # C3
    profile = yaml.safe_load((target / "tenant.yaml").read_text(encoding="utf-8"))
    assert profile["slug"] == "hotel-beispiel" and "journeys" not in profile
    assert validate_pack(target) == []

    report = await services.packs.import_pack(target, dry_run=True, actor="test")
    assert report.ok and report.changes == [] and not report.applied
    applied = await services.packs.import_pack(target, dry_run=False, actor="test")
    assert applied.ok and applied.applied and applied.changes == []


async def test_import_applies_kb_and_legal_changes_as_drafts(
    demo_stack: DemoStack, tmp_path: Path
) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    target = tmp_path / "pack"
    await services.packs.export(tenant, target)
    kb_file = target / "kb" / "entries.yaml"
    kb = yaml.safe_load(kb_file.read_text(encoding="utf-8"))
    entry = next(e for e in kb["entries"] if e["key"] == "parking")
    entry["text"]["de"] = "Parken kostet ab 2027 mehr."
    kb["entries"].append({"key": "extras.pool", "text": {"de": "Der Pool ist im Umbau."}})
    kb_file.write_text(yaml.safe_dump(kb, allow_unicode=True), encoding="utf-8")
    legal_file = target / "legal" / "texts.yaml"
    legal = yaml.safe_load(legal_file.read_text(encoding="utf-8"))
    legal["ai_disclosure"]["de"] = "Sie schreiben mit einem KI-Assistenten."
    legal_file.write_text(yaml.safe_dump(legal, allow_unicode=True), encoding="utf-8")
    tenant_file = target / "tenant.yaml"
    data = yaml.safe_load(tenant_file.read_text(encoding="utf-8"))
    data["name"] = "Hotel Beispiel am See"
    tenant_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    dry = await services.packs.import_pack(target, dry_run=True, actor="test")
    assert dry.ok and not dry.applied
    assert "profile.name: changed" in dry.changes
    assert "kb parking/de: new draft version" in dry.changes
    assert "kb extras.pool/de: new draft version" in dry.changes
    assert "legal ai_disclosure/de: new draft version" in dry.changes
    assert (await services.tenants.require("hotel-beispiel")).name == "Hotel Beispiel"

    applied = await services.packs.import_pack(target, dry_run=False, actor="test")
    assert applied.applied
    assert (await services.tenants.require("hotel-beispiel")).name == "Hotel Beispiel am See"
    drafts = [
        e
        for e in await services.kb.entries(tenant.id, lang="de")
        if e.key in {"parking", "extras.pool"}
    ]
    assert any(e.key == "parking" and e.status == "approved" for e in drafts)  # the live one stays
    assert any(
        e.key == "parking" and e.text.startswith("Parken kostet") and e.status != "approved"
        for e in drafts
    )
    assert any(e.key == "extras.pool" and e.status != "approved" for e in drafts)
    legal_row = next(
        r
        for r in await services.legal.rows(tenant.id)
        if r.key == "ai_disclosure" and r.lang == "de"
    )
    assert legal_row.text == "Sie schreiben mit einem KI-Assistenten." and legal_row.version == 2
    assert legal_row.status == "draft_unreviewed"
    again = await services.packs.import_pack(target, dry_run=True, actor="test")
    assert again.changes == []


async def test_validation_reports_field_paths_and_secrets(
    demo_stack: DemoStack, tmp_path: Path
) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    target = tmp_path / "pack"
    await services.packs.export(tenant, target)
    tenant_file = target / "tenant.yaml"
    data = yaml.safe_load(tenant_file.read_text(encoding="utf-8"))
    data["languages"] = ["de", "xx"]
    data["whatsapp"]["access_token"] = "should-not-be-here"
    tenant_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    (target / "kb" / "entries.yaml").write_text(
        "entries:\n  - text: no key\n  - key: faq.pool\n    text: {de: x}\n", encoding="utf-8"
    )
    errors = validate_pack(target)
    assert any(e.startswith("tenant.yaml: languages.1") for e in errors)
    assert any("whatsapp.access_token: secrets never travel in a pack" in e for e in errors)
    assert "kb/entries.yaml: entries[0].key: missing" in errors
    assert any(e.startswith("kb/entries.yaml: entries[1].key: unknown KB block") for e in errors)
    report = await services.packs.import_pack(target, dry_run=False, actor="test")
    assert not report.ok and not report.applied
    assert validate_pack(tmp_path / "nothing") == ["tenant.yaml: missing"]


async def test_zip_round_trip(demo_stack: DemoStack, tmp_path: Path) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    target = tmp_path / "pack"
    await services.packs.export(tenant, target)
    archive = tmp_path / "hotel-beispiel.zip"
    archive.write_bytes(pack_to_zip(target))
    extracted = open_pack(archive)
    assert extracted != target and validate_pack(extracted) == []
    assert open_pack(target) == target
    with pytest.raises(ValueError, match="neither a directory nor a zip"):
        open_pack(tmp_path / "missing.zip")


async def test_new_tenant_from_pack(demo_stack: DemoStack, tmp_path: Path) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    target = tmp_path / "pack"
    await services.packs.export(tenant, target)
    tenant_file = target / "tenant.yaml"
    data = yaml.safe_load(tenant_file.read_text(encoding="utf-8"))
    data["slug"] = "hotel-zwei"
    data["name"] = "Hotel Zwei"
    data["widget"]["public_key"] = "pk_zwei"
    data["whatsapp"] = {
        "waba_id": "zwei-waba",
        "phone_number_id": "zwei-phone-number-id",
        "display_phone_number": "+49 69 000010",
    }
    tenant_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    dry = await services.packs.import_pack(target, dry_run=True, actor="test")
    assert dry.ok, dry.errors
    assert dry.changes[0] == "create tenant hotel-zwei"
    assert any(c.startswith("kb: ") and c.endswith("entries as drafts") for c in dry.changes)
    applied = await services.packs.import_pack(target, dry_run=False, actor="test")
    assert applied.applied
    created = await services.tenants.require("hotel-zwei")
    assert created.name == "Hotel Zwei"
    entries = await services.kb.entries(created.id)
    assert entries and all(e.status != "approved" for e in entries)
    assert await services.legal.rows(created.id)

    # a second tenant that reuses a unique field is refused with a readable error
    data["slug"] = "hotel-drei"
    tenant_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    refused = await services.packs.import_pack(target, dry_run=False, actor="test")
    assert not refused.ok and not refused.applied
    assert refused.errors == [
        "tenant.yaml: a unique value (public key or phone number) is already used by another tenant"
    ]
    assert await services.tenants.get("hotel-drei") is None


def test_secret_scan_flags_credentials_not_quantities(tmp_path: Path) -> None:
    from concierge.tenants.packs import _scan_secrets

    (tmp_path / "tenant.yaml").write_text(
        yaml.safe_dump(
            {
                "limits": {"prompt_token_budget": 12000, "max_tokens": 1024},
                "whatsapp": {"wa_access_token": "x", "waba_id": "1"},
                "pms": {"api_key": "k", "client_secret_name": "beds24_secret"},
            }
        ),
        encoding="utf-8",
    )
    assert sorted(_scan_secrets(tmp_path)) == [
        "tenant.yaml: pms.api_key: secrets never travel in a pack",
        "tenant.yaml: whatsapp.wa_access_token: secrets never travel in a pack",
    ]


async def test_export_keeps_the_newest_version_per_language(
    demo_stack: DemoStack, tmp_path: Path
) -> None:
    services, tenant = demo_stack.services, demo_stack.tenant
    await services.kb.upsert(tenant.id, "parking", "de", text="Neuer Entwurf zum Parken.")
    target = tmp_path / "pack"
    await services.packs.export(tenant, target)
    entries = yaml.safe_load((target / "kb" / "entries.yaml").read_text(encoding="utf-8"))
    parking = next(e for e in entries["entries"] if e["key"] == "parking")
    assert parking["text"]["de"] == "Neuer Entwurf zum Parken."
    report = await services.packs.import_pack(target, dry_run=True, actor="test")
    assert report.changes == []
