"""Golden evaluation (F35, 8.6): YAML cases run through the Brain with the scripted model.

Implements: F35, F01, F03, F11, F17

A case is one conversation against the seeded demo tenant: guest turns, scripted model
steps and expectations on tool calls and arguments, the handoff flag and priority,
events, forbidden and required substrings, the canary and the number of model calls.
Wording is never asserted beyond the required facts. Appendix E scenarios carry six
language variants; cross-cutting cases are language-independent. ``concierge golden
run`` and ``tests/golden`` share this runner.

Case file (``cases/<scenario>.yaml``)::

    scenario: parking            # defaults to the file name
    group: appendix_e            # or cross_cutting
    feature: F11
    languages: [de, en, ru, uk, tr, it]   # default: all six
    setup:                       # optional
      chain: [beds24, manual, none]
      pms: {rooms: [{code: dz, per_night_cents: 13500, extras: {note: "..."}}]}
      ical: {units: {dz: 1}, feeds: {dz_1: "BEGIN:VCALENDAR..."}}
      kb: [{key: nearby.tip, lang: en, text: "..."}]
    turns:
      - guest: {de: "...", en: "..."}          # or one string for every language
        model:                                 # scripted steps, consumed in order
          - tools: [{name: lookup_kb, args: {key: parking}}]
          - reply: {intent: "A:parking", fact_refs: [parking], text: {de: "...", en: "..."}}
          - fail: {kind: overloaded}
          - raw: "not json"
        expect:
          mode: model                          # model, responder, static, degraded, silent
          llm_calls: 2
          tool_calls: [{name: lookup_kb, args: {key: parking}}]   # ordered; args: subset
          tool_errors: 1
          handoff: true
          priority: P2
          events: ["handoff:P1"]               # substrings matched against the events
          not_events: ["handoff:"]
          required: ["18"]                     # substrings of the outbound texts
          forbidden: ["4711"]
          tool_results_contain: ["live_pms"]
          tool_results_forbid: ["4711"]
          request_forbid: ["4111 1111"]        # never in any model request
          buttons: ["person"]                  # ids on the last outbound message
          suppressions: 1                      # suppression rows for the tenant

``{canary}`` in any text stands for the run's canary; the canary is always forbidden in
outbound text. A language map (keys are language codes) is resolved per language.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select

from concierge.availability.factory import AdapterFactory
from concierge.availability.fake import ScriptedPms, ScriptedRoom, StaticFetcher
from concierge.channels.whatsapp.fake import FakeGraph
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.conversations.service import Inbound
from concierge.db.migrate import upgrade
from concierge.db.models import Suppression, Tenant
from concierge.db.session import Database
from concierge.demo.seed import seed_demo
from concierge.llm.port import LLMPort, LLMRequest, TextPart, ToolResult, ToolUse, Usage
from concierge.llm.scripted import ScriptedLLM, Step
from concierge.notify.port import FakeEmail
from concierge.security.crypto import Keyring
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.tenants.secrets import SecretStore

LANGUAGES = ["de", "en", "ru", "uk", "tr", "it"]
CASES_DIR = Path(__file__).resolve().parent / "cases"
GUEST_KEY = "4915120009999"
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
APPENDIX_E = "appendix_e"
CROSS_CUTTING = "cross_cutting"
DEFAULT_HANDOFF = {"needed": False, "priority": "none", "reason": ""}
FEED_HOST = "https://calendar.example.invalid"


class CaseError(ValueError):
    """A case file is malformed."""


@dataclass
class GoldenTurn:
    guest: str
    model: list[dict[str, Any]]
    expect: dict[str, Any]
    button_id: str | None = None


@dataclass
class GoldenCase:
    id: str
    scenario: str
    group: str
    feature: str
    language: str
    description: str
    setup: dict[str, Any]
    turns: list[GoldenTurn]
    source: str


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    result: str = ""
    is_error: bool = False


@dataclass
class TurnReport:
    guest: str
    mode: str
    events: list[str]
    texts: list[str]
    tool_calls: list[ToolCall]
    llm_calls: int
    problems: list[str] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)


@dataclass
class CaseReport:
    case_id: str
    scenario: str
    language: str
    feature: str
    group: str
    turns: list[TurnReport] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and all(not t.problems for t in self.turns)

    @property
    def problems(self) -> list[str]:
        found = [self.error] if self.error else []
        for index, turn in enumerate(self.turns, start=1):
            found.extend(f"turn {index}: {p}" for p in turn.problems)
        return found

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case_id,
            "feature": self.feature,
            "group": self.group,
            "ok": self.ok,
            "seconds": round(self.seconds, 3),
            "problems": self.problems,
            "turns": [
                {
                    "mode": t.mode,
                    "events": t.events,
                    "llm_calls": t.llm_calls,
                    "tool_calls": [
                        {"name": c.name, "args": c.args, "error": c.is_error} for c in t.tool_calls
                    ],
                }
                for t in self.turns
            ],
        }


# ------------------------------------------------------------------ loading
def _is_language_map(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and set(value) <= set(LANGUAGES)


def _pick(value: Any, lang: str, *, field_name: str) -> Any:
    if _is_language_map(value):
        if lang not in value:
            raise CaseError(f"{field_name} has no {lang} variant")
        return value[lang]
    if isinstance(value, dict):
        return {k: _pick(v, lang, field_name=f"{field_name}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_pick(v, lang, field_name=f"{field_name}[]") for v in value]
    return value


def load_cases(directory: Path = CASES_DIR) -> list[GoldenCase]:
    """Every case of every file, expanded per language, in file order."""
    cases: list[GoldenCase] = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("turns"), list):
            raise CaseError(f"{path.name}: a case file needs a 'turns' list")
        scenario = str(data.get("scenario") or path.stem)
        group = str(data.get("group") or APPENDIX_E)
        feature = str(data.get("feature") or "F35")
        languages = [str(lang) for lang in data.get("languages", LANGUAGES)]
        for lang in languages:
            if lang not in LANGUAGES:
                raise CaseError(f"{path.name}: unknown language {lang!r}")
            turns = []
            for index, raw in enumerate(data["turns"], start=1):
                if "guest" not in raw:
                    raise CaseError(f"{path.name}: turn {index} has no guest text")
                turns.append(
                    GoldenTurn(
                        guest=str(_pick(raw["guest"], lang, field_name=f"turn {index} guest")),
                        model=_pick(raw.get("model", []), lang, field_name=f"turn {index} model"),
                        expect=_pick(
                            raw.get("expect", {}), lang, field_name=f"turn {index} expect"
                        ),
                        button_id=raw.get("button_id"),
                    )
                )
            cases.append(
                GoldenCase(
                    id=f"{scenario}:{lang}",
                    scenario=scenario,
                    group=group,
                    feature=feature,
                    language=lang,
                    description=str(data.get("description", "")),
                    setup=_pick(data.get("setup", {}), lang, field_name="setup"),
                    turns=turns,
                    source=path.name,
                )
            )
    return cases


# ------------------------------------------------------------------ tracing
def tool_trace(requests: list[LLMRequest]) -> list[ToolCall]:
    """Tool uses and their results as the model saw them, in call order."""
    calls: dict[str, ToolCall] = {}
    order: list[str] = []
    for request in requests:
        for message in request.messages:
            for part in message.content:
                if isinstance(part, ToolUse) and part.id not in calls:
                    calls[part.id] = ToolCall(part.name, dict(part.input))
                    order.append(part.id)
                elif isinstance(part, ToolResult) and part.tool_use_id in calls:
                    calls[part.tool_use_id].result = part.content
                    calls[part.tool_use_id].is_error = part.is_error
    return [calls[i] for i in order]


def request_text(request: LLMRequest) -> str:
    """Everything the model was shown in one request, for 'never reaches the model' checks."""
    pieces = [block.text for block in request.system]
    for message in request.messages:
        for part in message.content:
            if isinstance(part, TextPart):
                pieces.append(part.text)
            elif isinstance(part, ToolUse):
                pieces.append(json.dumps(part.input, ensure_ascii=False))
            elif isinstance(part, ToolResult):
                pieces.append(part.content)
    return "\n".join(pieces)


# ------------------------------------------------------------------ the stack
class GoldenStack:
    """A fresh demo tenant on a temporary SQLite file with the scripted model."""

    def __init__(
        self,
        services: Services,
        tenant: Tenant,
        llm: ScriptedLLM | None,
        adapters: AdapterFactory,
        clock: FakeClock,
    ) -> None:
        self.services = services
        self.tenant = tenant
        self.llm = llm
        self.adapters = adapters
        self.clock = clock
        self.canary = services.brain.prompts.canary
        self.guest_key = GUEST_KEY

    @classmethod
    async def create(
        cls,
        database_path: Path,
        *,
        llm: LLMPort | None = None,
        now: datetime = NOW,
        responder_only: bool = False,
    ) -> GoldenStack:
        """``responder_only`` runs the deterministic responder (demo mode) without a model."""
        key = os.urandom(32)
        encoded = base64.b64encode(key).decode()
        settings = Settings(
            _env_file=None,
            gc_env="development",
            gc_database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
            gc_master_keys=json.dumps({"k1": encoded}),
            gc_active_key_id="k1",
            gc_signing_key=secrets.token_urlsafe(36),
            gc_backup_key=encoded,
            gc_metrics_token=secrets.token_urlsafe(16),
            gc_operator_email="operator@example.invalid",
            gc_public_base_url="http://127.0.0.1:8000",
        )
        db = Database(settings.database_url())
        await upgrade(db.engine)
        keyring = Keyring({"k1": key}, "k1")
        clock = FakeClock(now)
        ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
        scripted = ScriptedLLM([]) if llm is None and not responder_only else None
        adapters = AdapterFactory(db, clock, SecretStore(db, keyring))
        services = build_services(
            ctx,
            llm=llm if llm is not None else scripted,
            wa_port=FakeGraph(),
            demo=responder_only,
            adapters=adapters,
            email=FakeEmail(),
        )
        await seed_demo(services)
        tenant = await services.tenants.require("hotel-beispiel")
        return cls(services, tenant, scripted, adapters, clock)

    async def close(self) -> None:
        await self.services.ctx.db.dispose()

    def sub(self, text: str) -> str:
        return text.replace("{canary}", self.canary)

    async def apply_setup(self, setup: dict[str, Any]) -> None:
        tenants = self.services.tenants
        profile = tenants.profile_of(self.tenant)
        update: dict[str, Any] = {}
        if "chain" in setup:
            update["chain"] = list(setup["chain"])
        if "ical" in setup:
            spec = setup["ical"]
            update["ical_units"] = dict(spec.get("units", {"dz": 1}))
            pages: dict[str, str] = {}
            for unit, feed in spec.get("feeds", {}).items():
                url = f"{FEED_HOST}/{unit}.ics"
                await self.services.secrets.set(
                    self.tenant.id, f"ical_url_{unit}", url, actor="golden"
                )
                pages[url] = self.sub(str(feed))
            self.adapters.fetcher = StaticFetcher(pages)
        if update:
            profile = profile.model_copy(
                update={"availability": profile.availability.model_copy(update=update)}
            )
            self.tenant = await tenants.update_profile(self.tenant.slug, profile, actor="golden")
        if "pms" in setup:
            spec = setup["pms"]
            rooms = [
                ScriptedRoom(
                    str(room["code"]),
                    dict(room.get("names", {"de": "Doppelzimmer", "en": "Double room"})),
                    int(room["per_night_cents"]),
                    units_left=int(room.get("units_left", 3)),
                    max_occupancy=int(room.get("max_occupancy", 2)),
                    extras={k: self.sub(str(v)) for k, v in room.get("extras", {}).items()},
                )
                for room in spec.get("rooms", [])
            ]
            self.adapters.overrides[str(spec.get("source", "beds24"))] = ScriptedPms(
                self.clock, rooms
            )
        for entry in setup.get("kb", []):
            await self.services.kb.upsert(
                self.tenant.id,
                str(entry["key"]),
                str(entry.get("lang", "en")),
                text=self.sub(str(entry["text"])),
                status="approved",
                approver="golden",
                actor="golden",
            )

    def step(self, spec: dict[str, Any], lang: str) -> Step:
        if "tools" in spec:
            return Step.tools(*[(str(t["name"]), dict(t.get("args", {}))) for t in spec["tools"]])
        if "reply" in spec:
            reply = spec["reply"]
            payload = {
                "reply_text": self.sub(str(reply["text"])),
                "language": reply.get("language", lang),
                "intent": reply["intent"],
                "handoff": reply.get("handoff", DEFAULT_HANDOFF),
                "fact_refs": list(reply.get("fact_refs", [])),
                "confidence": float(reply.get("confidence", 0.9)),
                "buttons": list(reply.get("buttons", [])),
            }
            return Step.final(payload, stop_reason=reply.get("stop_reason"))
        if "fail" in spec:
            failure = spec["fail"]
            return Step.fail(str(failure["kind"]), str(failure.get("message", "scripted failure")))
        if "raw" in spec:
            return Step.final(str(spec["raw"]), stop_reason=spec.get("stop_reason"))
        if "hang" in spec:
            return Step.hang(float(spec["hang"]))
        raise CaseError(f"unknown model step {sorted(spec)}")

    async def run_turn(self, turn: GoldenTurn, lang: str, *, live: bool = False) -> TurnReport:
        if self.llm is not None:
            self.llm.load([self.step(s, lang) for s in turn.model])
        before = len(self.llm.requests) if self.llm is not None else 0
        result = await self.services.brain.respond(
            self.tenant,
            Inbound(
                channel="whatsapp",
                channel_key=self.guest_key,
                text=self.sub(turn.guest),
                kind="text",
                external_id=None,
                phone=f"+{self.guest_key}",
                button_id=turn.button_id,
            ),
        )
        requests = self.llm.requests[before:] if self.llm is not None else []
        report = TurnReport(
            guest=turn.guest,
            mode=result.mode,
            events=list(result.events),
            texts=[m.text for m in result.messages],
            tool_calls=tool_trace(requests),
            llm_calls=len(requests),
            usages=list(result.usage),
        )
        buttons = [b.id for b in result.messages[-1].buttons] if result.messages else []
        report.problems = await self.check(turn.expect, report, requests, buttons, live=live)
        return report

    async def check(
        self,
        expect: dict[str, Any],
        report: TurnReport,
        requests: list[LLMRequest],
        buttons: list[str],
        *,
        live: bool,
    ) -> list[str]:
        problems: list[str] = []
        joined = "\n".join(report.texts)
        lowered = joined.lower()
        handoffs = [e for e in report.events if e.startswith("handoff:")]
        if self.canary in joined:
            problems.append("the canary appears in an outbound text")
        if "mode" in expect and not live and report.mode != expect["mode"]:
            problems.append(f"mode {report.mode!r}, expected {expect['mode']!r}")
        if "llm_calls" in expect and not live and report.llm_calls != int(expect["llm_calls"]):
            problems.append(f"{report.llm_calls} model calls, expected {expect['llm_calls']}")
        if "tool_calls" in expect:
            wanted = [(str(t["name"]), dict(t.get("args", {}))) for t in expect["tool_calls"]]
            actual = [(c.name, c.args) for c in report.tool_calls]
            names_ok = [n for n, _ in actual] == [n for n, _ in wanted]
            if names_ok:
                for (name, args), (_, got) in zip(wanted, actual, strict=True):
                    missing = {k: v for k, v in args.items() if got.get(k) != v}
                    if missing:
                        problems.append(f"tool {name} arguments {got} lack {missing}")
            elif not live:
                problems.append(
                    f"tool calls {[n for n, _ in actual]}, expected {[n for n, _ in wanted]}"
                )
            elif {n for n, _ in wanted} - {n for n, _ in actual}:
                problems.append(
                    f"tool calls {[n for n, _ in actual]} miss {[n for n, _ in wanted]}"
                )
        if "tool_errors" in expect and not live:
            errors = sum(1 for c in report.tool_calls if c.is_error)
            if errors != int(expect["tool_errors"]):
                problems.append(f"{errors} tool errors, expected {expect['tool_errors']}")
        if "handoff" in expect and bool(expect["handoff"]) != bool(handoffs):
            problems.append(f"handoff events {handoffs}, expected handoff={expect['handoff']}")
        if "priority" in expect:
            priorities = [h.split(":")[1] for h in handoffs]
            if str(expect["priority"]) not in priorities:
                problems.append(f"handoff priorities {priorities}, expected {expect['priority']}")
        for needle in expect.get("events", []):
            if not any(str(needle) in e for e in report.events):
                problems.append(f"no event matches {needle!r}: {report.events}")
        for needle in expect.get("not_events", []):
            if any(str(needle) in e for e in report.events):
                problems.append(f"an event matches forbidden {needle!r}: {report.events}")
        for needle in expect.get("required", []):
            if str(needle) not in joined:
                problems.append(f"required text {needle!r} missing")
        for needle in expect.get("forbidden", []):
            if str(needle).lower() in lowered:
                problems.append(f"forbidden text {needle!r} present")
        results = "\n".join(c.result for c in report.tool_calls)
        for needle in expect.get("tool_results_contain", []):
            if str(needle) not in results:
                problems.append(f"tool results lack {needle!r}")
        for needle in expect.get("tool_results_forbid", []):
            if str(needle) in results:
                problems.append(f"tool results contain forbidden {needle!r}")
        if expect.get("request_forbid"):
            shown = "\n".join(request_text(r) for r in requests)
            for needle in expect["request_forbid"]:
                if str(needle) in shown:
                    problems.append(f"{needle!r} reached the model")
        if "buttons" in expect and not live and buttons != [str(b) for b in expect["buttons"]]:
            problems.append(f"buttons {buttons}, expected {expect['buttons']}")
        if "suppressions" in expect:
            async with self.services.ctx.db.scope() as session:
                count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(Suppression)
                        .where(Suppression.tenant_id == self.tenant.id)
                    )
                    or 0
                )
            if count != int(expect["suppressions"]):
                problems.append(f"{count} suppression rows, expected {expect['suppressions']}")
        return problems


# ------------------------------------------------------------------ running
async def run_case(case: GoldenCase, database_path: Path) -> CaseReport:
    """One case on a fresh stack; the database file is created and left for the caller."""
    started = time.perf_counter()
    report = CaseReport(case.id, case.scenario, case.language, case.feature, case.group)
    stack = await GoldenStack.create(database_path)
    try:
        await stack.apply_setup(case.setup)
        for turn in case.turns:
            report.turns.append(await stack.run_turn(turn, case.language))
    except Exception as exc:  # a crash is a failed case, reported not raised
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        await stack.close()
    report.seconds = time.perf_counter() - started
    return report


async def run_cases(cases: list[GoldenCase], work_dir: Path) -> list[CaseReport]:
    reports = []
    for index, case in enumerate(cases):
        reports.append(await run_case(case, work_dir / f"golden-{index}.db"))
    return reports


def summary(reports: list[CaseReport]) -> dict[str, Any]:
    groups: dict[str, dict[str, int]] = {}
    for report in reports:
        bucket = groups.setdefault(report.group, {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(report.ok)
    return {
        "total": len(reports),
        "passed": sum(1 for r in reports if r.ok),
        "failed": sum(1 for r in reports if not r.ok),
        "groups": groups,
    }
