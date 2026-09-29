"""Backups (F30): create, prune, restore-test, tamper detection."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_profile import SAMPLE

from concierge.clock import FakeClock
from concierge.db.session import Database
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService
from concierge.tools import backup

pytestmark = pytest.mark.feature("F30")


async def test_backup_restore_prune_and_tamper(
    db: Database, clock: FakeClock, settings: Settings, tmp_path: Path
) -> None:
    await TenantService(db, clock).create(TenantProfile.model_validate(SAMPLE), actor="test")
    key = settings.backup_key()
    now = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    out = tmp_path / "backups"
    path = backup.create_backup(settings.database_url(), out, key, now=now)
    assert path.name == "concierge-20260929T100000Z.sqlite.enc" and path.read_bytes().startswith(
        backup.MAGIC
    )
    assert b"alembic_version" not in path.read_bytes()  # encrypted, not a plain SQLite file

    report = await backup.restore_test(path, key, scratch_dir=tmp_path)
    assert report.ok and report.head == report.expected_head
    assert report.tables["tenant"] == 1 and report.tables["message"] == 0
    assert report.expected_tables is not None and report.expected_tables["tenant"] == 1
    assert "alembic_version" in report.tables and report.mismatches == {}
    plain, kind, manifest = backup.decrypt(path.read_bytes(), key)
    assert kind == "sqlite" and manifest is not None
    assert manifest["tables"] == report.tables and manifest["created_at"] == now.isoformat()

    # a restore that lost rows fails on the manifest, not only on the head
    forged = out / "concierge-20260901T103000Z.sqlite.enc"  # older than the newest
    forged.write_bytes(
        backup.encrypt(plain, key, kind=b"sqlite", manifest={"tables": {"tenant": 2, "guest": 5}})
    )
    lost = await backup.restore_test(forged, key, scratch_dir=tmp_path)
    assert lost.head_ok and not lost.ok
    assert lost.mismatches == {"guest": (5, 0), "tenant": (2, 1)}

    # files from before the manifest existed restore with the head check only
    legacy = out / "concierge-20260901T104000Z.sqlite.enc"
    nonce_header = backup.LEGACY_MAGIC + b"sqlite".ljust(8, b" ") + b"\x07" * backup.NONCE_BYTES
    legacy.write_bytes(
        nonce_header + backup.AESGCM(key).encrypt(b"\x07" * backup.NONCE_BYTES, plain, nonce_header)
    )
    old = await backup.restore_test(legacy, key, scratch_dir=tmp_path)
    assert old.ok and old.expected_tables is None and old.tables["tenant"] == 1
    with pytest.raises(backup.BackupError, match="manifest is malformed"):
        backup.decrypt(
            backup.MAGIC
            + b"sqlite".ljust(8, b" ")
            + b"\x07" * backup.NONCE_BYTES
            + backup.AESGCM(key).encrypt(
                b"\x07" * backup.NONCE_BYTES,
                b"\x00\x00\x00\x03[]x",
                backup.MAGIC + b"sqlite".ljust(8, b" ") + b"\x07" * backup.NONCE_BYTES,
            ),
            key,
        )

    older = out / "concierge-20260801T100000Z.sqlite.enc"
    older.write_bytes(path.read_bytes())
    removed = backup.prune(out, keep_days=30, now=now)
    assert removed == [older] and not older.exists() and path.exists()
    assert backup.latest_backup(out) == path

    blob = bytearray(path.read_bytes())
    blob[-5] ^= 0x01
    tampered = out / "concierge-20260929T110000Z.sqlite.enc"
    tampered.write_bytes(bytes(blob))
    with pytest.raises(backup.BackupError, match="authentication failed"):
        await backup.restore_test(tampered, key, scratch_dir=tmp_path)
    with pytest.raises(backup.BackupError, match="authentication failed"):
        backup.decrypt(path.read_bytes(), b"\x02" * 32)
    with pytest.raises(backup.BackupError, match="not a Guest Concierge backup"):
        backup.decrypt(b"nope", key)
    with pytest.raises(backup.BackupError, match="does not exist"):
        backup.create_backup(
            "sqlite+aiosqlite:///" + (tmp_path / "missing.db").as_posix(), out, key, now=now
        )
    with pytest.raises(backup.BackupError, match="unsupported"):
        backup.create_backup("mysql://x", out, key, now=now)
    assert backup.sqlite_path("sqlite+aiosqlite:///:memory:") is None
    assert backup.sqlite_path("postgresql+asyncpg://x/y") is None
