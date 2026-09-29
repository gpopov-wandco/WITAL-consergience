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
