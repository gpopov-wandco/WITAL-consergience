"""QA queue (F27): sampled conversations, every handoff and validator failure, flags.

Implements: F27, F17

Sampling is deterministic (a keyed-free hash of the conversation id), so the same 5
percent are chosen on every run and a test can predict them. Rows are ``qa_review``;
operators work the queue across tenants, hotel staff flag wrong replies from the inbox.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from sqlalchemy import select

from concierge.clock import Clock
from concierge.db.models import QaReview
from concierge.db.session import Database

SAMPLE_PERCENT = 5


def sampled(conversation_id: str, percent: int = SAMPLE_PERCENT) -> bool:
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100 < percent


def kb_key_for(intent: str | None) -> str:
    """The KB block a reply's intent points at (``A:parking`` -> ``parking``)."""
    if not intent or ":" not in intent:
        return ""
    topic = intent.split(":", 1)[1].strip()
    return topic.split(".")[0] if topic else ""


class QaService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def on_turn(
        self,
        tenant_id: str,
        conversation_id: str,
        *,
        events: Sequence[str],
        handoff_id: str | None,
        message_id: str | None = None,
    ) -> list[str]:
        """Queue reviews for this turn; returns the reasons created."""
        reasons: list[str] = []
        if handoff_id:
            reasons.append("handoff")
        if any(e.startswith("validator:") for e in events):
            reasons.append("validator")
        if sampled(conversation_id):
            reasons.append("sample")
        created: list[str] = []
        if not reasons:
            return created
        async with self.db.tenant_scope(tenant_id) as session:
            existing = {
                row.reason
                for row in (
                    await session.execute(
                        select(QaReview).where(
                            QaReview.tenant_id == tenant_id,
                            QaReview.conversation_id == conversation_id,
                            QaReview.status == "open",
                        )
                    )
                ).scalars()
            }
            for reason in reasons:
                if reason in existing:
                    continue
                session.add(
                    QaReview(
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        message_id=message_id,
                        reason=reason,
                    )
                )
                created.append(reason)
            await session.commit()
        return created

    async def flag(
        self,
        tenant_id: str,
        conversation_id: str,
        *,
        message_id: str | None,
        user_id: str,
        note: str = "",
    ) -> QaReview:
        async with self.db.tenant_scope(tenant_id) as session:
            row = QaReview(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                message_id=message_id,
                reason="flagged",
                notes=f"{user_id}: {note.strip()[:500]}" if note.strip() else user_id,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def queue(self, *, status: str = "open", limit: int = 200) -> list[QaReview]:
        async with self.db.scope() as session:
            return list(
                (
                    await session.execute(
                        select(QaReview)
                        .where(QaReview.status == status)
                        .order_by(QaReview.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    async def resolve(self, review_id: str, *, reviewer: str, notes: str = "") -> QaReview | None:
        async with self.db.scope() as session:
            row = await session.get(QaReview, review_id)
            if row is None:
                return None
            row.status = "done"
            row.reviewer = reviewer
            row.resolved_at = self.clock.now()
            if notes.strip():
                row.notes = (row.notes + "\n" + notes.strip()).strip()[:2000]
            await session.commit()
            await session.refresh(row)
            return row
