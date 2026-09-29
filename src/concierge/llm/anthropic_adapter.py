"""Anthropic Messages API adapter behind ``LLMPort``.

Implements: F01, 8.1, 8.2, 8.4, C11

Rules from ``docs/VERIFIED_FACTS.md``: never send ``temperature``, ``top_p``,
``top_k``, ``thinking`` or a forced ``tool_choice``; effort and ``inference_geo`` only
for models that support them; structured output through ``output_config.format``;
``cache_control`` on the marked system block and at the top level; SDK retries stay
at two and the caller wraps each turn in a timeout (8.1). Errors are classified for
the failover policy; a spend cap is never retried.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import anthropic
import httpx2

from concierge.llm.models import capabilities_for
from concierge.llm.port import (
    ErrorKind,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    ModelInfo,
    TextPart,
    ToolResult,
    ToolUse,
    Usage,
)

_SPEND_CAP_MARKERS = ("enforced_spend_limit_reached", "specified API usage limits", "spend limit")


class AnthropicAdapter:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_s: float = 20.0,
        max_retries: int = 2,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise LLMError("auth", "ANTHROPIC_API_KEY is not set")
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,
            max_retries=max_retries,
            http_client=http_client,
        )

    # ------------------------------------------------------------ requests
    def build_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        caps = capabilities_for(request.model)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": [self._system_block(b) for b in request.system],
            "messages": [self._message(m) for m in request.messages],
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "strict": t.strict,
                }
                for t in sorted(request.tools, key=lambda t: t.name)
            ]
            kwargs["tool_choice"] = {"type": "auto"}
        output_config: dict[str, Any] = {}
        if request.effort and caps.supports_effort:
            output_config["effort"] = request.effort
        if request.output_schema and caps.supports_structured_outputs:
            output_config["format"] = {"type": "json_schema", "schema": request.output_schema}
        if output_config:
            kwargs["output_config"] = output_config
        if request.inference_geo and caps.supports_inference_geo:
            kwargs["inference_geo"] = request.inference_geo
        if request.metadata.get("user_id"):
            kwargs["metadata"] = {"user_id": _hash_user(request.metadata["user_id"])}
        if request.cache_tail:
            kwargs["cache_control"] = {"type": "ephemeral"}
        return kwargs

    @staticmethod
    def _system_block(block: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache:
            out["cache_control"] = {"type": "ephemeral"}
        return out

    @staticmethod
    def _message(message: Message) -> dict[str, Any]:
        if message.raw is not None:
            return {"role": message.role, "content": message.raw}
        parts: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, TextPart):
                parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ToolUse):
                parts.append(
                    {"type": "tool_use", "id": part.id, "name": part.name, "input": part.input}
                )
            elif isinstance(part, ToolResult):
                item: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": part.tool_use_id,
                    "content": part.content,
                }
                if part.is_error:
                    item["is_error"] = True
                parts.append(item)
        # every tool_result block of a turn comes first, in one user message (10.11)
        parts.sort(key=lambda p: 0 if p["type"] == "tool_result" else 1)
        return {"role": message.role, "content": parts}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs = self.build_kwargs(request)
        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise classify(exc) from exc
        content: list[TextPart | ToolUse] = []
        raw: list[dict[str, Any]] = []
        for block in response.content:
            raw.append(block.model_dump(exclude_none=True))
            if block.type == "text":
                content.append(TextPart(block.text))
            elif block.type == "tool_use":
                content.append(ToolUse(id=block.id, name=block.name, input=dict(block.input or {})))
        usage = response.usage
        return LLMResponse(
            content=content,
            stop_reason=response.stop_reason,
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                inference_geo=usage.inference_geo,
            ),
            model=response.model,
            request_id=getattr(response, "_request_id", None),
            raw=raw,
        )

    async def count_tokens(self, request: LLMRequest) -> int:
        kwargs = self.build_kwargs(request)
        allowed = {"model", "system", "messages", "tools", "tool_choice"}
        payload = {k: v for k, v in kwargs.items() if k in allowed}
        try:
            result = await self._client.messages.count_tokens(**payload)
        except anthropic.APIError as exc:
            raise classify(exc) from exc
        return int(result.input_tokens)

    async def list_models(self) -> list[ModelInfo]:
        try:
            page = await self._client.models.list(limit=100)
        except anthropic.APIError as exc:
            raise classify(exc) from exc
        return [
            ModelInfo(
                id=m.id,
                display_name=getattr(m, "display_name", "") or "",
                max_input_tokens=getattr(m, "max_input_tokens", None),
                max_tokens=getattr(m, "max_tokens", None),
            )
            for m in page.data
        ]


def _hash_user(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


def classify(exc: anthropic.APIError) -> LLMError:
    """Map SDK exceptions to the failover policy's error kinds."""
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    body = getattr(exc, "body", None)
    text = json.dumps(body, ensure_ascii=False) if body else str(exc)
    retry_after: float | None = None
    response = getattr(exc, "response", None)
    if response is not None:
        header = response.headers.get("retry-after")
        if header and header.replace(".", "", 1).isdigit():
            retry_after = float(header)
    kind: ErrorKind
    if any(marker in text for marker in _SPEND_CAP_MARKERS):
        kind = "spend_cap"
    elif isinstance(exc, anthropic.APITimeoutError):
        kind = "timeout"
    elif isinstance(exc, anthropic.APIConnectionError):
        kind = "connection"
    elif isinstance(exc, anthropic.RateLimitError):
        kind = "spend_cap" if retry_after is None and "spend" in text.lower() else "rate_limit"
    elif isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        kind = "auth"
    elif isinstance(
        exc,
        anthropic.BadRequestError | anthropic.UnprocessableEntityError | anthropic.NotFoundError,
    ):
        kind = "invalid_request"
    elif status == 529 or isinstance(exc, anthropic.OverloadedError):
        kind = "overloaded"
    elif status is not None and status >= 500:
        kind = "server"
    else:
        kind = "unknown"
    return LLMError(kind, str(exc), status=status, retry_after=retry_after, request_id=request_id)
