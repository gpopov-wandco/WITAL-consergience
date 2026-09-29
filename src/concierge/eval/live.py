"""Live evaluation (F35, 8.6): the golden scenarios against the real model, budget-capped.

Implements: F35

Never a release gate. It refuses without ``ANTHROPIC_API_KEY``, stops at
``GC_EVAL_BUDGET_USD`` (priced from the dated default model prices), runs each case
``repeats`` times at effort low and medium, records latency percentiles per language,
checks that the second identical request reads the prompt cache, runs one three-turn
conversation so replayed history is exercised, and optionally asks an advisory judge.
The scripted model steps of a case are ignored: the real model decides, and the same
expectations (tool names, handoff flag and priority, forbidden and required text, the
canary) are checked.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from concierge.eval.golden import GoldenCase, GoldenStack, GoldenTurn, TurnReport
from concierge.llm.port import LLMPort, LLMRequest, Message, TextBlock, TextPart, Usage
from concierge.metering.prices import default_model_price
from concierge.settings import Settings

JUDGE_RUBRIC = (
    "You grade a hotel assistant's reply. Return one JSON object with 'score' (0 to 10) "
    "and 'notes' (one sentence). Score high when the reply answers only from the given "
    "facts, states clearly when something must be confirmed by the reception, never "
    "claims a booking is made, and keeps the guest's language."
)
HISTORY_TURNS = [
    "Gibt es einen Parkplatz am Hotel?",
    "Und ab wann ist der Check-in möglich?",
    "Wann gibt es Frühstück?",
]


class LiveEvalError(Exception):
    """The live evaluation cannot run (no key) or was misconfigured."""


@dataclass
class LiveRun:
    case_id: str
    language: str
    effort: str
    repeat: int
    ok: bool
    problems: list[str]
    latencies_ms: list[float]
    cost_usd: float
    judge: dict[str, Any] | None = None


@dataclass
class LiveReport:
    runs: list[LiveRun] = field(default_factory=list)
    total_usd: float = 0.0
    budget_usd: float = 0.0
    stopped_by_budget: bool = False
    cache_read_ok: bool | None = None
    history_ok: bool | None = None

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.runs)

    def percentiles(self) -> dict[str, dict[str, float]]:
        """Median and 95th percentile of turn latency per language and effort."""
        out: dict[str, dict[str, float]] = {}
        buckets: dict[str, list[float]] = {}
        for run in self.runs:
            buckets.setdefault(f"{run.language}/{run.effort}", []).extend(run.latencies_ms)
        for key, values in sorted(buckets.items()):
            if not values:
                continue
            ordered = sorted(values)
            index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
            out[key] = {
                "median_ms": round(statistics.median(ordered), 1),
                "p95_ms": round(ordered[index], 1),
                "turns": len(ordered),
            }
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "runs": [
                {
                    "case": r.case_id,
                    "effort": r.effort,
                    "repeat": r.repeat,
                    "ok": r.ok,
                    "problems": r.problems,
                    "cost_usd": round(r.cost_usd, 5),
                    "judge": r.judge,
                }
                for r in self.runs
            ],
            "latency": self.percentiles(),
            "total_usd": round(self.total_usd, 4),
            "budget_usd": self.budget_usd,
            "stopped_by_budget": self.stopped_by_budget,
            "cache_read_ok": self.cache_read_ok,
            "history_ok": self.history_ok,
        }


def usage_cost_usd(usages: list[Usage], model: str) -> float:
    price = default_model_price(model)
    if price is None:
        return 0.0
    total = 0.0
    for usage in usages:
        total += usage.input_tokens * price.input_usd_per_mtok / 1_000_000
        total += usage.output_tokens * price.output_usd_per_mtok / 1_000_000
        total += usage.cache_write_tokens * price.cache_write_usd_per_mtok / 1_000_000
        total += usage.cache_read_tokens * price.cache_read_usd_per_mtok / 1_000_000
    return total


def require_key(settings: Settings) -> None:
    if not settings.anthropic_api_key.get_secret_value():
        raise LiveEvalError(
            "ANTHROPIC_API_KEY is not set: the live evaluation needs a workspace key with a "
            "spend limit and GC_EVAL_BUDGET_USD (default 5); nothing was called"
        )


async def _judge(llm: LLMPort, model: str, guest: str, reply: str) -> dict[str, Any]:
    request = LLMRequest(
        model=model,
        system=[TextBlock(JUDGE_RUBRIC)],
        messages=[Message("user", [TextPart(f"Guest: {guest}\nAssistant: {reply}")])],
        effort="low",
        max_tokens=200,
        cache_tail=False,
    )
    response = await llm.complete(request)
    text = "".join(p.text for p in response.content if isinstance(p, TextPart))
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"score": None, "notes": text[:200], "advisory": True}
    return {**parsed, "advisory": True}


async def run_live(
    settings: Settings,
    *,
    cases: list[GoldenCase],
    work_dir: Path,
    repeats: int = 3,
    budget_usd: float | None = None,
    llm: LLMPort | None = None,
    judge: bool = False,
    efforts: tuple[str, ...] = ("low", "medium"),
) -> LiveReport:
    """Run the cases live; ``llm`` is injected by tests, otherwise the configured adapter."""
    if llm is None:
        require_key(settings)
        from concierge.runtime import build_llm

        llm = build_llm(settings)
        if llm is None:
            raise LiveEvalError("GC_LLM_PROVIDER is not 'anthropic'; set it and the key")
    model = settings.gc_llm_model_primary
    report = LiveReport(
        budget_usd=settings.gc_eval_budget_usd if budget_usd is None else budget_usd
    )
    index = 0
    first_turn_usage: dict[str, list[Usage]] = {}
    for case in cases:
        for effort in efforts:
            for repeat in range(1, repeats + 1):
                if report.total_usd >= report.budget_usd:
                    report.stopped_by_budget = True
                    return report
                index += 1
                stack = await GoldenStack.create(work_dir / f"live-{index}.db", llm=llm)
                stack.services.brain.models.effort_override = effort  # type: ignore[assignment]
                run = LiveRun(case.id, case.language, effort, repeat, True, [], [], 0.0)
                try:
                    await stack.apply_setup(case.setup)
                    for turn_index, turn in enumerate(case.turns):
                        started = time.perf_counter()
                        turn_report = await stack.run_turn(turn, case.language, live=True)
                        run.latencies_ms.append((time.perf_counter() - started) * 1000)
                        run.problems.extend(turn_report.problems)
                        usages = _usages(turn_report)
                        run.cost_usd += usage_cost_usd(usages, model)
                        if turn_index == 0 and effort == efforts[0]:
                            first_turn_usage.setdefault(case.id, []).extend(usages)
                        if judge and turn_report.texts:
                            run.judge = await _judge(llm, model, turn.guest, turn_report.texts[-1])
                except Exception as exc:  # the report carries the failure
                    run.problems.append(f"{type(exc).__name__}: {exc}")
                finally:
                    await stack.close()
                run.ok = not run.problems
                report.total_usd += run.cost_usd
                report.runs.append(run)
    cache_reads = [u.cache_read_tokens for usages in first_turn_usage.values() for u in usages[1:]]
    report.cache_read_ok = any(c > 0 for c in cache_reads) if cache_reads else None
    if report.total_usd < report.budget_usd:
        report.history_ok = await _history_check(work_dir / "live-history.db", llm, report, model)
    return report


def _usages(turn: TurnReport) -> list[Usage]:
    return list(turn.usages)


async def _history_check(path: Path, llm: LLMPort, report: LiveReport, model: str) -> bool:
    """Three turns in one conversation: replayed text-only history must stay accepted."""
    stack = await GoldenStack.create(path, llm=llm)
    try:
        modes = []
        for text in HISTORY_TURNS:
            turn = GoldenTurn(guest=text, model=[], expect={})
            result = await stack.run_turn(turn, "de", live=True)
            modes.append(result.mode)
            report.total_usd += usage_cost_usd(_usages(result), model)
        return all(m == "model" for m in modes)
    finally:
        await stack.close()
