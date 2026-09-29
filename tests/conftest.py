"""Shared fixtures: no network, temporary SQLite, fake clock, keyring, settings."""

from __future__ import annotations

import base64
import json
import os
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from concierge.clock import FakeClock
from concierge.db.migrate import upgrade
from concierge.db.session import Database
from concierge.security.crypto import Keyring
from concierge.settings import Settings, reset_settings_cache

_REAL_SOCKET_CONNECT = socket.socket.connect


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default tests are offline: any real socket connection fails at once."""
    if request.node.get_closest_marker("live") or request.node.get_closest_marker("browser"):
        return

    def _guard(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in {"127.0.0.1", "::1", "localhost"} or str(host).startswith("/"):
            return _REAL_SOCKET_CONNECT(self, address)  # type: ignore[arg-type]
        raise RuntimeError(f"network access blocked in tests: {host!r}")

    monkeypatch.setattr(socket.socket, "connect", _guard)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 9, 29, 10, 0, tzinfo=UTC))


@pytest.fixture
def master_key() -> str:
    return base64.b64encode(b"\x01" * 32).decode()


@pytest.fixture
def keyring(master_key: str) -> Keyring:
    return Keyring({"k1": base64.b64decode(master_key)}, "k1")


@pytest.fixture
def settings(tmp_path: Path, master_key: str) -> Settings:
    db_path = (tmp_path / "test.db").as_posix()
    return Settings(
        _env_file=None,
        gc_env="development",
        gc_database_url=f"sqlite+aiosqlite:///{db_path}",
        gc_master_keys=json.dumps({"k1": master_key}),
        gc_active_key_id="k1",
        gc_signing_key="s" * 48,
        gc_backup_key=master_key,
        gc_metrics_token="metrics-token-for-tests",
        gc_operator_email="operator@example.invalid",
        gc_public_base_url="http://127.0.0.1:8000",
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.database_url())
    await upgrade(database.engine)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("GC_") or name in {"ANTHROPIC_API_KEY", "RESEND_API_KEY"}:
            monkeypatch.delenv(name, raising=False)
    reset_settings_cache()
