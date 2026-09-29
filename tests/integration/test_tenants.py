"""Tenant service, secret store, activation gates and setup wizard."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from tests.unit.test_profile import SAMPLE

from concierge.clock import FakeClock
from concierge.db.models import AuditLog, Guest, SystemSetting, TenantSecret, User
from concierge.db.session import Database
from concierge.security.crypto import CryptoError, Keyring
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile
from concierge.tenants.secrets import SecretStore
from concierge.tenants.service import TenantError, TenantService
from concierge.tools.setup import read_env, run_setup

pytestmark = pytest.mark.feature("F21")


@pytest.fixture
def service(db: Database, clock: FakeClock) -> TenantService:
    return TenantService(db, clock)


async def test_create_mirror_and_resolve(service: TenantService) -> None:
    profile = TenantProfile.model_validate(
        dict(
            SAMPLE,
            whatsapp={"waba_id": "w1", "phone_number_id": "pn1", "display_phone_number": "+49690"},
        )
    )
    tenant = await service.create(profile, actor="test")
    assert tenant.slug == "hotel-beispiel"
    assert tenant.timezone == "Europe/Berlin"
    assert tenant.public_key == "pk_demo"
    assert (await service.by_phone_number_id("pn1")).id == tenant.id
    assert (await service.by_public_key("pk_demo")).id == tenant.id
    assert await service.by_public_key("pk_other") is None
    assert await service.by_phone_number_id("") is None
    assert service.profile_of(tenant).contacts.email == "reception@example.invalid"
    with pytest.raises(TenantError, match="already exists"):
        await service.create(profile, actor="test")
    with pytest.raises(TenantError, match="unknown tenant"):
        await service.require("nope")


async def test_pause_and_kill_switch_are_audited(service: TenantService, db: Database) -> None:
    await service.create(TenantProfile.model_validate(SAMPLE), actor="test")
    paused = await service.set_paused("hotel-beispiel", True, actor="op")
    assert paused.paused and paused.status == "paused" and paused.profile["status"] == "paused"
    resumed = await service.set_paused("hotel-beispiel", False, actor="op")
    assert not resumed.paused and resumed.status == "pilot"
    killed = await service.set_kill_switch("hotel-beispiel", True, actor="op")
    assert killed.kill_switch
    async with db.scope() as session:
        actions = [a.action for a in (await session.execute(select(AuditLog))).scalars()]
    assert actions == ["tenant.create", "tenant.pause", "tenant.unpause", "tenant.kill_switch"]


async def test_activation_gates_name_every_missing_item(service: TenantService) -> None:
    await service.create(TenantProfile.model_validate(SAMPLE), actor="test")
    report = await service.activation_report("hotel-beispiel", doctor_live_ok=None)
    assert not report.ok
    joined = "\n".join(report.failures)
    assert "legal text 'privacy_paragraph' (de)" in joined
    assert "processor agreement" in joined
    assert "sub-processor" in joined
    assert "core KB key 'identity' (de)" in joined
    assert "phone_number_id" in joined
    assert "doctor --live has not been run" in joined
    assert "template 'gc_booking_followup' (uk)" in joined
    result = await service.activate("hotel-beispiel", actor="op", doctor_live_ok=True)
    assert not result.ok
    assert (await service.require("hotel-beispiel")).status == "demo"


async def test_secret_store_round_trip_and_isolation(
    db: Database, keyring: Keyring, service: TenantService
) -> None:
    a = await service.create(TenantProfile.model_validate(SAMPLE), actor="t")
    b = await service.create(
        TenantProfile.model_validate(dict(SAMPLE, slug="other", widget={"public_key": "pk_other"})),
        actor="t",
    )
    store = SecretStore(db, keyring)
    await store.set(a.id, "wa_access_token", "EAA-token-a", actor="op")
    await store.set(b.id, "wa_access_token", "EAA-token-b", actor="op")
    await store.set(a.id, "wa_access_token", "EAA-token-a2", actor="op")
    assert await store.get(a.id, "wa_access_token") == "EAA-token-a2"
    assert await store.get(b.id, "wa_access_token") == "EAA-token-b"
    assert await store.get(a.id, "missing") is None
    assert await store.names(a.id) == ["wa_access_token"]
    assert await store.get_many(a.id, ["wa_access_token", "missing"]) == {
        "wa_access_token": "EAA-token-a2"
    }
    with pytest.raises(ValueError, match="empty"):
        await store.set(a.id, "x", "", actor="op")
    with pytest.raises(ValueError, match="letters"):
        await store.set(a.id, "bad name", "v", actor="op")
    async with db.scope() as session:
        rows = (await session.execute(select(TenantSecret))).scalars().all()
    assert all(b"EAA" not in r.ciphertext for r in rows)
    assert await store.delete(a.id, "wa_access_token", actor="op")
    assert not await store.delete(a.id, "wa_access_token", actor="op")


async def test_rotation_re_encrypts_secrets_and_pii(db: Database, service: TenantService) -> None:
    k1, k2 = b"\x01" * 32, b"\x02" * 32
    old = Keyring({"k1": k1}, "k1")
    tenant = await service.create(TenantProfile.model_validate(SAMPLE), actor="t")
    await SecretStore(db, old).set(tenant.id, "beds24_refresh_token", "rt-secret", actor="op")
    async with db.scope() as session:
        session.add(
            Guest(
                tenant_id=tenant.id,
                channel="whatsapp",
                channel_key="u1",
                phone_enc=old.encrypt_str("+4915112345678", tenant.id, "phone"),
                phone_bidx=old.blind_index("+4915112345678", tenant.id, "phone"),
            )
        )
        await session.commit()
    both = Keyring({"k1": k1, "k2": k2}, "k2")
    touched = await SecretStore(db, both).rotate_all(actor="op")
    assert touched == 2
    new_only = Keyring({"k2": k2}, "k2")
    assert await SecretStore(db, new_only).get(tenant.id, "beds24_refresh_token") == "rt-secret"
    async with db.scope() as session:
        guest = (await session.execute(select(Guest))).scalar_one()
    assert new_only.decrypt_str(guest.phone_enc, tenant.id, "phone") == "+4915112345678"
    assert guest.phone_bidx == new_only.blind_index("+4915112345678", tenant.id, "phone")
    with pytest.raises(CryptoError):
        old.decrypt_str(guest.phone_enc, tenant.id, "phone")
    assert await SecretStore(db, both).rotate_all(actor="op") == 0


async def test_setup_wizard_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    env_path = tmp_path / ".env"
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'setup.db').as_posix()}"
    profile_path = tmp_path / "tenant.yaml"
    profile_path.write_text(yaml.safe_dump(SAMPLE), encoding="utf-8")
    clock = FakeClock(datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    lines: list[str] = []
    result = await run_setup(
        env_path=env_path,
        non_interactive=True,
        database_url=db_url,
        public_base_url="https://concierge.example.invalid",
        operator_email="ops@example.invalid",
        tenant_profile=profile_path,
        clock=clock,
        echo=lines.append,
    )
    values = read_env(env_path)
    assert set(values) >= {
        "GC_MASTER_KEYS",
        "GC_ACTIVE_KEY_ID",
        "GC_SIGNING_KEY",
        "GC_BACKUP_KEY",
        "GC_METRICS_TOKEN",
        "GC_DATABASE_URL",
    }
    assert len(base64.b64decode(json.loads(values["GC_MASTER_KEYS"])["k1"])) == 32
    assert result.admin_link is not None and result.admin_link.startswith(
        "https://concierge.example.invalid/admin/bootstrap/"
    )
    assert result.tenant_slug == "hotel-beispiel"
    settings = Settings(_env_file=env_path)
    assert settings.master_keys()
    db = Database(db_url)
    try:
        async with db.scope() as session:
            operator = (
                await session.execute(select(User).where(User.role == "operator"))
            ).scalar_one()
            assert operator.email == "ops@example.invalid" and operator.password_hash is None
            setting = await session.get(SystemSetting, "bootstrap_admin")
            assert setting is not None and setting.value["user_id"] == operator.id
        # a second run keeps the keys and creates nothing twice
        again = await run_setup(
            env_path=env_path,
            non_interactive=True,
            tenant_profile=profile_path,
            clock=clock,
            echo=lines.append,
        )
        assert read_env(env_path)["GC_MASTER_KEYS"] == values["GC_MASTER_KEYS"]
        assert again.admin_link != result.admin_link
        async with db.scope() as session:
            assert len((await session.execute(select(User))).scalars().all()) == 1
    finally:
        await db.dispose()
    assert any("already exists" in line for line in lines)
