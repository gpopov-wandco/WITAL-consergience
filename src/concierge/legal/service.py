"""Legal text service (C10, F19, F21): drafts, approval with name and date, status.

Implements: C10, C12, F19, F21
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from concierge import audit
from concierge.clock import Clock
from concierge.db.models import LegalText
from concierge.db.session import Database
from concierge.tenants.profile import TenantProfile

DRAFTS_FILE = Path(__file__).resolve().parent / "drafts.yaml"
FORBIDDEN_CLAIMS = ("compliant", "legally safe", "rechtssicher", "rechtskonform", "dsgvo-konform")


class LegalError(ValueError):
    pass


@dataclass
class LegalDrafts:
    data: dict[str, Any]

    @property
    def keys(self) -> list[str]:
        return list(self.data["texts"])

    def text(self, key: str, lang: str) -> str | None:
        block = self.data["texts"].get(key) or {}
        value = block.get(lang)
        return str(value) if value else None


def load_drafts(path: Path = DRAFTS_FILE) -> LegalDrafts:
    drafts = LegalDrafts(yaml.safe_load(path.read_text(encoding="utf-8")))
    for key in drafts.keys:
        for lang, text in drafts.data["texts"][key].items():
            lowered = str(text).lower()
            for claim in FORBIDDEN_CLAIMS:
                if claim in lowered:
                    raise LegalError(f"draft {key}/{lang} claims {claim!r}")
    return drafts


def render(text: str, profile: TenantProfile) -> str:
    values = {
        "hotel": profile.name,
        "privacy_url": profile.contacts.privacy_url or "-",
        "imprint_url": profile.contacts.imprint_url or "-",
        "phone": profile.contacts.reception_phone or "-",
        "transcript_days": str(profile.voice.transcript_retention_days),
    }
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


class LegalService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock
        self._drafts: LegalDrafts | None = None

    def drafts(self) -> LegalDrafts:
        if self._drafts is None:
            self._drafts = load_drafts()
        return self._drafts

    async def install_drafts(self, tenant_id: str, profile: TenantProfile, *, actor: str) -> int:
        """Create the missing draft rows for the tenant's languages (never overwrites)."""
        drafts = self.drafts()
        created = 0
        async with self.db.tenant_scope(tenant_id) as session:
            existing = {
                (row.key, row.lang)
                for row in (
                    await session.execute(select(LegalText).where(LegalText.tenant_id == tenant_id))
                ).scalars()
            }
            for key in drafts.keys:
                for lang in profile.languages:
                    if (key, lang) in existing:
                        continue
                    text = drafts.text(key, lang) or drafts.text(key, "en")
                    if text is None:
                        continue
                    session.add(
                        LegalText(
                            tenant_id=tenant_id,
                            key=key,
                            lang=lang,
                            text=render(text, profile),
                            status="draft_unreviewed",
                        )
                    )
                    created += 1
            if created:
                audit.record(
                    session,
                    actor=actor,
                    action="legal.install_drafts",
                    tenant_id=tenant_id,
                    details={"created": created},
                )
            await session.commit()
        return created

    async def rows(self, tenant_id: str) -> list[LegalText]:
        async with self.db.tenant_scope(tenant_id) as session:
            return list(
                (
                    await session.execute(
                        select(LegalText)
                        .where(LegalText.tenant_id == tenant_id)
                        .order_by(LegalText.key, LegalText.lang)
                    )
                ).scalars()
            )

    async def approve(
        self, tenant_id: str, legal_id: str, *, approver_name: str, actor: str
    ) -> LegalText:
        name = approver_name.strip()
        if len(name) < 3:
            raise LegalError("the approver's full name is required")
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(LegalText, legal_id)
            if row is None or row.tenant_id != tenant_id:
                raise LegalError("unknown legal text")
            row.status = "approved"
            row.approved_by = name[:200]
            row.approved_at = self.clock.now()
            audit.record(
                session,
                actor=actor,
                action="legal.approve",
                target=f"{row.key}/{row.lang}",
                tenant_id=tenant_id,
                details={"approver": name[:200], "version": row.version},
            )
            await session.commit()
            await session.refresh(row)
            return row

    async def update_text(
        self, tenant_id: str, legal_id: str, text: str, *, actor: str
    ) -> LegalText:
        lowered = text.lower()
        for claim in FORBIDDEN_CLAIMS:
            if claim in lowered:
                raise LegalError(f"the text must not claim to be {claim!r}")
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(LegalText, legal_id)
            if row is None or row.tenant_id != tenant_id:
                raise LegalError("unknown legal text")
            row.text = text.strip()
            row.version += 1
            row.status = "draft_unreviewed"
            row.approved_by = None
            row.approved_at = None
            audit.record(
                session,
                actor=actor,
                action="legal.update",
                target=f"{row.key}/{row.lang}",
                tenant_id=tenant_id,
                details={"version": row.version},
            )
            await session.commit()
            await session.refresh(row)
            return row
