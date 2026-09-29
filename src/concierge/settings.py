"""Application settings read from environment variables (Appendix A).

Implements: C2, C3, F28

Every environment variable the service reads is a field of :class:`Settings`.
A test compares the field list with ``.env.example`` in both directions. Secrets
are :class:`pydantic.SecretStr` so that they never appear in representations or
logs. Nothing here is a hotel fact, a price or a threshold: those live in the
per-tenant profile (:mod:`concierge.tenants.profile`).
"""

from __future__ import annotations

import base64
import binascii
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MASTER_KEY_BYTES = 32


class SettingsError(ValueError):
    """Raised when configuration is missing or malformed; the message names the variable."""


class Settings(BaseSettings):
    """Environment configuration. Field names map to the upper-cased variable names."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Platform
    gc_env: Literal["development", "production"] = "development"
    gc_database_url: str = ""
    gc_public_base_url: str = ""
    gc_master_keys: SecretStr = SecretStr("")
    gc_active_key_id: str = ""
    gc_signing_key: SecretStr = SecretStr("")
    gc_backup_key: SecretStr = SecretStr("")
    gc_metrics_token: SecretStr = SecretStr("")
    gc_operator_email: str = ""

    # Language model
    gc_llm_provider: Literal["anthropic", "none"] = "none"
    anthropic_api_key: SecretStr = SecretStr("")
    gc_llm_model_primary: str = "claude-sonnet-5-5"
    gc_llm_model_failover: str = "claude-sonnet-5"
    gc_llm_model_cheap: str = ""
    gc_llm_structured_outputs: bool = True
    gc_llm_turn_timeout_s: float = 25.0
    gc_eval_budget_usd: float = 5.0

    # WhatsApp Cloud API
    gc_wa_graph_version: str = "v26.0"

    # E-mail
    gc_email_provider: Literal["resend", "smtp", "none"] = "resend"
    resend_api_key: SecretStr = SecretStr("")
    gc_email_from: str = ""
    gc_smtp_host: str = ""
    gc_smtp_port: int = 587
    gc_smtp_user: str = ""
    gc_smtp_password: SecretStr = SecretStr("")
    gc_smtp_starttls: bool = True

    # Optional integrations
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")
    retell_api_key: SecretStr = SecretStr("")
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    hubspot_token: SecretStr = SecretStr("")

    # Logging, time zone, UTF-8
    gc_log_level: str = "INFO"
    gc_tz_default: str = "Europe/Berlin"
    pythonutf8: str = "1"

    @field_validator("gc_wa_graph_version")
    @classmethod
    def _graph_version(cls, value: str) -> str:
        if not value.startswith("v") or not value[1:].replace(".", "").isdigit():
            raise ValueError("GC_WA_GRAPH_VERSION must look like v26.0")
        return value

    @field_validator("gc_log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("GC_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return level

    # ------------------------------------------------------------------ helpers
    @property
    def is_production(self) -> bool:
        return self.gc_env == "production"

    def database_url(self) -> str:
        """The SQLAlchemy URL, defaulting to a SQLite file in the user's home directory.

        The default lives outside the project so that a project folder synchronised by
        OneDrive or similar never contains a live SQLite database (6.8).
        """
        if self.gc_database_url:
            return self.gc_database_url
        path = default_data_dir() / "concierge.db"
        return f"sqlite+aiosqlite:///{path.as_posix()}"

    def master_keys(self) -> dict[str, bytes]:
        """Parse ``GC_MASTER_KEYS`` (JSON map of key id to base64 32-byte key)."""
        raw = self.gc_master_keys.get_secret_value()
        if not raw:
            raise SettingsError("GC_MASTER_KEYS is not set; run `concierge setup`")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SettingsError("GC_MASTER_KEYS is not valid JSON") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise SettingsError("GC_MASTER_KEYS must be a non-empty JSON object")
        keys: dict[str, bytes] = {}
        for key_id, encoded in parsed.items():
            if not isinstance(key_id, str) or not isinstance(encoded, str):
                raise SettingsError("GC_MASTER_KEYS entries must map strings to strings")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SettingsError(f"GC_MASTER_KEYS[{key_id}] is not valid base64") from exc
            if len(decoded) != MASTER_KEY_BYTES:
                raise SettingsError(f"GC_MASTER_KEYS[{key_id}] must decode to 32 bytes")
            keys[key_id] = decoded
        return keys

    def active_key_id(self) -> str:
        keys = self.master_keys()
        if self.gc_active_key_id:
            if self.gc_active_key_id not in keys:
                raise SettingsError("GC_ACTIVE_KEY_ID names a key missing from GC_MASTER_KEYS")
            return self.gc_active_key_id
        if len(keys) == 1:
            return next(iter(keys))
        raise SettingsError("GC_ACTIVE_KEY_ID is required when GC_MASTER_KEYS has several keys")

    def signing_key(self) -> bytes:
        value = self.gc_signing_key.get_secret_value()
        if len(value) < 32:
            raise SettingsError(
                "GC_SIGNING_KEY must be at least 32 characters; run `concierge setup`"
            )
        return value.encode("utf-8")

    def backup_key(self) -> bytes:
        value = self.gc_backup_key.get_secret_value()
        if not value:
            raise SettingsError("GC_BACKUP_KEY is not set; run `concierge setup`")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SettingsError("GC_BACKUP_KEY is not valid base64") from exc
        if len(decoded) != MASTER_KEY_BYTES:
            raise SettingsError("GC_BACKUP_KEY must decode to 32 bytes")
        return decoded

    def public_base_url(self) -> str:
        """The public origin, required in production, defaulting to localhost otherwise."""
        if self.gc_public_base_url:
            return self.gc_public_base_url.rstrip("/")
        if self.is_production:
            raise SettingsError("GC_PUBLIC_BASE_URL is required when GC_ENV=production")
        return "http://127.0.0.1:8000"

    def env_var_names(self) -> list[str]:
        """Upper-cased names of every variable this service reads."""
        return [name.upper() for name in type(self).model_fields]


def default_data_dir() -> Path:
    """Directory for the default SQLite file and backups, outside the project."""
    return Path.home() / ".witalco-concierge"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, loaded once from the environment and ``.env``."""
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
