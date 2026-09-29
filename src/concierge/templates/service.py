"""WhatsApp template registry (F14): standard set, lint, Meta sync, approval gate.

Implements: F14, F13, C7, C10, C12

Templates live per tenant in ``template`` with their Meta status. The standard set in
``standard.yaml`` is loaded into a tenant's registry; ``lint`` enforces Meta's limits
and the house rules (no promotional cues in utility templates, the first template of a
journey carries the AI disclosure, one topic per message). ``sync`` creates the missing
templates at Meta (100 per hour) and ``approved`` is the only way a sender obtains a
template: anything not APPROVED in that language is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from concierge import audit
from concierge.channels.whatsapp.formatting import BUTTON_PREFIX, language_code, template_payload
from concierge.channels.whatsapp.port import TemplateRecord, WhatsAppPort
from concierge.clock import Clock
from concierge.db.models import Template
from concierge.db.session import Database
from concierge.log import get_logger
from concierge.security.limiter import MovingWindowLimiter, RateLimiter
from concierge.tenants.secrets import SecretStore

log = get_logger(__name__)

STANDARD_FILE = Path(__file__).resolve().parent / "standard.yaml"
STANDARD_NAMES = (
    "gc_booking_followup",
    "gc_prearrival_7d",
    "gc_prearrival_2d",
    "gc_arrival_day",
    "gc_key_safe",
    "gc_stay_check",
    "gc_checkout_reminder",
    "gc_review_request",
    "gc_registration_link",
    "gc_staff_alert",
)
LANGS = ("de", "en", "ru", "uk", "tr", "it")
NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
VARIABLE_RE = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
POSITIONAL_RE = re.compile(r"\{\{\d+\}\}")
BODY_MAX = 1024
HEADER_MAX = 60
FOOTER_MAX = 60
BUTTON_MAX_META = 25
BUTTON_MAX_OWN = 20
QUICK_REPLIES_MAX = 3
CREATE_BUDGET = "100 per hour"
DISCLOSURE_MARKERS: dict[str, tuple[str, ...]] = {
    "de": ("KI-Assistent",),
    "en": ("AI assistant",),
    "ru": ("ИИ-ассистент",),
    "uk": ("ШІ-асистент",),
    "tr": ("yapay zekâ asistanı", "yapay zeka asistanı"),
    "it": ("assistente IA",),
}
PROMOTIONAL_CUES: dict[str, tuple[str, ...]] = {
    "de": (
        "angebot",
        "rabatt",
        "sparen sie",
        "gutschein",
        "aktion",
        "% ",
        "prozent",
        "nur heute",
        "jetzt buchen",
    ),
    "en": (
        "offer",
        "discount",
        "save ",
        "voucher",
        "sale",
        "deal",
        "% ",
        "percent",
        "only today",
        "book now",
    ),
    "ru": ("скидк", "акци", "предложени", "ваучер", "только сегодня", "% "),
    "uk": ("знижк", "акці", "пропозиці", "ваучер", "лише сьогодні", "% "),
    "tr": ("indirim", "kampanya", "teklif", "kupon", "sadece bugün", "% "),
    "it": ("sconto", "offerta", "promozion", "buono", "solo oggi", "% "),
}
STOP_MARKER = "STOP"


@dataclass(frozen=True)
class TemplateDefinition:
    name: str
    lang: str
    category: str
    body: str
    variables: tuple[str, ...]
    samples: dict[str, str]
    buttons: tuple[tuple[str, str], ...] = ()
    """(button id, label) quick replies."""
    header: str | None = None
    footer: str | None = None
    first_contact: bool = False

    @property
    def language_code(self) -> str:
        return language_code(self.lang)


@dataclass
class LintIssue:
    name: str
    lang: str
    rule: str
    detail: str


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    skipped_budget: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class TemplateRefused(RuntimeError):
    def __init__(self, name: str, lang: str, reason: str) -> None:
        super().__init__(f"template {name}/{lang} refused: {reason}")
        self.name = name
        self.lang = lang
        self.reason = reason


def load_standard(path: Path = STANDARD_FILE) -> list[TemplateDefinition]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: list[TemplateDefinition] = []
    for name, spec in data["templates"].items():
        buttons_spec = spec.get("buttons") or {}
        ids = tuple(buttons_spec.get("ids", ()))
        for lang in LANGS:
            labels = tuple(buttons_spec.get(lang, ())) if ids else ()
            out.append(
                TemplateDefinition(
                    name=str(name),
                    lang=lang,
                    category=str(spec["category"]),
                    body=str(spec["body"][lang]),
                    variables=tuple(spec.get("variables", ())),
                    samples={str(k): str(v) for k, v in (spec.get("samples") or {}).items()},
                    buttons=tuple(zip(ids, labels, strict=True)),
                    first_contact=bool(spec.get("first_contact", False)),
                )
            )
    return out


def lint(definition: TemplateDefinition, *, own_buttons: bool = True) -> list[LintIssue]:
    issues: list[LintIssue] = []

    def issue(rule: str, detail: str) -> None:
        issues.append(LintIssue(definition.name, definition.lang, rule, detail))

    if not NAME_RE.match(definition.name):
        issue("name", "lowercase letters, digits and underscores only")
    if definition.category not in {"utility", "marketing", "authentication"}:
        issue("category", f"unknown category {definition.category!r}")
    if len(definition.body) > BODY_MAX:
        issue("body_length", f"{len(definition.body)} > {BODY_MAX}")
    if not definition.body.strip():
        issue("body_empty", "empty body")
    if definition.header and len(definition.header) > HEADER_MAX:
        issue("header_length", f"{len(definition.header)} > {HEADER_MAX}")
    if definition.footer and len(definition.footer) > FOOTER_MAX:
        issue("footer_length", f"{len(definition.footer)} > {FOOTER_MAX}")
    if len(definition.buttons) > QUICK_REPLIES_MAX:
        issue("buttons", f"{len(definition.buttons)} quick replies > {QUICK_REPLIES_MAX}")
    limit = BUTTON_MAX_OWN if own_buttons else BUTTON_MAX_META
    for button_id, label in definition.buttons:
        if len(label) > limit:
            issue("button_length", f"{label!r} longer than {limit}")
        if not re.fullmatch(r"[a-z0-9_]{1,40}", button_id):
            issue("button_id", f"{button_id!r} is not a valid id")
    used = set(VARIABLE_RE.findall(definition.body))
    if POSITIONAL_RE.search(definition.body):
        issue("variables", "positional {{1}} variables are not allowed; use names")
    declared = set(definition.variables)
    if used != declared:
        issue("variables", f"body uses {sorted(used)} but declares {sorted(declared)}")
    missing = [v for v in definition.variables if not definition.samples.get(v)]
    if missing:
        issue("samples", f"no sample for {missing}")
    lowered = definition.body.lower()
    if definition.category == "utility":
        cues = [c for c in PROMOTIONAL_CUES.get(definition.lang, ()) if c in lowered]
        if cues:
            issue("promotional", f"utility template contains {cues}")
    if definition.first_contact and not any(
        marker.lower() in lowered for marker in DISCLOSURE_MARKERS.get(definition.lang, ())
    ):
        issue("disclosure", "first-contact template lacks the AI disclosure")
    if definition.name != "gc_staff_alert" and STOP_MARKER not in definition.body:
        issue("stop", "business-initiated template lacks the STOP instruction")
    if definition.name == "gc_staff_alert" and STOP_MARKER not in definition.body:
        issue("stop", "staff alert lacks the STOP instruction")
    return issues


def lint_all(definitions: list[TemplateDefinition]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for definition in definitions:
        issues.extend(lint(definition))
    return issues


def render(definition: TemplateDefinition, values: dict[str, str]) -> str:
    """The text a guest sees (for previews and the stored message copy)."""
    text = definition.body
    for name in definition.variables:
        text = text.replace("{{" + name + "}}", values.get(name, ""))
    return text


def meta_components(definition: TemplateDefinition) -> list[dict[str, Any]]:
    """The creation payload components (named parameters with samples; VERIFY format)."""
    body: dict[str, Any] = {"type": "BODY", "text": definition.body}
    if definition.variables:
        body["example"] = {
            "body_text_named_params": [
                {"param_name": v, "example": definition.samples.get(v, "")}
                for v in definition.variables
            ]
        }
    components: list[dict[str, Any]] = [body]
    if definition.buttons:
        components.append(
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": label} for _, label in definition.buttons
                ],
            }
        )
    return components


def send_components(definition: TemplateDefinition, values: dict[str, str]) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    if definition.variables:
        components.append(
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "parameter_name": v, "text": values.get(v, "")}
                    for v in definition.variables
                ],
            }
        )
    for index, (button_id, _label) in enumerate(definition.buttons):
        components.append(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(index),
                "parameters": [{"type": "payload", "payload": f"{BUTTON_PREFIX}{button_id}"}],
            }
        )
    return components


class TemplateService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        port: WhatsAppPort,
        secrets: SecretStore,
        *,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.port = port
        self.secrets = secrets
        self.limiter = limiter or MovingWindowLimiter()
        self._standard: list[TemplateDefinition] | None = None

    def standard(self) -> list[TemplateDefinition]:
        if self._standard is None:
            self._standard = load_standard()
        return self._standard

    def definition(self, name: str, lang: str) -> TemplateDefinition | None:
        return next((d for d in self.standard() if d.name == name and d.lang == lang), None)

    async def install_standard(self, tenant_id: str, *, actor: str) -> int:
        """Load the standard set into the registry as drafts (idempotent, lint first)."""
        issues = lint_all(self.standard())
        if issues:
            raise ValueError(
                f"standard templates fail lint: {issues[0].rule} {issues[0].name}/{issues[0].lang}"
            )
        created = 0
        async with self.db.tenant_scope(tenant_id) as session:
            existing = {
                (row.name, row.lang): row
                for row in (
                    await session.execute(select(Template).where(Template.tenant_id == tenant_id))
                ).scalars()
            }
            for definition in self.standard():
                row = existing.get((definition.name, definition.lang))
                if row is None:
                    row = Template(
                        tenant_id=tenant_id,
                        name=definition.name,
                        lang=definition.lang,
                        category=definition.category,
                        status="draft",
                    )
                    session.add(row)
                    created += 1
                row.body = definition.body
                row.buttons = [{"id": i, "label": label} for i, label in definition.buttons]
                row.variables = list(definition.variables)
                row.samples = dict(definition.samples)
                row.category = definition.category
            if created:
                audit.record(
                    session,
                    actor=actor,
                    action="templates.install",
                    tenant_id=tenant_id,
                    details={"created": created},
                )
            await session.commit()
        return created

    async def rows(self, tenant_id: str) -> list[Template]:
        async with self.db.tenant_scope(tenant_id) as session:
            return list(
                (
                    await session.execute(
                        select(Template)
                        .where(Template.tenant_id == tenant_id)
                        .order_by(Template.name, Template.lang)
                    )
                ).scalars()
            )

    async def approved(self, tenant_id: str, name: str, lang: str) -> Template | None:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(Template).where(
                    Template.tenant_id == tenant_id, Template.name == name, Template.lang == lang
                )
            )
        if row is None or row.status != "APPROVED":
            return None
        return row

    async def set_status(
        self, tenant_id: str, name: str, lang: str, status: str, *, reason: str | None = None
    ) -> Template:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(Template).where(
                    Template.tenant_id == tenant_id, Template.name == name, Template.lang == lang
                )
            )
            if row is None:
                raise ValueError(f"unknown template {name}/{lang}")
            row.status = status
            row.status_reason = reason
            row.last_status_at = self.clock.now()
            await session.commit()
            await session.refresh(row)
            return row

    async def sync(self, tenant_id: str, waba_id: str, *, actor: str) -> SyncReport:
        """Create missing templates at Meta and record ids and statuses of existing ones."""
        report = SyncReport()
        token = await self.secrets.get(tenant_id, "wa_access_token")
        if not token:
            report.errors.append("wa_access_token missing")
            return report
        remote = {
            (r.name, r.language): r
            for r in await self.port.list_templates(waba_id, access_token=token)
        }
        async with self.db.tenant_scope(tenant_id) as session:
            rows = list(
                (
                    await session.execute(select(Template).where(Template.tenant_id == tenant_id))
                ).scalars()
            )
            for row in rows:
                definition = self.definition(row.name, row.lang) or _definition_from_row(row)
                code = definition.language_code
                found = remote.get((row.name, code))
                if found is not None:
                    _apply_record(row, found, self.clock)
                    report.existing.append(f"{row.name}/{row.lang}")
                    continue
                if not self.limiter.hit(f"tpl:{tenant_id}", CREATE_BUDGET):
                    report.skipped_budget.append(f"{row.name}/{row.lang}")
                    continue
                payload = {
                    "name": row.name,
                    "language": code,
                    "category": row.category.upper(),
                    "components": meta_components(definition),
                }
                try:
                    record = await self.port.create_template(waba_id, payload, access_token=token)
                except Exception as exc:  # the port raises GraphError subclasses
                    report.errors.append(f"{row.name}/{row.lang}: {type(exc).__name__}")
                    continue
                _apply_record(row, record, self.clock)
                report.created.append(f"{row.name}/{row.lang}")
            audit.record(
                session,
                actor=actor,
                action="templates.sync",
                tenant_id=tenant_id,
                details={
                    "created": len(report.created),
                    "existing": len(report.existing),
                    "skipped_budget": len(report.skipped_budget),
                    "errors": report.errors[:10],
                },
            )
            await session.commit()
        return report

    async def require_approved(self, tenant_id: str, name: str, lang: str) -> TemplateDefinition:
        row = await self.approved(tenant_id, name, lang)
        if row is None:
            raise TemplateRefused(name, lang, "not APPROVED in this language")
        return self.definition(name, lang) or _definition_from_row(row)

    async def payload(
        self, tenant_id: str, to: str, name: str, lang: str, values: dict[str, str]
    ) -> tuple[dict[str, Any], str]:
        """The send payload and the rendered text for a template APPROVED in ``lang``."""
        definition = await self.require_approved(tenant_id, name, lang)
        payload = template_payload(
            to, definition.name, definition.language_code, send_components(definition, values)
        )
        return payload, render(definition, values)


def _apply_record(row: Template, record: TemplateRecord, clock: Clock) -> None:
    row.meta_template_id = record.id or row.meta_template_id
    new_status = (record.status or "PENDING").upper()
    if row.status != new_status:
        row.status = new_status
        row.last_status_at = clock.now()
    remote_category = (record.category or "").lower()
    if remote_category and remote_category != row.category:
        row.meta_category = remote_category
        row.status_reason = f"Meta category {remote_category} differs from {row.category}"


def _definition_from_row(row: Template) -> TemplateDefinition:
    buttons = tuple(
        (str(b.get("id", "")), str(b.get("label", "")))
        for b in (row.buttons or [])
        if isinstance(b, dict)
    )
    return TemplateDefinition(
        name=row.name,
        lang=row.lang,
        category=row.category,
        body=row.body,
        variables=tuple(str(v) for v in (row.variables or [])),
        samples={str(k): str(v) for k, v in (row.samples or {}).items()},
        buttons=buttons,
        header=row.header,
        footer=row.footer,
    )
