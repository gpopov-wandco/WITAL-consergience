"""Per-tenant knowledge base: lifecycle, resolver, snapshot, lookup, search, gaps.

Implements: F05, F06, C5, C6, 8.2

Only approved entries reach guests. The resolver uses the stay date against
``valid_from``/``valid_to``, ignores overdue volatile entries and returns one entry
per key (the latest approved version). The public snapshot is deterministic text in
stable key order; non-public entries never enter it and are only reachable through
``lookup`` after verification.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from concierge import audit
from concierge.clock import Clock, local_date
from concierge.db.models import KbEntry, KbGap
from concierge.db.session import Database
from concierge.kb.blocks import DEFAULT_REVIEW_DAYS, REQUIRED_KEYS, block_of, validate_key

STATUSES = ("draft", "draft_mt", "pending_approval", "approved", "retired")
TRUTHS = ("authoritative_pms", "authoritative_kb", "indicative")
SENSITIVITIES = ("public", "verified_guest_only", "staff_only")
_TOKEN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


class KbError(ValueError):
    pass


@dataclass(frozen=True)
class KbView:
    key: str
    lang: str
    text: str
    value: dict[str, Any]
    truth: str
    sensitivity: str
    version: int
    entry_id: str
    fallback_from: str | None = None


@dataclass
class KbSnapshot:
    lang: str
    entries: list[KbView]
    text: str
    digest: str
    excluded_overdue: list[str] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        return [e.key for e in self.entries]


class KbService:
    def __init__(self, db: Database, clock: Clock, *, tz: str = "Europe/Berlin") -> None:
        self.db = db
        self.clock = clock
        self.tz = tz

    def today(self) -> date:
        return local_date(self.clock.now(), self.tz)

    # ----------------------------------------------------------- lifecycle
    async def upsert(
        self,
        tenant_id: str,
        key: str,
        lang: str,
        *,
        text: str,
        value: dict[str, Any] | None = None,
        status: str = "draft",
        truth: str = "authoritative_kb",
        sensitivity: str | None = None,
        source: str = "",
        valid_from: date | None = None,
        valid_to: date | None = None,
        volatile: bool | None = None,
        review_days: int = DEFAULT_REVIEW_DAYS,
        approver: str | None = None,
        actor: str = "system",
    ) -> KbEntry:
        key = validate_key(key)
        spec = block_of(key)
        if status not in STATUSES:
            raise KbError(f"unknown status {status!r}")
        if truth not in TRUTHS:
            raise KbError(f"unknown truth {truth!r}")
        sensitivity = sensitivity or spec.sensitivity
        if sensitivity not in SENSITIVITIES:
            raise KbError(f"unknown sensitivity {sensitivity!r}")
        if status == "approved" and not approver:
            raise KbError("approved entries need an approver")
        if valid_from and valid_to and valid_to < valid_from:
            raise KbError("valid_to is before valid_from")
        async with self.db.tenant_scope(tenant_id) as session:
            latest = await session.scalar(
                select(KbEntry)
                .where(KbEntry.tenant_id == tenant_id, KbEntry.key == key, KbEntry.lang == lang)
                .order_by(KbEntry.version.desc())
            )
            version = (latest.version + 1) if latest else 1
            now = self.clock.now()
            entry = KbEntry(
                tenant_id=tenant_id,
                key=key,
                lang=lang,
                value=value or {},
                text=text.strip(),
                valid_from=valid_from,
                valid_to=valid_to,
                source=source,
                truth=truth,
                status=status,
                sensitivity=sensitivity,
                review_due=self.today() + timedelta(days=review_days),
                volatile=spec.volatile if volatile is None else volatile,
                version=version,
                approver=approver if status == "approved" else None,
                approved_at=now if status == "approved" else None,
            )
            session.add(entry)
            if status == "approved":
                await self._retire_previous(
                    session,
                    tenant_id,
                    key,
                    lang,
                    keep_version=version,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            audit.record(
                session,
                actor=actor,
                action="kb.upsert",
                target=f"{key}/{lang}",
                tenant_id=tenant_id,
                details={"status": status, "version": version},
            )
            await session.commit()
            await session.refresh(entry)
            return entry

    async def set_status(
        self, tenant_id: str, entry_id: str, status: str, *, actor: str, approver: str | None = None
    ) -> KbEntry:
        if status not in STATUSES:
            raise KbError(f"unknown status {status!r}")
        async with self.db.tenant_scope(tenant_id) as session:
            entry = await session.get(KbEntry, entry_id)
            if entry is None:
                raise KbError("entry not found")
            allowed = {
                "draft": {"pending_approval", "retired", "approved"},
                "draft_mt": {"pending_approval", "retired", "approved"},
                "pending_approval": {"approved", "draft", "retired"},
                "approved": {"retired"},
                "retired": set(),
            }
            if status not in allowed[entry.status]:
                raise KbError(f"cannot move {entry.status} to {status}")
            if status == "approved":
                if not approver:
                    raise KbError("approval needs the approver's name")
                entry.approver = approver
                entry.approved_at = self.clock.now()
                await self._retire_previous(
                    session,
                    tenant_id,
                    entry.key,
                    entry.lang,
                    keep_version=entry.version,
                    valid_from=entry.valid_from,
                    valid_to=entry.valid_to,
                )
            entry.status = status
            audit.record(
                session,
                actor=actor,
                action=f"kb.{status}",
                target=f"{entry.key}/{entry.lang}",
                tenant_id=tenant_id,
            )
            await session.commit()
            await session.refresh(entry)
            return entry

    async def bulk_upsert(
        self, tenant_id: str, rows: list[dict[str, Any]], *, actor: str = "import"
    ) -> int:
        """Insert many entries in one transaction (fixtures, client packs).

        Each row carries ``key``, ``lang``, ``text`` and optionally ``value``,
        ``status``, ``truth``, ``sensitivity``, ``source``, ``volatile``, ``approver``.
        """
        today = self.today()
        now = self.clock.now()
        written = 0
        async with self.db.tenant_scope(tenant_id) as session:
            existing = (
                await session.execute(
                    select(KbEntry.key, KbEntry.lang, KbEntry.version).where(
                        KbEntry.tenant_id == tenant_id
                    )
                )
            ).all()
            versions: dict[tuple[str, str], int] = {}
            for key, lang, version in existing:
                versions[(key, lang)] = max(versions.get((key, lang), 0), version)
            for row in rows:
                key = validate_key(str(row["key"]))
                lang = str(row["lang"])
                spec = block_of(key)
                status = str(row.get("status", "draft"))
                if status not in STATUSES:
                    raise KbError(f"unknown status {status!r}")
                approver = row.get("approver")
                if status == "approved" and not approver:
                    raise KbError("approved entries need an approver")
                version = versions.get((key, lang), 0) + 1
                versions[(key, lang)] = version
                volatile = row.get("volatile")
                entry = KbEntry(
                    tenant_id=tenant_id,
                    key=key,
                    lang=lang,
                    value=dict(row.get("value") or {}),
                    text=str(row["text"]).strip(),
                    valid_from=row.get("valid_from"),
                    valid_to=row.get("valid_to"),
                    source=str(row.get("source", "")),
                    truth=str(row.get("truth", "authoritative_kb")),
                    status=status,
                    sensitivity=str(row.get("sensitivity") or spec.sensitivity),
                    review_due=today
                    + timedelta(days=int(row.get("review_days", DEFAULT_REVIEW_DAYS))),
                    volatile=spec.volatile if volatile is None else bool(volatile),
                    version=version,
                    approver=approver if status == "approved" else None,
                    approved_at=now if status == "approved" else None,
                )
                session.add(entry)
                if status == "approved" and version > 1:
                    await self._retire_previous(
                        session,
                        tenant_id,
                        key,
                        lang,
                        keep_version=version,
                        valid_from=entry.valid_from,
                        valid_to=entry.valid_to,
                    )
                written += 1
            audit.record(
                session,
                actor=actor,
                action="kb.bulk_upsert",
                tenant_id=tenant_id,
                details={"rows": written},
            )
            await session.commit()
        return written

    @staticmethod
    async def _retire_previous(
        session: Any,
        tenant_id: str,
        key: str,
        lang: str,
        *,
        keep_version: int,
        valid_from: date | None,
        valid_to: date | None,
    ) -> None:
        """Retire older approved versions whose validity window overlaps the new one.

        Entries with disjoint windows (a summer and a winter breakfast time) coexist.
        """
        rows = (
            await session.execute(
                select(KbEntry).where(
                    KbEntry.tenant_id == tenant_id,
                    KbEntry.key == key,
                    KbEntry.lang == lang,
                    KbEntry.status == "approved",
                    KbEntry.version != keep_version,
                )
            )
        ).scalars()
        for row in rows:
            if _windows_overlap(row.valid_from, row.valid_to, valid_from, valid_to):
                row.status = "retired"

    async def entries(
        self, tenant_id: str, *, status: str | None = None, lang: str | None = None
    ) -> list[KbEntry]:
        async with self.db.tenant_scope(tenant_id) as session:
            stmt = select(KbEntry).where(KbEntry.tenant_id == tenant_id)
            if status:
                stmt = stmt.where(KbEntry.status == status)
            if lang:
                stmt = stmt.where(KbEntry.lang == lang)
            stmt = stmt.order_by(KbEntry.key, KbEntry.lang, KbEntry.version.desc())
            return list((await session.execute(stmt)).scalars())

    # ------------------------------------------------------------ resolver
    async def resolve(
        self,
        tenant_id: str,
        lang: str,
        *,
        stay_date: date | None = None,
        sensitivities: tuple[str, ...] = ("public",),
        fallback: bool = True,
    ) -> tuple[list[KbView], list[str]]:
        """Approved, valid, not-overdue entries for a language: one per key, plus overdue keys."""
        on = stay_date or self.today()
        today = self.today()
        async with self.db.tenant_scope(tenant_id) as session:
            rows = list(
                (
                    await session.execute(
                        select(KbEntry)
                        .where(KbEntry.tenant_id == tenant_id, KbEntry.status == "approved")
                        .order_by(KbEntry.key, KbEntry.version.desc())
                    )
                ).scalars()
            )
        by_key: dict[str, KbView] = {}
        overdue: list[str] = []
        candidates_en: dict[str, KbEntry] = {}
        for row in rows:
            if row.sensitivity not in sensitivities:
                continue
            if row.valid_from and on < row.valid_from:
                continue
            if row.valid_to and on > row.valid_to:
                continue
            if row.volatile and row.review_due and row.review_due < today:
                if row.key not in overdue:
                    overdue.append(row.key)
                continue
            if row.lang == lang and row.key not in by_key:
                by_key[row.key] = _view(row)
            elif row.lang == "en" and row.key not in candidates_en:
                candidates_en[row.key] = row
        if fallback and lang != "en":
            for key, row in candidates_en.items():
                if key not in by_key:
                    by_key[key] = _view(row, fallback_from="en")
        views = [by_key[k] for k in sorted(by_key)]
        return views, sorted(overdue)

    async def snapshot(
        self, tenant_id: str, lang: str, *, stay_date: date | None = None
    ) -> KbSnapshot:
        """Public approved entries as deterministic text (stable order, stable bytes)."""
        views, overdue = await self.resolve(
            tenant_id, lang, stay_date=stay_date, sensitivities=("public",)
        )
        lines: list[str] = []
        for view in views:
            lines.append(f"## {view.key}")
            if view.fallback_from:
                lines.append(f"(text available in {view.fallback_from} only)")
            if view.value:
                lines.append(
                    "values: " + json.dumps(view.value, ensure_ascii=False, sort_keys=True)
                )
            lines.append(view.text)
            lines.append("")
        text = "\n".join(lines).strip() + "\n"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return KbSnapshot(
            lang=lang, entries=views, text=text, digest=digest, excluded_overdue=overdue
        )

    async def lookup(
        self,
        tenant_id: str,
        key: str,
        lang: str,
        *,
        verified: bool = False,
        stay_date: date | None = None,
    ) -> KbView | None:
        sensitivities = ("public", "verified_guest_only") if verified else ("public",)
        views, _ = await self.resolve(
            tenant_id, lang, stay_date=stay_date, sensitivities=sensitivities
        )
        for view in views:
            if view.key == key:
                return view
        return None

    async def search(
        self, tenant_id: str, query: str, lang: str, *, verified: bool = False, limit: int = 3
    ) -> list[KbView]:
        sensitivities = ("public", "verified_guest_only") if verified else ("public",)
        views, _ = await self.resolve(tenant_id, lang, sensitivities=sensitivities)
        terms = {t.casefold() for t in _TOKEN.findall(query) if len(t) >= 4}
        if not terms:
            return []
        scored: list[tuple[int, KbView]] = []
        for view in views:
            key_tokens = {t.casefold() for t in _TOKEN.findall(view.key.replace(".", " "))}
            text_tokens = {t.casefold() for t in _TOKEN.findall(view.text)}
            score = 3 * len(terms & key_tokens) + 2 * len(terms & text_tokens)
            # German and Turkish compounds: a query word inside a longer text word
            for term in terms:
                if len(term) >= 5 and any(term in token and term != token for token in text_tokens):
                    score += 1
            if score:
                scored.append((score, view))
        scored.sort(key=lambda item: (-item[0], item[1].key))
        return [v for _, v in scored[:limit]]

    async def overdue(self, tenant_id: str) -> list[KbEntry]:
        today = self.today()
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(KbEntry)
                    .where(
                        KbEntry.tenant_id == tenant_id,
                        KbEntry.status == "approved",
                        KbEntry.review_due < today,
                    )
                    .order_by(KbEntry.key, KbEntry.lang)
                )
            ).scalars()
            return list(rows)

    # ---------------------------------------------------------------- gaps
    async def record_gap(
        self, tenant_id: str, question: str, lang: str, conversation_id: str | None
    ) -> None:
        question = question.strip()[:600]
        if not question:
            return
        async with self.db.tenant_scope(tenant_id) as session:
            existing = await session.scalar(
                select(KbGap).where(
                    KbGap.tenant_id == tenant_id, KbGap.question == question, KbGap.lang == lang
                )
            )
            if existing is not None:
                existing.count += 1
            else:
                session.add(
                    KbGap(
                        tenant_id=tenant_id,
                        question=question,
                        lang=lang,
                        conversation_id=conversation_id,
                    )
                )
            await session.commit()

    async def gap_report(self, tenant_id: str) -> list[KbGap]:
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(KbGap)
                    .where(KbGap.tenant_id == tenant_id, KbGap.status == "open")
                    .order_by(KbGap.count.desc())
                )
            ).scalars()
            return list(rows)

    async def coverage(self, tenant_id: str, languages: list[str]) -> dict[str, dict[str, str]]:
        """Status per required key and language: approved, draft, missing."""
        rows = await self.entries(tenant_id)
        best: dict[tuple[str, str], str] = {}
        rank = {"approved": 3, "pending_approval": 2, "draft": 1, "draft_mt": 1, "retired": 0}
        for row in rows:
            root = row.key.split(".", 1)[0]
            current = best.get((root, row.lang))
            if current is None or rank[row.status] > rank.get(current, 0):
                best[(root, row.lang)] = row.status
        return {
            key: {lang: best.get((key, lang), "missing") for lang in languages}
            for key in REQUIRED_KEYS
        }


def _windows_overlap(
    from_a: date | None, to_a: date | None, from_b: date | None, to_b: date | None
) -> bool:
    start_a = from_a or date.min
    end_a = to_a or date.max
    start_b = from_b or date.min
    end_b = to_b or date.max
    return start_a <= end_b and start_b <= end_a


def _view(row: KbEntry, fallback_from: str | None = None) -> KbView:
    return KbView(
        key=row.key,
        lang=row.lang,
        text=row.text,
        value=dict(row.value or {}),
        truth=row.truth,
        sensitivity=row.sensitivity,
        version=row.version,
        entry_id=row.id,
        fallback_from=fallback_from,
    )


def stay_date_or_today(clock: Clock, tz: str, stay: date | None) -> date:
    return stay or local_date(clock.now(), tz)


def review_due_default(today: date) -> date:
    return today + timedelta(days=DEFAULT_REVIEW_DAYS)


def snapshot_age(snapshot: KbSnapshot, now: datetime) -> int:
    return 0 if snapshot else int(now.timestamp())
