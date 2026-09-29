"""Structured JSON logging without personal data.

Implements: F31, F32

``structlog`` renders JSON lines to stderr with tenant, conversation and request ids
bound through context variables. A redaction processor replaces the values of keys
that could carry personal data or secrets (``text``, ``body``, ``message``, ``name``,
``email``, ``phone``, ``address``, ``token`` and any ``*_token``, ``*_secret``,
``*_password``, ``api_key``) with ``[redacted]``. Phone numbers are only ever logged
as truncated keyed hashes produced by :func:`concierge.security.crypto.phone_ref`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

REDACTED = "[redacted]"
REDACT_KEYS: frozenset[str] = frozenset(
    {
        "text",
        "body",
        "message",
        "name",
        "email",
        "phone",
        "address",
        "token",
        "password",
        "secret",
        "authorization",
        "cookie",
        "api_key",
        "access_token",
        "refresh_token",
        "guest_name",
        "first_name",
        "last_name",
        "family_name",
    }
)
REDACT_SUFFIXES: tuple[str, ...] = (
    "_token",
    "_secret",
    "_password",
    "_api_key",
    "_email",
    "_phone",
)


def redact(value: Any) -> Any:
    """Recursively redact known keys in mappings and sequences of mappings."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_sensitive(key):
                out[key] = REDACTED
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return lowered in REDACT_KEYS or lowered.endswith(REDACT_SUFFIXES)


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key == "event":
            continue
        if _is_sensitive(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", stream: Any = None) -> None:
    """Configure structlog for JSON output; safe to call more than once."""
    numeric = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    logging.basicConfig(level=numeric, handlers=[logging.NullHandler()], force=True)
    logging.getLogger("uvicorn.access").disabled = True
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.WriteLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_context(**values: Any) -> None:
    """Bind ids (tenant, conversation, request) to every log line of this context."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
