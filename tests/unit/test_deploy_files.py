"""Deployment files (F33): parsed and linted statically; Docker builds run in the
cold-start test when a daemon is available."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.feature("F33")


def test_compose_file_has_app_postgres_and_caddy() -> None:
    data = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = data["services"]
    assert set(services) == {"app", "db", "caddy"}
    app = services["app"]
    assert app["read_only"] is True and app["env_file"] == ".env"
    assert app["environment"]["GC_ENV"] == "production"
    assert app["environment"]["GC_DATABASE_URL"].startswith("postgresql+asyncpg://gc:")
    assert app["depends_on"]["db"]["condition"] == "service_healthy"
    assert services["db"]["image"] == "postgres:18"
    assert services["caddy"]["image"].startswith("caddy:")
    assert "80:80" in services["caddy"]["ports"] and "443:443" in services["caddy"]["ports"]
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in services["caddy"]["volumes"]
    assert set(data["volumes"]) == {"app-data", "db-data", "caddy-data", "caddy-config"}
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "${POSTGRES_PASSWORD:?" in text and "${CONCIERGE_DOMAIN:?" in text  # no default secrets


def test_caddyfile_proxies_to_the_app_with_hsts() -> None:
    text = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert "{$CONCIERGE_DOMAIN}" in text
    assert "reverse_proxy app:8000" in text
    assert "Strict-Transport-Security" in text and "max-age=31536000" in text
    assert text.count("{") == text.count("}")


def test_dockerfile_is_non_root_slim_and_hash_locked() -> None:
    lines = [
        line.strip()
        for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    froms = [line for line in lines if line.startswith("FROM ")]
    assert froms and all("python:3.13-slim" in line for line in froms)
    assert any(line.startswith("USER gc") for line in lines)
    assert any("--require-hashes -r requirements.lock" in line for line in lines)
    assert lines[-1].startswith('CMD ["concierge", "serve"') and '"0.0.0.0"' in lines[-1]
    assert not any("ADD " in line for line in lines)
    assert not any("ANTHROPIC_API_KEY" in line or "GC_MASTER_KEYS" in line for line in lines)


def test_systemd_unit_hardening() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "deploy" / "systemd" / "concierge.service", encoding="utf-8")
    service = parser["Service"]
    assert service["User"] == "gc" and service["Group"] == "gc"
    assert service["EnvironmentFile"] == "/etc/gc/env"
    assert service["Restart"] == "on-failure"
    assert service["ProtectSystem"] == "strict"
    assert service["ReadWritePaths"] == "/var/lib/gc"
    assert service["NoNewPrivileges"] == "true" and service["PrivateTmp"] == "true"
    assert service["ExecStart"].endswith("concierge serve --host 127.0.0.1 --port 8000")
    assert parser["Install"]["WantedBy"] == "multi-user.target"
    assert parser["Unit"]["After"] == "network-online.target"
    units = list((ROOT / "deploy" / "systemd").glob("*.service"))
    assert units == [ROOT / "deploy" / "systemd" / "concierge.service"]  # exactly one unit (C1)
