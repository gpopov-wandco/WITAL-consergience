"""The structured reply the model must return (8.4) and its JSON schema.

Implements: F01, F23, 8.4

The schema sent to the provider uses only features structured outputs support
(no ``minimum``, ``maxLength`` or ``pattern``; ``additionalProperties: false``; every
property required). Ranges and lengths are validated here with pydantic after the
response arrives.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Priority = Literal["P0", "P1", "P2", "P3", "none"]
IntentCode = Literal["A", "P", "Q", "H", "R"]

MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20


class HandoffDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needed: bool
    priority: Priority = "none"
    reason: str = ""


class Button(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not value or len(value) > 64 or not all(ch.isalnum() or ch in "_:-" for ch in value):
            raise ValueError("button id must be 1 to 64 characters of letters, digits, _ : -")
        return value

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > MAX_BUTTON_TITLE:
            raise ValueError(f"button title must be 1 to {MAX_BUTTON_TITLE} characters")
        return value


class AgentReply(BaseModel):
    """One validated reply; ``intent`` is ``<code>:<topic>`` (codes A, P, Q, H, R)."""

    model_config = ConfigDict(extra="forbid")

    reply_text: str
    language: Literal["de", "en", "ru", "uk", "tr", "it"]
    intent: str = Field(description="intent code and topic, for example A:parking")
    handoff: HandoffDecision
    fact_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(description="0 to 1")
    buttons: list[Button] = Field(default_factory=list)

    @field_validator("reply_text")
    @classmethod
    def _text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reply_text is empty")
        return value

    @field_validator("intent")
    @classmethod
    def _intent(cls, value: str) -> str:
        code, sep, topic = value.partition(":")
        if code not in {"A", "P", "Q", "H", "R"} or not sep or not topic:
            raise ValueError("intent must be <A|P|Q|H|R>:<topic>")
        return value

    @field_validator("confidence")
    @classmethod
    def _confidence(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return value

    @field_validator("buttons")
    @classmethod
    def _buttons(cls, value: list[Button]) -> list[Button]:
        if len(value) > MAX_BUTTONS:
            raise ValueError(f"at most {MAX_BUTTONS} buttons")
        if len({b.id for b in value}) != len(value):
            raise ValueError("button ids must be unique")
        return value

    @property
    def intent_code(self) -> IntentCode:
        return self.intent.split(":", 1)[0]  # type: ignore[return-value]

    @property
    def topic(self) -> str:
        return self.intent.split(":", 1)[1]


def output_schema() -> dict[str, Any]:
    """JSON schema for ``output_config.format`` (structured outputs)."""
    return {
        "type": "object",
        "properties": {
            "reply_text": {
                "type": "string",
                "description": "The message to the guest, 1 to 3 short sentences, at most 350 characters.",
            },
            "language": {"type": "string", "enum": ["de", "en", "ru", "uk", "tr", "it"]},
            "intent": {
                "type": "string",
                "description": "Intent code and topic: A answered from the hotel facts, P from a live source, Q a question back, H handoff to staff, R refusal. Example: A:parking",
            },
            "handoff": {
                "type": "object",
                "properties": {
                    "needed": {"type": "boolean"},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3", "none"]},
                    "reason": {"type": "string"},
                },
                "required": ["needed", "priority", "reason"],
                "additionalProperties": False,
            },
            "fact_refs": {
                "type": "array",
                "description": "Keys of the hotel facts or tool results the reply relies on.",
                "items": {"type": "string"},
            },
            "confidence": {"type": "number", "description": "Between 0 and 1."},
            "buttons": {
                "type": "array",
                "description": "At most 3 quick replies, titles of at most 20 characters.",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
                    "required": ["id", "title"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["reply_text", "language", "intent", "handoff", "fact_refs", "confidence", "buttons"],
        "additionalProperties": False,
    }
