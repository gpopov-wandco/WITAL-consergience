"""Shared HTTP plumbing for the PMS adapters (F08): timeouts, error mapping, JSON.

Implements: F08, 6.6, C3

Only adapters use this module (import boundary). Vendor errors become
``AvailabilityError`` subclasses so the chain can move on: 401/403 ``AuthError``, 429
``RateLimited`` with ``Retry-After``, 5xx and transport failures ``SourceUnavailable``,
unparseable bodies ``MalformedResponse``. Credentials are sent in headers only and
never appear in log lines or exception messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx2

from concierge.availability.models import (
    AuthError,
    MalformedResponse,
    RateLimited,
    SourceUnavailable,
)


@dataclass
class VendorResponse:
    status: int
    headers: dict[str, str]
    data: Any

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class VendorClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 8.0,
        transport: httpx2.AsyncBaseTransport | None = None,
        user_agent: str = "WITALCO-Guest-Concierge/0.1",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.transport = transport
        self.user_agent = user_agent

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        expect_json: bool = True,
        ok_statuses: tuple[int, ...] = (200, 201),
    ) -> VendorResponse:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs: dict[str, Any] = {"timeout": self.timeout_s}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            async with httpx2.AsyncClient(**kwargs) as client:
                response = await client.request(
                    method,
                    url,
                    headers={"User-Agent": self.user_agent, **(headers or {})},
                    params=params,
                    json=json_body,
                    data=data,
                    auth=auth,
                )
        except httpx2.TimeoutException as exc:
            raise SourceUnavailable("vendor timeout") from exc
        except httpx2.HTTPError as exc:
            raise SourceUnavailable(f"vendor transport error: {type(exc).__name__}") from exc
        lowered = {k.lower(): v for k, v in response.headers.items()}
        status = response.status_code
        if status in {401, 403}:
            raise AuthError(f"vendor rejected the credentials ({status})")
        if status == 429:
            raise RateLimited("vendor rate limit", retry_after_s=_retry_after(lowered))
        if status == 404:
            return VendorResponse(status, lowered, None)
        if status >= 500:
            raise SourceUnavailable(f"vendor error {status}", retry_after_s=_retry_after(lowered))
        if status not in ok_statuses:
            raise MalformedResponse(f"unexpected vendor status {status}")
        if not expect_json:
            return VendorResponse(status, lowered, response.content)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedResponse("vendor body is not JSON") from exc
        return VendorResponse(status, lowered, payload)


def _retry_after(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def need(value: Any, *keys: str) -> Any:
    """Walk a vendor payload; a missing or mistyped level is a ``MalformedResponse``."""
    current = value
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            raise MalformedResponse(f"vendor payload lacks {'.'.join(keys)}")
    return current


def as_list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise MalformedResponse(f"vendor payload: {what} is not a list")
    return value


def as_int(value: Any, what: str, default: int | None = None) -> int:
    if isinstance(value, bool):
        raise MalformedResponse(f"vendor payload: {what} is a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    if value is None and default is not None:
        return default
    raise MalformedResponse(f"vendor payload: {what} is not an integer")


def to_cents(value: Any, what: str) -> int:
    if isinstance(value, bool) or value is None:
        raise MalformedResponse(f"vendor payload: {what} is not an amount")
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError) as exc:
        raise MalformedResponse(f"vendor payload: {what} is not an amount") from exc
