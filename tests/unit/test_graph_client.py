"""Graph API adapter via httpx2.MockTransport: sends, errors, timeouts, media, templates."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from concierge.channels.whatsapp.graph import GraphClient
from concierge.channels.whatsapp.port import GraphError

pytestmark = pytest.mark.feature("F18")


def _client(handler: Any) -> GraphClient:
    return GraphClient(version="v26.0", transport=httpx2.MockTransport(handler))


async def test_send_message_success_and_headers() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.abc"}]}
        )

    result = await _client(handler).send_message(
        "PN1", {"to": "49151", "type": "text", "text": {"body": "hi"}}, access_token="tok"
    )
    assert result.ok and result.wamid == "wamid.abc"
    assert seen["url"] == "https://graph.facebook.com/v26.0/PN1/messages"
    assert seen["auth"] == "Bearer tok"
    assert (
        seen["body"]["messaging_product"] == "whatsapp"
        and seen["body"]["recipient_type"] == "individual"
    )


@pytest.mark.parametrize(
    ("status", "body", "headers", "code", "retry_after"),
    [
        (
            400,
            {
                "error": {
                    "message": "Re-engagement message",
                    "type": "OAuthException",
                    "code": 131047,
                    "error_subcode": 2494010,
                    "fbtrace_id": "x",
                }
            },
            {},
            131047,
            None,
        ),
        (
            429,
            {"error": {"message": "Rate limit hit", "code": 130429}},
            {"retry-after": "7"},
            130429,
            7.0,
        ),
        (401, {"error": {"message": "Invalid OAuth access token", "code": 190}}, {}, 190, None),
        (500, "not json", {}, -2, None),
    ],
)
async def test_error_mapping(
    status: int, body: Any, headers: dict[str, str], code: int, retry_after: float | None
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if isinstance(body, str):
            return httpx2.Response(status, content=body, headers=headers)
        return httpx2.Response(status, json=body, headers=headers)

    result = await _client(handler).send_message(
        "PN1", {"to": "1", "type": "text", "text": {"body": "x"}}, access_token="t"
    )
    assert not result.ok and not result.unknown
    assert (
        result.error is not None
        and result.error.code == code
        and result.error.retry_after == retry_after
    )
    assert result.error.status == status


async def test_timeout_is_unknown_and_network_is_error() -> None:
    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    result = await _client(timeout).send_message(
        "PN1", {"to": "1", "type": "text", "text": {"body": "x"}}, access_token="t"
    )
    assert result.unknown and not result.ok and result.error is not None and result.error.is_network

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    result = await _client(down).send_message(
        "PN1", {"to": "1", "type": "text", "text": {"body": "x"}}, access_token="t"
    )
    assert not result.unknown and result.error is not None and result.error.code == -1


async def test_mark_read_media_phone_templates() -> None:
    calls: list[tuple[str, str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        calls.append(
            (request.method, path, json.loads(request.content) if request.content else None)
        )
        if path.endswith("/PN1/messages"):
            return httpx2.Response(200, json={"success": True})
        if path.endswith("/media-1"):
            return httpx2.Response(
                200,
                json={
                    "url": "https://lookaside.invalid/m",
                    "mime_type": "image/jpeg",
                    "sha256": "abc",
                    "file_size": 3,
                },
            )
        if request.url.host == "lookaside.invalid":
            return httpx2.Response(200, content=b"\xff\xd8\xff")
        if path.endswith("/PN1"):
            return httpx2.Response(
                200,
                json={
                    "id": "PN1",
                    "display_phone_number": "+49 69 0",
                    "verified_name": "Hotel",
                    "quality_rating": "GREEN",
                },
            )
        if path.endswith("/subscribed_apps"):
            return httpx2.Response(
                200, json={"data": [{"whatsapp_business_api_data": {"name": "gc"}}]}
            )
        if path.endswith("/message_templates") and request.method == "GET":
            return httpx2.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "gc_prearrival_2d",
                            "language": "de",
                            "category": "UTILITY",
                            "status": "APPROVED",
                            "id": "1",
                            "components": [],
                        }
                    ]
                },
            )
        if path.endswith("/message_templates") and request.method == "POST":
            return httpx2.Response(
                200, json={"id": "9", "status": "PENDING", "category": "UTILITY"}
            )
        return httpx2.Response(404, json={"error": {"message": "nope", "code": 100}})

    client = _client(handler)
    await client.mark_read("PN1", "wamid.1", access_token="t", typing=True)
    assert calls[-1][2]["typing_indicator"] == {"type": "text"} and calls[-1][2]["status"] == "read"
    info = await client.media_info("media-1", access_token="t")
    assert info.mime_type == "image/jpeg" and info.file_size == 3
    data = await client.download_media(info.url, access_token="t", max_bytes=10)
    assert data == b"\xff\xd8\xff"
    with pytest.raises(GraphError, match="larger"):
        await client.download_media(info.url, access_token="t", max_bytes=2)
    phone = await client.phone_info("PN1", access_token="t")
    assert phone["quality_rating"] == "GREEN"
    assert (await client.subscribed_apps("WABA", access_token="t"))[0][
        "whatsapp_business_api_data"
    ]["name"] == "gc"
    templates = await client.list_templates("WABA", access_token="t")
    assert templates[0].name == "gc_prearrival_2d" and templates[0].status == "APPROVED"
    created = await client.create_template(
        "WABA",
        {"name": "gc_x", "language": "de", "category": "UTILITY", "components": []},
        access_token="t",
    )
    assert created.id == "9" and created.status == "PENDING"
    with pytest.raises(GraphError) as info_err:
        await client.media_info("missing", access_token="t")
    assert info_err.value.code == 100 and info_err.value.status == 404
    await client.aclose()
