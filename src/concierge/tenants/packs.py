"""Client packs (F21): export, validate, diff and import a tenant's configuration.

Implements: F21, C2, C3, C5

A pack is a directory (or zip) with ``tenant.yaml``, ``kb/entries.yaml``,
``templates/overrides.yaml``, ``legal/texts.yaml``, ``journeys.yaml``,
``availability.yaml``, ``branding/branding.yaml`` and ``questionnaire_answers.yaml``.
Secrets are never part of a pack. Import is idempotent, dry-run by default, and writes
one audit row; validation errors name the field path.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from concierge import audit
from concierge.db.models import KbEntry, LegalText, Template, Tenant
from concierge.db.session import Database
from concierge.kb.blocks import validate_key
from concierge.kb.questionnaire import QuestionnaireService, answers_document, load_answers
from concierge.kb.store import KbService
from concierge.legal.service import LegalService
from concierge.templates.service import TemplateService
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantError, TenantService

PACK_FILES = (
    "tenant.yaml",
    "kb/entries.yaml",
    "templates/overrides.yaml",
    "legal/texts.yaml",
    "journeys.yaml",
    "availability.yaml",
    "branding/branding.yaml",
    "questionnaire_answers.yaml",
)
SECRET_MARKERS = ("secret", "token", "password", "api_key", "apikey")
# a leaf that names a quantity (``prompt_token_budget``) is a number, not a secret
QUANTITY_SUFFIXES = ("_budget", "_limit", "_count", "_cap", "_max", "_min", "_tokens")
# KB entries carry guest-facing access data (the Wi-Fi password is hotel content with
# ``verified_guest_only`` sensitivity), so only credential-style keys are refused there.
KB_SECRET_MARKERS = ("secret", "token", "api_key", "apikey")


@dataclass
class PackReport:
    errors: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _load(path: Path) -> Any:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def open_pack(source: Path) -> Path:
    """A directory for the pack; a zip is extracted into a temporary directory."""
    if source.is_dir():
        return source
    if source.suffix.lower() == ".zip" and source.is_file():
        target = Path(tempfile.mkdtemp(prefix="concierge-pack-"))
        with zipfile.ZipFile(source) as archive:
            for member in archive.namelist():
                member_path = Path(member)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"unsafe path in zip: {member}")
            archive.extractall(target)
        return target
    raise ValueError(f"{source} is neither a directory nor a zip file")


def validate_pack(directory: Path) -> list[str]:
    """Field-path errors; an empty list means the pack is well-formed."""
    errors: list[str] = []
    tenant_file = directory / "tenant.yaml"
    if not tenant_file.exists():
        return ["tenant.yaml: missing"]
    raw = _load(tenant_file)
    if not isinstance(raw, dict):
        return ["tenant.yaml: not a mapping"]
    for extra in ("journeys.yaml", "availability.yaml"):
        overlay = _load(directory / extra)
        if overlay is None:
            continue
        if not isinstance(overlay, dict):
            errors.append(f"{extra}: not a mapping")
            continue
        raw[extra.removesuffix(".yaml")] = overlay
    branding = _load(directory / "branding" / "branding.yaml")
    if isinstance(branding, dict):
        raw["branding"] = branding
    try:
        TenantProfile.model_validate(raw)
    except ValidationError as exc:
        for item in exc.errors():
            location = ".".join(str(p) for p in item.get("loc", ())) or "tenant"
            errors.append(f"tenant.yaml: {location}: {item.get('msg', 'invalid')}")
    errors.extend(_scan_secrets(directory))
    kb = _load(directory / "kb" / "entries.yaml")
    if kb is not None:
        if not isinstance(kb, dict) or not isinstance(kb.get("entries"), list):
            errors.append("kb/entries.yaml: expected a mapping with an `entries` list")
        else:
            for index, entry in enumerate(kb["entries"]):
                if not isinstance(entry, dict) or not entry.get("key"):
                    errors.append(f"kb/entries.yaml: entries[{index}].key: missing")
                    continue
                try:
                    validate_key(str(entry["key"]))
                except ValueError as exc:
                    errors.append(f"kb/entries.yaml: entries[{index}].key: {exc}")
                if not isinstance(entry.get("text"), dict):
                    errors.append(
                        f"kb/entries.yaml: entries[{index}].text: expected a mapping of language to text"
                    )
    legal = _load(directory / "legal" / "texts.yaml")
    if legal is not None and not isinstance(legal, dict):
        errors.append("legal/texts.yaml: expected a mapping key -> lang -> text")
    overrides = _load(directory / "templates" / "overrides.yaml")
    if overrides is not None and not isinstance(overrides, dict):
        errors.append("templates/overrides.yaml: expected a mapping name -> lang -> body")
    answers_file = directory / "questionnaire_answers.yaml"
    if answers_file.exists():
        _answers, answer_errors = load_answers(answers_file)
        errors.extend(f"questionnaire_answers.yaml: {error}" for error in answer_errors)
    return errors


def _scan_secrets(directory: Path) -> list[str]:
    errors: list[str] = []
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
            continue
        data = _load(path)
        relative = path.relative_to(directory).as_posix()
        markers = KB_SECRET_MARKERS if relative == "kb/entries.yaml" else SECRET_MARKERS
        for key_path in _keys(data):
            leaf = key_path.rsplit(".", 1)[-1].lower()
            if any(marker in leaf for marker in markers) and not leaf.endswith(
                ("_secret_name", "_names", *QUANTITY_SUFFIXES)
            ):
                errors.append(f"{relative}: {key_path}: secrets never travel in a pack")
    return errors


def _keys(data: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.append(path)
            out.extend(_keys(value, path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            out.extend(_keys(item, f"{prefix}[{index}]"))
    return out


class PackService:
    def __init__(
        self,
        db: Database,
        tenants: TenantService,
        kb: KbService,
        legal: LegalService,
        templates: TemplateService,
        questionnaire: QuestionnaireService | None = None,
    ) -> None:
        self.db = db
        self.tenants = tenants
        self.kb = kb
        self.legal = legal
        self.templates = templates
        self.questionnaire = questionnaire or QuestionnaireService(db)

    async def export(self, tenant: Tenant, directory: Path) -> list[str]:
        """Write the pack files; returns the relative paths written."""
        profile = TenantProfile.model_validate(tenant.profile)
        data = profile.model_dump(mode="json", exclude_none=False)
        journeys = data.pop("journeys")
        availability = data.pop("availability")
        branding = data.pop("branding")
        written: list[str] = []

        def write(rel: str, payload: Any) -> None:
            path = directory / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_dump(payload), encoding="utf-8")
            written.append(rel)

        write("tenant.yaml", data)
        write("journeys.yaml", journeys)
        write("availability.yaml", availability)
        write("branding/branding.yaml", branding)
        entries = await self.kb.entries(tenant.id)
        grouped: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if entry.status == "retired":
                continue
            block = grouped.setdefault(
                entry.key,
                {
                    "key": entry.key,
                    "value": entry.value,
                    "sensitivity": entry.sensitivity,
                    "text": {},
                },
            )
            block["text"].setdefault(entry.lang, entry.text)  # newest version wins
            if entry.volatile:
                block["volatile"] = True
        write("kb/entries.yaml", {"tenant": tenant.slug, "entries": list(grouped.values())})
        legal_rows = await self.legal.rows(tenant.id)
        legal: dict[str, dict[str, str]] = {}
        for legal_row in legal_rows:
            legal.setdefault(legal_row.key, {})[legal_row.lang] = legal_row.text
        write("legal/texts.yaml", legal)
        overrides: dict[str, dict[str, str]] = {}
        for template_row in await self.templates.rows(tenant.id):
            standard = self.templates.definition(template_row.name, template_row.lang)
            if standard is None or standard.body != template_row.body:
                overrides.setdefault(template_row.name, {})[template_row.lang] = template_row.body
        write("templates/overrides.yaml", overrides)
        write(
            "questionnaire_answers.yaml",
            answers_document(tenant.slug, await self.questionnaire.answers(tenant.id)),
        )
        return written

    async def diff(self, tenant: Tenant | None, directory: Path) -> list[str]:
        """Human-readable changes an import would apply."""
        changes: list[str] = []
        raw = _merged_profile(directory)
        incoming = TenantProfile.model_validate(raw)
        if tenant is None:
            changes.append(f"create tenant {incoming.slug}")
        else:
            current = TenantProfile.model_validate(tenant.profile).model_dump(mode="json")
            for key, value in incoming.model_dump(mode="json").items():
                if current.get(key) != value:
                    changes.append(f"profile.{key}: changed")
        kb = _load(directory / "kb" / "entries.yaml")
        if isinstance(kb, dict) and tenant is not None:
            existing = _latest_texts(await self.kb.entries(tenant.id))
            for entry in kb.get("entries", []):
                for lang, text in (entry.get("text") or {}).items():
                    if existing.get((entry["key"], lang)) != text:
                        changes.append(f"kb {entry['key']}/{lang}: new draft version")
        elif isinstance(kb, dict):
            changes.append(f"kb: {len(kb.get('entries', []))} entries as drafts")
        legal = _load(directory / "legal" / "texts.yaml")
        if isinstance(legal, dict):
            if tenant is None:
                changes.append(f"legal: {sum(len(v or {}) for v in legal.values())} draft texts")
            else:
                existing_legal = {(r.key, r.lang): r.text for r in await self.legal.rows(tenant.id)}
                for key, languages in legal.items():
                    for lang, text in (languages or {}).items():
                        if existing_legal.get((key, lang)) != text:
                            changes.append(f"legal {key}/{lang}: new draft version")
        overrides = _load(directory / "templates" / "overrides.yaml")
        if isinstance(overrides, dict) and overrides:
            if tenant is None:
                count = sum(len(v or {}) for v in overrides.values())
                changes.append(f"templates: {count} body overrides")
            else:
                bodies = {(r.name, r.lang): r.body for r in await self.templates.rows(tenant.id)}
                for name, languages in overrides.items():
                    for lang, body in (languages or {}).items():
                        if bodies.get((name, lang)) != body:
                            changes.append(f"template {name}/{lang}: body override")
        answers_file = directory / "questionnaire_answers.yaml"
        if answers_file.exists():
            answers, _errors = load_answers(answers_file)
            if tenant is None:
                if answers:
                    changes.append(f"questionnaire: {len(answers)} answers")
            else:
                current = await self.questionnaire.answers(tenant.id)
                changed = sum(
                    1 for key in set(current) | set(answers) if current.get(key) != answers.get(key)
                )
                if changed:
                    changes.append(f"questionnaire: {changed} answers changed")
        return changes

    async def import_pack(self, directory: Path, *, dry_run: bool = True, actor: str) -> PackReport:
        report = PackReport()
        report.errors = validate_pack(directory)
        if report.errors:
            return report
        raw = _merged_profile(directory)
        incoming = TenantProfile.model_validate(raw)
        tenant = await self.tenants.get(incoming.slug)
        report.changes = await self.diff(tenant, directory)
        if dry_run:
            return report
        try:
            if tenant is None:
                tenant = await self.tenants.create(incoming, actor=actor)
            else:
                tenant = await self.tenants.update_profile(incoming.slug, incoming, actor=actor)
        except IntegrityError:
            report.errors.append(
                "tenant.yaml: a unique value (public key or phone number) is already used by "
                "another tenant"
            )
            return report
        except TenantError as exc:
            report.errors.append(f"tenant.yaml: {exc}")
            return report
        kb = _load(directory / "kb" / "entries.yaml")
        if isinstance(kb, dict):
            rows: list[dict[str, Any]] = []
            existing = _latest_texts(await self.kb.entries(tenant.id))
            for entry in kb.get("entries", []):
                for lang, text in (entry.get("text") or {}).items():
                    if existing.get((entry["key"], lang)) == text:
                        continue
                    rows.append(
                        {
                            "key": entry["key"],
                            "lang": lang,
                            "text": text,
                            "value": entry.get("value") or {},
                            "sensitivity": entry.get("sensitivity", "public"),
                            "volatile": bool(entry.get("volatile", False)),
                            "source": "pack",
                        }
                    )
            if rows:
                await self.kb.bulk_upsert(tenant.id, rows, actor=actor)
        answers_file = directory / "questionnaire_answers.yaml"
        if answers_file.exists():
            answers, _errors = load_answers(answers_file)
            if answers != await self.questionnaire.answers(tenant.id):
                await self.questionnaire.save(tenant.id, answers, actor=actor, replace=True)
        legal = _load(directory / "legal" / "texts.yaml")
        async with self.db.tenant_scope(tenant.id) as session:
            if isinstance(legal, dict):
                for key, languages in legal.items():
                    for lang, text in (languages or {}).items():
                        legal_row = await session.scalar(
                            select(LegalText).where(
                                LegalText.tenant_id == tenant.id,
                                LegalText.key == key,
                                LegalText.lang == lang,
                            )
                        )
                        if legal_row is None:
                            session.add(
                                LegalText(tenant_id=tenant.id, key=key, lang=lang, text=str(text))
                            )
                        elif legal_row.text != text:
                            legal_row.text = str(text)
                            legal_row.version += 1
                            legal_row.status = "draft_unreviewed"
                            legal_row.approved_by = None
                            legal_row.approved_at = None
            overrides = _load(directory / "templates" / "overrides.yaml")
            if isinstance(overrides, dict):
                for name, languages in overrides.items():
                    for lang, body in (languages or {}).items():
                        template_row = await session.scalar(
                            select(Template).where(
                                Template.tenant_id == tenant.id,
                                Template.name == name,
                                Template.lang == lang,
                            )
                        )
                        if template_row is not None and template_row.body != body:
                            template_row.body = str(body)
                            template_row.status = "draft"
            audit.record(
                session,
                actor=actor,
                action="pack.import",
                target=incoming.slug,
                tenant_id=tenant.id,
                details={"changes": report.changes[:50]},
            )
            await session.commit()
        report.applied = True
        return report


def _latest_texts(entries: list[KbEntry]) -> dict[tuple[str, str], str]:
    """Text of the newest non-retired version per key and language."""
    latest: dict[tuple[str, str], str] = {}
    for entry in entries:  # ordered by key, lang, version descending
        if entry.status != "retired" and (entry.key, entry.lang) not in latest:
            latest[(entry.key, entry.lang)] = entry.text
    return latest


def _merged_profile(directory: Path) -> dict[str, Any]:
    raw = _load(directory / "tenant.yaml")
    if not isinstance(raw, dict):
        raise ValueError("tenant.yaml is not a mapping")
    for extra in ("journeys", "availability"):
        overlay = _load(directory / f"{extra}.yaml")
        if isinstance(overlay, dict):
            raw[extra] = overlay
    branding = _load(directory / "branding" / "branding.yaml")
    if isinstance(branding, dict):
        raw["branding"] = branding
    return raw


def kb_entries_count(directory: Path) -> int:
    kb = _load(directory / "kb" / "entries.yaml")
    return len(kb.get("entries", [])) if isinstance(kb, dict) else 0


def pack_to_zip(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    return buffer.getvalue()
