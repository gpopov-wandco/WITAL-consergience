"""``ScriptedLLM``: a deterministic fake that scripts the model's steps.

Implements: F35, 8.6, 10.4

A script is a list of steps consumed in order. Each step is a final JSON reply, a
list of tool calls, a failure to raise or a delay. Every request is recorded so tests
can assert on prompt assembly (tool order, cache breakpoints, envelope placement)
without asserting on prose.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from concierge.llm.port import (
    LLMError,
    LLMRequest,
    LLMResponse,
    ModelInfo,
    StopReason,
    TextPart,
    ToolUse,
    Usage,
)


@dataclass
class Step:
    reply: dict[str, Any] | str | None = None
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    error: LLMError | None = None
    delay_s: float = 0.0
    stop_reason: StopReason | None = None
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=1200, output_tokens=80))

    @classmethod
    def final(cls, reply: dict[str, Any] | str, **kw: Any) -> Step:
        return cls(reply=reply, **kw)

    @classmethod
    def tools(cls, *calls: tuple[str, dict[str, Any]]) -> Step:
        return cls(tool_calls=list(calls))

    @classmethod
    def fail(cls, kind: str, message: str = "scripted failure", **kw: Any) -> Step:
        return cls(error=LLMError(kind, message, **kw))  # type: ignore[arg-type]

    @classmethod
    def hang(cls, seconds: float) -> Step:
        return cls(delay_s=seconds, reply="")


class ScriptedLLM:
    def __init__(self, steps: list[Step] | None = None, *, models: list[str] | None = None) -> None:
        self.steps: list[Step] = list(steps or [])
        self.requests: list[LLMRequest] = []
        self.models = models or ["claude-sonnet-5-5", "claude-sonnet-5", "claude-haiku-4-5"]
        self._counter = 0

    def load(self, steps: list[Step]) -> None:
        self.steps = list(steps)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.steps:
            raise LLMError("unknown", "ScriptedLLM has no steps left")
        step = self.steps.pop(0)
        if step.delay_s:
            await asyncio.sleep(step.delay_s)
        if step.error is not None:
            raise step.error
        self._counter += 1
        if step.tool_calls:
            content: list[TextPart | ToolUse] = [
                ToolUse(id=f"toolu_{self._counter}_{i}", name=name, input=args)
                for i, (name, args) in enumerate(step.tool_calls)
            ]
            raw = [
                {"type": "tool_use", "id": p.id, "name": p.name, "input": p.input}
                for p in content
                if isinstance(p, ToolUse)
            ]
            return LLMResponse(
                content=content,
                stop_reason="tool_use",
                usage=step.usage,
                model=request.model,
                raw=raw,
            )
        text = (
            step.reply
            if isinstance(step.reply, str)
            else json.dumps(step.reply, ensure_ascii=False)
        )
        return LLMResponse(
            content=[TextPart(text)],
            stop_reason=step.stop_reason or "end_turn",
            usage=step.usage,
            model=request.model,
            request_id=f"req_scripted_{self._counter}",
            raw=[{"type": "text", "text": text}],
        )

    async def count_tokens(self, request: LLMRequest) -> int:
        chars = sum(len(b.text) for b in request.system)
        chars += sum(len(json.dumps(t.input_schema)) + len(t.description) for t in request.tools)
        for m in request.messages:
            for p in m.content:
                chars += len(
                    getattr(p, "text", "")
                    or getattr(p, "content", "")
                    or json.dumps(getattr(p, "input", {}))
                )
        return max(1, chars // 4)

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=m) for m in self.models]
