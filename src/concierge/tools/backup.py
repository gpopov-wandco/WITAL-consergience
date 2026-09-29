"""Backups (F30): encrypted database snapshots, pruning, restore test.

Implements: F30, F31, C3, C16

SQLite is copied with the online backup API, Postgres with ``pg_dump`` in custom
format. The copy is encrypted with AES-256-GCM under ``GC_BACKUP_KEY`` (random 96-bit
nonce, the header as associated data), so a flipped byte fails authentication instead
of restoring silently. A manifest with the row count of every table at backup time
travels inside the encrypted payload; ``restore_test`` decrypts into a scratch file,
checks the migration head and compares the restored row counts with the manifest, so
a restore that lost rows fails instead of passing on the head alone. Backups hold
guest data: the retention rule of the operator (``--keep-days``) prunes them by age.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import create_engine, func, inspect, select, table
from sqlalchemy.ext.asyncio import AsyncConnection

from concierge.db.migrate import head_state
from concierge.db.session import Database

MAGIC = b"GCBK2"
LEGACY_MAGIC = b"GCBK1"
"""Files without a manifest (first builds); restored with a head check only."""
NONCE_BYTES = 12
SUFFIX = ".enc"
MANIFEST_LIMIT = 1_000_000


class BackupError(RuntimeError):
    pass


@dataclass
class RestoreReport:
    path: Path
    head: str | None
    expected_head: str | None
    tables: dict[str, int] = field(default_factory=dict)
    expected_tables: dict[str, int] | None = None
    """Row counts recorded at backup time; ``None`` for a file without a manifest."""

    @property
    def mismatches(self) -> dict[str, tuple[int, int]]:
        """Tables whose restored count differs from the manifest: name -> (expected, restored)."""
        if self.expected_tables is None:
            return {}
        return {
            name: (expected, self.tables.get(name, 0))
            for name, expected in sorted(self.expected_tables.items())
            if self.tables.get(name, 0) != expected
        }

    @property
    def head_ok(self) -> bool:
        return self.head is not None and self.head == self.expected_head

    @property
    def ok(self) -> bool:
        return self.head_ok and not self.mismatches


def sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite"):
        return None
    tail = url.split("///", 1)[1] if "///" in url else ""
    return Path(tail) if tail and tail != ":memory:" else None


def _dialect(url: str) -> str:
    return urlsplit(url).scheme.split("+", 1)[0]


def encrypt(
    plain: bytes, key: bytes, *, kind: bytes, manifest: dict[str, Any] | None = None
) -> bytes:
    """Header, then AES-GCM over a length-prefixed JSON manifest and the snapshot."""
    nonce = os.urandom(NONCE_BYTES)
    header = MAGIC + kind[:8].ljust(8, b" ") + nonce
    manifest_bytes = json.dumps(manifest or {}, sort_keys=True).encode("utf-8")
    body = len(manifest_bytes).to_bytes(4, "big") + manifest_bytes + plain
    return header + AESGCM(key).encrypt(nonce, body, header)


def decrypt(blob: bytes, key: bytes) -> tuple[bytes, str, dict[str, Any] | None]:
    """(snapshot, kind, manifest); raises BackupError on tampering or a wrong key.

    The manifest is ``None`` for files written before the manifest existed.
    """
    header_len = len(MAGIC) + 8 + NONCE_BYTES
    if len(blob) < header_len or not (blob.startswith(MAGIC) or blob.startswith(LEGACY_MAGIC)):
        raise BackupError("not a Guest Concierge backup file")
    header, body = blob[:header_len], blob[header_len:]
    nonce = header[len(MAGIC) + 8 :]
    kind = header[len(MAGIC) : len(MAGIC) + 8].strip().decode("ascii")
    try:
        plain = AESGCM(key).decrypt(nonce, body, header)
    except InvalidTag as exc:
        raise BackupError("backup authentication failed: wrong key or tampered file") from exc
    if blob.startswith(LEGACY_MAGIC):
        return plain, kind, None
    length = int.from_bytes(plain[:4], "big")
    if length > MANIFEST_LIMIT or len(plain) < 4 + length:
        raise BackupError("backup manifest is malformed")
    try:
        manifest = json.loads(plain[4 : 4 + length].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupError("backup manifest is malformed") from exc
    if not isinstance(manifest, dict):
        raise BackupError("backup manifest is malformed")
    return plain[4 + length :], kind, manifest


def _sync_counts(url: str) -> dict[str, int]:
    """Row count per table of a SQLite file through SQLAlchemy inspection (no ORM registry)."""
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            names = inspect(connection).get_table_names()
            return {
                name: int(connection.scalar(select(func.count()).select_from(table(name))) or 0)
                for name in names
            }
    finally:
        engine.dispose()


async def _async_counts(connection: AsyncConnection) -> dict[str, int]:
    names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = int(
            await connection.scalar(select(func.count()).select_from(table(name))) or 0
        )
    return counts


def _dump_sqlite(path: Path) -> tuple[bytes, dict[str, int]]:
    """The snapshot bytes (online backup API, WAL content included) and its row counts."""
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
        counts = _sync_counts(f"sqlite:///{target.as_posix()}")
        return target.read_bytes(), counts


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
    manifest: dict[str, Any] = {"created_at": now.isoformat(), "tables": {}}
    if dialect == "sqlite":
        path = sqlite_path(database_url)
        if path is None or not path.exists():
            raise BackupError("the SQLite database file does not exist yet")
        plain, counts = _dump_sqlite(path)
        kind = b"sqlite"
        manifest["tables"] = counts
    elif dialect == "postgresql":
        plain, kind = _dump_postgres(database_url), b"pgdump"
    else:
        raise BackupError(f"unsupported database dialect {dialect!r}")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"concierge-{stamp}.{kind.decode()}{SUFFIX}"
    target.write_bytes(encrypt(plain, key, kind=kind, manifest=manifest))
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
    """Restore a SQLite backup into a scratch file; check the head and the row counts."""
    plain, kind, manifest = decrypt(await asyncio.to_thread(path.read_bytes), key)
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
        async with database.engine.connect() as connection:
            tables = await _async_counts(connection)
    finally:
        await database.dispose()
    expected: dict[str, int] | None = None
    if manifest is not None:
        raw = manifest.get("tables")
        expected = {str(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    return RestoreReport(
        path=target, head=current, expected_head=head, tables=tables, expected_tables=expected
    )
