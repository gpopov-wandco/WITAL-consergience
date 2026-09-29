"""Graph API adapter for the WhatsApp Cloud API (``httpx2`` only).

Implements: 6.6, F14, F18, 7

Errors are mapped to :class:`GraphError` with Meta's code and subcode; a timeout on a
send yields ``unknown`` (the status webhook reconciles it, never a blind resend).
The base URL and Graph version come from configuration so the demo can point the
same client at the mock Graph server.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx2

from concierge.channels.whatsapp.port import GraphError, MediaInfo, SendResult, TemplateRecord

DEFAULT_BASE_URL = "https://graph.facebook.com"


def in_process_client(version: str, asgi_app: Any, *, base_url: str) -> GraphClient:
    """A Graph client whose HTTP stack is routed into an ASGI app (the demo's mock server).

    The full client code runs; only the socket is replaced, so a demo never depends on
    the server accepting its own connections during startup.
    """
    return GraphClient(
        version=version, base_url=base_url, transport=httpx2.ASGITransport(app=asgi_app)
    )


class GraphClient:
    def __init__(
        self,
        *,
        version: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 10.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.version = version
        self.base_url = base_url.rstrip("/")
        kwargs: dict[str, Any] = {"timeout": timeout_s}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx2.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{self.version}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = await self._client.request(
                method, self._url(path), headers=headers, json=json, params=params
            )
        except httpx2.TimeoutException as exc:
            raise GraphError(-1, "timeout", details={"timeout": True}) from exc
        except httpx2.HTTPError as exc:
            raise GraphError(-1, f"network error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise _error_from_response(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise GraphError(-2, "malformed response body", status=response.status_code) from exc
        if not isinstance(data, dict):
            raise GraphError(-2, "unexpected response shape", status=response.status_code)
        return data

    # --------------------------------------------------------------- send
    async def send_message(
        self, phone_number_id: str, payload: dict[str, Any], *, access_token: str
    ) -> SendResult:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
        try:
            data = await self._request(
                "POST", f"{phone_number_id}/messages", access_token=access_token, json=body
            )
        except GraphError as exc:
            if exc.details.get("timeout"):
                return SendResult(ok=False, error=exc, unknown=True)
            return SendResult(ok=False, error=exc)
        messages = data.get("messages") or []
        wamid = str(messages[0].get("id")) if messages and isinstance(messages[0], dict) else None
        return SendResult(ok=True, wamid=wamid)

    async def mark_read(
        self, phone_number_id: str, message_id: str, *, access_token: str, typing: bool = False
    ) -> None:
        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        if typing:
            body["typing_indicator"] = {"type": "text"}
        await self._request(
            "POST", f"{phone_number_id}/messages", access_token=access_token, json=body
        )

    # -------------------------------------------------------------- media
    async def media_info(self, media_id: str, *, access_token: str) -> MediaInfo:
        data = await self._request("GET", media_id, access_token=access_token)
        return MediaInfo(
            url=str(data.get("url", "")),
            mime_type=str(data.get("mime_type", "")),
            sha256=str(data.get("sha256", "")),
            file_size=int(data.get("file_size", 0) or 0),
        )

    async def download_media(self, url: str, *, access_token: str, max_bytes: int) -> bytes:
        try:
            async with self._client.stream(
                "GET", url, headers={"Authorization": f"Bearer {access_token}"}
            ) as response:
                if response.status_code >= 400:
                    raise GraphError(
                        -3,
                        f"media download failed: HTTP {response.status_code}",
                        status=response.status_code,
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise GraphError(-4, "media larger than the allowed size")
                    chunks.append(chunk)
        except httpx2.HTTPError as exc:
            raise GraphError(-1, f"network error: {type(exc).__name__}") from exc
        return b"".join(chunks)

    # ----------------------------------------------------------- doctor
    async def phone_info(self, phone_number_id: str, *, access_token: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            phone_number_id,
            access_token=access_token,
            params={
                "fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier"
            },
        )

    async def subscribed_apps(self, waba_id: str, *, access_token: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"{waba_id}/subscribed_apps", access_token=access_token)
        items = data.get("data") or []
        return [i for i in items if isinstance(i, dict)]

    # -------------------------------------------------------- templates
    async def list_templates(self, waba_id: str, *, access_token: str) -> list[TemplateRecord]:
        records: list[TemplateRecord] = []
        params: dict[str, Any] = {
            "fields": "name,language,category,status,id,components",
            "limit": 100,
        }
        data = await self._request(
            "GET", f"{waba_id}/message_templates", access_token=access_token, params=params
        )
        for item in data.get("data") or []:
            if isinstance(item, dict):
                records.append(_template_record(item))
        return records

    async def create_template(
        self, waba_id: str, template: dict[str, Any], *, access_token: str
    ) -> TemplateRecord:
        data = await self._request(
            "POST", f"{waba_id}/message_templates", access_token=access_token, json=template
        )
        return TemplateRecord(
            name=str(template.get("name", "")),
            language=str(template.get("language", "")),
            category=str(data.get("category") or template.get("category", "")),
            status=str(data.get("status", "PENDING")),
            id=str(data.get("id", "")),
            components=list(template.get("components", [])),
        )


def _template_record(item: dict[str, Any]) -> TemplateRecord:
    return TemplateRecord(
        name=str(item.get("name", "")),
        language=str(item.get("language", "")),
        category=str(item.get("category", "")),
        status=str(item.get("status", "")),
        id=str(item.get("id", "")),
        reason=str(item.get("rejected_reason", "") or ""),
        components=list(item.get("components") or []),
    )


def _error_from_response(response: httpx2.Response) -> GraphError:
    retry_after: float | None = None
    header = response.headers.get("retry-after")
    if header and header.replace(".", "", 1).isdigit():
        retry_after = float(header)
    try:
        body = response.json()
    except ValueError:
        return GraphError(
            -2,
            f"HTTP {response.status_code} with a non-JSON body",
            status=response.status_code,
            retry_after=retry_after,
        )
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return GraphError(
            -2, f"HTTP {response.status_code}", status=response.status_code, retry_after=retry_after
        )
    code = error.get("code")
    details = error.get("error_data") if isinstance(error.get("error_data"), dict) else {}
    return GraphError(
        int(code) if isinstance(code, int) else -2,
        str(error.get("message", "Graph API error")),
        subcode=error.get("error_subcode") if isinstance(error.get("error_subcode"), int) else None,
        status=response.status_code,
        retry_after=retry_after,
        details={
            **details,
            "type": error.get("type", ""),
            "fbtrace_id": error.get("fbtrace_id", ""),
        },
    )


def media_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
