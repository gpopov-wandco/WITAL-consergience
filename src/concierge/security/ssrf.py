"""One SSRF guard for every outbound fetch of an operator- or hotel-supplied URL.

Implements: F31, F06, F09

Rules: ``http`` and ``https`` only; no userinfo; the host is resolved once and the
connection is pinned to that address (DNS rebinding cannot swap it); private,
loopback, link-local, multicast and reserved ranges are refused, also after
redirects; redirects are capped; the body size and total time are capped.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx2

Resolver = Callable[[str], Awaitable[list[str]]]


class SSRFError(Exception):
    """The URL or its resolution is not allowed."""


class FetchError(OSError):
    """The transport failed (connection, protocol, timeout); the message is the cause's class name."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes
    etag: str | None = None
    last_modified: str | None = None


class Fetcher(Protocol):
    """Anything that fetches a URL the way ``SafeFetcher`` does (fakes serve from memory)."""

    async def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResult: ...


async def default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url(url: str) -> tuple[str, str, int, str]:
    """Return (scheme, host, port, path_with_query) or raise :class:`SSRFError`."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise SSRFError("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise SSRFError("URLs with credentials are not allowed")
    host = parts.hostname
    if not host:
        raise SSRFError("URL has no host")
    if host.endswith(".") or host in {"localhost"} or host.endswith(".localhost"):
        raise SSRFError("localhost is not allowed")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise SSRFError("invalid port") from exc
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, host, port, path


class SafeFetcher:
    """Fetch public URLs with pinning, caps and no credentials."""

    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        max_redirects: int = 3,
        max_bytes: int = 1_000_000,
        timeout_s: float = 10.0,
        user_agent: str = "WITALCO-Guest-Concierge/0.1 (+kb-import)",
    ) -> None:
        self._transport = transport
        self._resolver = resolver or default_resolver
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        self.user_agent = user_agent

    async def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResult:
        """Fetch with pinning and caps; transport failures surface as :class:`FetchError`."""
        try:
            async with asyncio.timeout(self.timeout_s):
                return await self._fetch(url, headers or {})
        except (httpx2.HTTPError, TimeoutError) as exc:
            raise FetchError(type(exc).__name__) from exc

    async def _fetch(self, url: str, extra_headers: dict[str, str]) -> FetchResult:
        current = url
        client_kwargs: dict[str, object] = {"follow_redirects": False, "timeout": self.timeout_s}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        async with httpx2.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            for _hop in range(self.max_redirects + 1):
                scheme, host, port, path = validate_url(current)
                address = await self._pin(host)
                target_host = f"[{address}]" if ":" in address else address
                request = client.build_request(
                    "GET",
                    f"{scheme}://{target_host}:{port}{path}",
                    headers={"Host": host, "User-Agent": self.user_agent, **extra_headers},
                    extensions={"sni_hostname": host},
                )
                response = await client.send(request, stream=True)
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise SSRFError("redirect without location")
                        current = urljoin(current, location)
                        continue
                    body = await self._read_capped(response)
                finally:
                    await response.aclose()
                return FetchResult(
                    url=current,
                    status=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    body=body,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
        raise SSRFError("too many redirects")

    async def _pin(self, host: str) -> str:
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            try:
                addresses = await self._resolver(host)
            except (OSError, socket.gaierror) as exc:
                raise SSRFError(f"cannot resolve {host}") from exc
        if not addresses:
            raise SSRFError(f"cannot resolve {host}")
        for address in addresses:
            if not is_public_address(address):
                raise SSRFError(f"{host} resolves to a non-public address")
        return addresses[0]

    async def _read_capped(self, response: httpx2.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            raise SSRFError("response larger than the allowed size")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self.max_bytes:
                raise SSRFError("response larger than the allowed size")
            chunks.append(chunk)
        return b"".join(chunks)
