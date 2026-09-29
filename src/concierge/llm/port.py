"""The ``LLMPort``: everything the conversation engine needs from a language model.

Implements: F01, F35, 8.1, 8.4

Core code depends only on these types. The Anthropic adapter and the scripted fake
implement the protocol. Assistant turns produced inside a tool loop carry ``raw``
content so the adapter can replay them byte-identical (8.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

StopReason = Literal[
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
]
ErrorKind = Literal[
    "rate_limit",
    "overloaded",
    "timeout",
    "spend_cap",
    "invalid_request",
    "auth",
    "connection",
    "server",
    "unknown",
]


@dataclass(frozen=True)
class TextBlock:
    """A system block; ``cache`` marks the prompt-cache breakpoint."""

    text: str
    cache: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = True


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


Part = TextPart | ToolUse | ToolResult


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: list[Part]
    raw: list[dict[str, Any]] | None = None
    """Provider content blocks exactly as returned; replayed verbatim when present."""


@dataclass
class LLMRequest:
    model: str
    system: list[TextBlock]
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int = 1024
    effort: Literal["low", "medium", "high"] | None = "low"
    output_schema: dict[str, Any] | None = None
    inference_geo: str | None = "global"
    metadata: dict[str, str] = field(default_factory=dict)
    cache_tail: bool = True


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    inference_geo: str | None = None


@dataclass
class LLMResponse:
    content: list[TextPart | ToolUse]
    stop_reason: StopReason | None
    usage: Usage
    model: str
    request_id: str | None = None
    raw: list[dict[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.content if isinstance(p, TextPart))

    @property
    def tool_uses(self) -> list[ToolUse]:
        return [p for p in self.content if isinstance(p, ToolUse)]


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str = ""
    max_input_tokens: int | None = None
    max_tokens: int | None = None


class LLMError(Exception):
    """A provider failure classified for the failover policy (8.1)."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.kind in {"rate_limit", "overloaded", "timeout", "connection", "server"}


class LLMPort(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def count_tokens(self, request: LLMRequest) -> int: ...

    async def list_models(self) -> list[ModelInfo]: ...
