"""The live evaluation loop offline (F35): refusal without a key, budget cap, reporting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concierge.eval.golden import load_cases
from concierge.eval.live import LiveEvalError, run_live, usage_cost_usd
from concierge.llm.port import LLMRequest, LLMResponse, TextPart, Usage
from concierge.settings import Settings

pytestmark = pytest.mark.feature("F35")

REPLY = {
    "reply_text": "Thank you very much for the kind words. Discounts are decided by the reception.",
    "language": "en",
    "intent": "A:feedback",
    "handoff": {"needed": False, "priority": "none", "reason": ""},
    "fact_refs": [],
    "confidence": 0.9,
    "buttons": [],
}


GERMAN_REPLY = {
    **REPLY,
    "reply_text": "Vielen Dank für die freundlichen Worte; das Team der Rezeption freut sich.",
    "language": "de",
}


class RepeatLLM:
    """Answers every request with one final reply in the requested language."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        last = request.messages[-1].content[0]
        envelope = last.text if isinstance(last, TextPart) else ""
        payload = GERMAN_REPLY if "reply_language: de" in envelope else REPLY
        return LLMResponse(
            content=[TextPart(json.dumps(payload, ensure_ascii=False))],
            stop_reason="end_turn",
            usage=Usage(input_tokens=1000, output_tokens=50, cache_read_tokens=800),
            model=request.model,
        )

    async def models(self) -> list[str]:
        return ["claude-sonnet-5-5"]


def test_refuses_without_a_key(settings: Settings, tmp_path: Path) -> None:
    import asyncio

    with pytest.raises(LiveEvalError, match="ANTHROPIC_API_KEY"):
        asyncio.run(run_live(settings, cases=[], work_dir=tmp_path))


async def test_loop_budget_and_report(settings: Settings, tmp_path: Path) -> None:
    case = next(c for c in load_cases() if c.id == "x05_injection_review_text:en")
    llm = RepeatLLM()
    report = await run_live(
        settings,
        cases=[case],
        work_dir=tmp_path,
        repeats=2,
        budget_usd=0.5,
        llm=llm,
        efforts=("low", "medium"),
    )
    assert report.ok and len(report.runs) == 4
    assert [r.effort for r in report.runs] == ["low", "low", "medium", "medium"]
    assert all(r.cost_usd > 0 for r in report.runs) and report.total_usd < 0.5
    assert report.cache_read_ok is True and report.history_ok is True
    assert llm.requests[0].effort == "low" and llm.requests[2].effort == "medium"
    data = report.to_dict()
    assert data["latency"]["en/low"]["turns"] == 2 and data["stopped_by_budget"] is False

    tiny_dir = tmp_path / "tiny"
    tiny_dir.mkdir()
    tiny = await run_live(
        settings, cases=[case], work_dir=tiny_dir, repeats=3, budget_usd=0.00001, llm=llm
    )
    assert tiny.stopped_by_budget and len(tiny.runs) == 1 and tiny.history_ok is None


def test_usage_cost_uses_the_default_price_table() -> None:
    usage = [Usage(input_tokens=1_000_000, output_tokens=0)]
    assert usage_cost_usd(usage, "claude-sonnet-5-5") == 2.0
    assert usage_cost_usd(usage, "unknown-model") == 0.0
