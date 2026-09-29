"""Backups (F30): encrypted database snapshots, pruning, restore test.

Implements: F30, F31, C3, C16

SQLite is copied with the online backup API, Postgres with ``pg_dump`` in custom
format. The copy is encrypted with AES-256-GCM under ``GC_BACKUP_KEY`` (random 96-bit
nonce, the header as associated data), so a flipped byte fails authentication instead
of restoring silently. ``restore_test`` decrypts into a scratch file, checks the
migration head and counts the rows of every table. Backups hold guest data: the
retention rule of the operator (``--keep-days``) prunes them by age.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select

from concierge.db.base import Base
from concierge.db.migrate import head_state
from concierge.db.session import Database

MAGIC = b"GCBK1"
NONCE_BYTES = 12
SUFFIX = ".enc"


class BackupError(RuntimeError):
    pass


@dataclass
class RestoreReport:
    path: Path
    head: str | None
    expected_head: str | None
    tables: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.head is not None and self.head == self.expected_head


def sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite"):
        return None
    tail = url.split("///", 1)[1] if "///" in url else ""
    return Path(tail) if tail and tail != ":memory:" else None


def _dialect(url: str) -> str:
    return urlsplit(url).scheme.split("+", 1)[0]


def encrypt(plain: bytes, key: bytes, *, kind: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    header = MAGIC + kind[:8].ljust(8, b" ") + nonce
    return header + AESGCM(key).encrypt(nonce, plain, header)


def decrypt(blob: bytes, key: bytes) -> tuple[bytes, str]:
    """(plaintext, kind); raises BackupError on tampering or a wrong key."""
    header_len = len(MAGIC) + 8 + NONCE_BYTES
    if len(blob) < header_len or not blob.startswith(MAGIC):
        raise BackupError("not a Guest Concierge backup file")
    header, body = blob[:header_len], blob[header_len:]
    nonce = header[len(MAGIC) + 8 :]
    kind = header[len(MAGIC) : len(MAGIC) + 8].strip().decode("ascii")
    try:
        return AESGCM(key).decrypt(nonce, body, header), kind
    except InvalidTag as exc:
        raise BackupError("backup authentication failed: wrong key or tampered file") from exc


def _dump_sqlite(path: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="gc-backup-") as tmp:
        target = Path(tmp) / "snapshot.sqlite"
        source = sqlite3.connect(str(path))
        try:
            destination = sqlite3.connect(str(target))
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        return target.read_bytes()


def _dump_postgres(url: str) -> bytes:
    executable = shutil.which("pg_dump")
    if executable is None:
        raise BackupError("pg_dump is not installed on this machine")
    plain_url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    # fixed argument list, no shell, the URL comes from settings
    result = subprocess.run(  # nosec B603
        [executable, "--format=custom", "--no-owner", plain_url],
        capture_output=True,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        raise BackupError(f"pg_dump failed with exit code {result.returncode}")
    return result.stdout


def create_backup(database_url: str, out_dir: Path, key: bytes, *, now: datetime) -> Path:
    """Write ``concierge-<UTC stamp>.<kind>.enc`` into ``out_dir``; returns its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dialect = _dialect(database_url)
    if dialect == "sqlite":
        path = sqlite_path(database_url)
        if path is None or not path.exists():
            raise BackupError("the SQLite database file does not exist yet")
        plain, kind = _dump_sqlite(path), b"sqlite"
    elif dialect == "postgresql":
        plain, kind = _dump_postgres(database_url), b"pgdump"
    else:
        raise BackupError(f"unsupported database dialect {dialect!r}")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"concierge-{stamp}.{kind.decode()}{SUFFIX}"
    target.write_bytes(encrypt(plain, key, kind=kind))
    return target


def prune(out_dir: Path, *, keep_days: int, now: datetime) -> list[Path]:
    """Delete backups older than ``keep_days`` by their file stamp; returns what went."""
    removed: list[Path] = []
    cutoff = now - timedelta(days=keep_days)
    for path in sorted(out_dir.glob(f"concierge-*{SUFFIX}")):
        stamp = path.name.split("-", 1)[1].split(".", 1)[0]
        try:
            made = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
        if made < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def latest_backup(out_dir: Path) -> Path | None:
    files = sorted(out_dir.glob(f"concierge-*{SUFFIX}"))
    return files[-1] if files else None


async def restore_test(path: Path, key: bytes, *, scratch_dir: Path | None = None) -> RestoreReport:
    """Restore a SQLite backup into a scratch file and check head and row counts."""
    plain, kind = decrypt(await asyncio.to_thread(path.read_bytes), key)
    if kind != "sqlite":
        raise BackupError(
            "restore-test restores SQLite snapshots; restore pg_dump files with pg_restore"
        )
    directory = Path(tempfile.mkdtemp(prefix="gc-restore-", dir=scratch_dir))
    target = directory / "restored.sqlite"
    await asyncio.to_thread(target.write_bytes, plain)
    database = Database(f"sqlite+aiosqlite:///{target.as_posix()}")
    try:
        current, head = await head_state(database.engine)
        report = RestoreReport(path=target, head=current, expected_head=head)
        async with database.scope() as session:
            for table in Base.metadata.sorted_tables:
                count = await session.scalar(select(func.count()).select_from(table))
                report.tables[table.name] = int(count or 0)
    finally:
        await database.dispose()
    return report
