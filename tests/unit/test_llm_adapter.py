"""Anthropic adapter: request shape, replay, usage and error classification via MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from concierge.llm.anthropic_adapter import AnthropicAdapter
from concierge.llm.models import capabilities_for
from concierge.llm.port import (
    LLMError,
    LLMRequest,
    Message,
    TextBlock,
    TextPart,
    ToolResult,
    ToolSpec,
)
from concierge.llm.scripted import ScriptedLLM, Step

pytestmark = pytest.mark.feature("F01")


def _response(
    payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None
) -> httpx2.Response:
    return httpx2.Response(
        status, json=payload, headers={"request-id": "req_test_1", **(headers or {})}
    )


def _message_payload(text: str = '{"reply_text": "ok"}') -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 900,
            "output_tokens": 40,
            "cache_creation_input_tokens": 600,
            "cache_read_input_tokens": 300,
            "inference_geo": "global",
        },
    }


def _adapter(handler: Any, retries: int = 0) -> AnthropicAdapter:
    return AnthropicAdapter(
        "test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        max_retries=retries,
    )


def _request(model: str = "claude-sonnet-5-5", **kw: Any) -> LLMRequest:
    return LLMRequest(
        model=model,
        system=[TextBlock("rules " * 10, cache=True), TextBlock("tenant")],
        messages=[Message("user", [TextPart("Hallo")])],
        tools=[
            ToolSpec(
                "lookup_kb",
                "look",
                {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "check_availability",
                "avail",
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ],
        output_schema={
            "type": "object",
            "properties": {"reply_text": {"type": "string"}},
            "required": ["reply_text"],
            "additionalProperties": False,
        },
        metadata={"user_id": "wa:123"},
        **kw,
    )


async def test_request_shape_follows_verified_rules() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["json"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return _response(_message_payload())

    result = await _adapter(handler).complete(_request())
    body = captured["json"]
    for forbidden in ("temperature", "top_p", "top_k", "thinking"):
        assert forbidden not in body
    assert body["tool_choice"] == {"type": "auto"}
    assert [t["name"] for t in body["tools"]] == ["check_availability", "lookup_kb"]
    assert all(t["strict"] is True for t in body["tools"])
    assert body["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": _request().output_schema},
    }
    assert body["inference_geo"] == "global"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in body["system"][1]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["metadata"]["user_id"] != "wa:123" and len(body["metadata"]["user_id"]) == 32
    assert captured["headers"]["x-api-key"] == "test-key"
    assert result.text == '{"reply_text": "ok"}'
    assert result.usage.cache_read_tokens == 300 and result.usage.cache_write_tokens == 600
    assert result.usage.inference_geo == "global"
    assert result.request_id == "req_test_1"
    assert result.raw == [{"type": "text", "text": '{"reply_text": "ok"}'}]


async def test_haiku_omits_effort_and_geo() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["json"] = json.loads(request.content)
        return _response(_message_payload())

    await _adapter(handler).complete(_request(model="claude-haiku-4-5", effort="low"))
    assert "inference_geo" not in captured["json"]
    assert "effort" not in captured["json"].get("output_config", {})
    assert not capabilities_for("claude-haiku-4-5").supports_effort
    assert capabilities_for("claude-sonnet-5-5").min_cache_prefix_tokens == 512


async def test_tool_loop_replays_raw_and_orders_tool_results_first() -> None:
    captured: list[dict[str, Any]] = []
    responses = [
        {
            **_message_payload(),
            "content": [
                {"type": "text", "text": "thinking aloud"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "lookup_kb",
                    "input": {"key": "parking"},
                },
            ],
            "stop_reason": "tool_use",
        },
        _message_payload('{"reply_text": "done"}'),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        return _response(responses[len(captured) - 1])

    adapter = _adapter(handler)
    first = await adapter.complete(_request())
    assert first.stop_reason == "tool_use" and first.tool_uses[0].input == {"key": "parking"}
    follow_up = _request()
    follow_up.messages = [
        Message("user", [TextPart("Hallo")]),
        Message("assistant", first.content, raw=first.raw),
        Message(
            "user",
            [
                TextPart("envelope"),
                ToolResult("toolu_1", '{"ok": true}'),
                ToolResult("toolu_x", "bad", is_error=True),
            ],
        ),
    ]
    await adapter.complete(follow_up)
    assistant_turn = captured[1]["messages"][1]
    assert assistant_turn["content"] == first.raw
    user_turn = captured[1]["messages"][2]["content"]
    assert [p["type"] for p in user_turn] == ["tool_result", "tool_result", "text"]
    assert user_turn[1]["is_error"] is True


@pytest.mark.parametrize(
    ("status", "body", "headers", "kind"),
    [
        (
            429,
            {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            {"retry-after": "3"},
            "rate_limit",
        ),
        (
            429,
            {
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": "cap",
                    "details": {"error_code": "enforced_spend_limit_reached"},
                },
            },
            {},
            "spend_cap",
        ),
        (
            400,
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "You have reached your specified API usage limits",
                },
            },
            {},
            "spend_cap",
        ),
        (
            400,
            {"type": "error", "error": {"type": "invalid_request_error", "message": "bad schema"}},
            {},
            "invalid_request",
        ),
        (
            401,
            {"type": "error", "error": {"type": "authentication_error", "message": "no"}},
            {},
            "auth",
        ),
        (
            529,
            {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
            {},
            "overloaded",
        ),
        (500, {"type": "error", "error": {"type": "api_error", "message": "boom"}}, {}, "server"),
    ],
)
async def test_error_classification(
    status: int, body: dict[str, Any], headers: dict[str, str], kind: str
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _response(body, status=status, headers=headers)

    with pytest.raises(LLMError) as info:
        await _adapter(handler).complete(_request())
    assert info.value.kind == kind
    assert info.value.status == status
    if kind == "rate_limit":
        assert info.value.retry_after == 3.0 and info.value.retryable
    if kind == "spend_cap":
        assert not info.value.retryable


async def test_timeout_and_connection_errors() -> None:
    def timeout_handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    with pytest.raises(LLMError) as info:
        await _adapter(timeout_handler).complete(_request())
    assert info.value.kind == "timeout"

    def connect_handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    with pytest.raises(LLMError) as info2:
        await _adapter(connect_handler).complete(_request())
    assert info2.value.kind == "connection"


async def test_count_tokens_and_models() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("count_tokens"):
            body = json.loads(request.content)
            assert "output_config" not in body and "cache_control" not in body
            return _response({"input_tokens": 777})
        return _response(
            {
                "data": [
                    {
                        "id": "claude-sonnet-5-5",
                        "display_name": "Sonnet 5.5",
                        "type": "model",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "has_more": False,
                "first_id": "a",
                "last_id": "a",
            }
        )

    adapter = _adapter(handler)
    assert await adapter.count_tokens(_request()) == 777
    assert [m.id for m in await adapter.list_models()] == ["claude-sonnet-5-5"]


def test_missing_key_is_an_auth_error() -> None:
    with pytest.raises(LLMError) as info:
        AnthropicAdapter("")
    assert info.value.kind == "auth"


async def test_scripted_llm_steps_and_recording() -> None:
    fake = ScriptedLLM(
        [
            Step.tools(("lookup_kb", {"key": "parking"})),
            Step.final({"reply_text": "x"}),
            Step.fail("overloaded"),
        ]
    )
    first = await fake.complete(_request())
    assert first.stop_reason == "tool_use" and first.tool_uses[0].name == "lookup_kb"
    second = await fake.complete(_request())
    assert json.loads(second.text) == {"reply_text": "x"}
    with pytest.raises(LLMError):
        await fake.complete(_request())
    with pytest.raises(LLMError, match="no steps"):
        await fake.complete(_request())
    assert len(fake.requests) == 4
    assert await fake.count_tokens(_request()) > 0
