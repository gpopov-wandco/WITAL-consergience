"""Job queue on the ``job`` table with leases, retries, dedupe keys and cancellation.

Implements: F13, F17, F24, F32, 6.6, C16

A job is leased by one worker for a bounded time; a worker that dies mid-job loses
the lease and the job is recovered exactly once. Dedupe keys (for example
``journey:{res_id}:{step}:{arrival}``) make scheduling idempotent. Retries use
jittered backoff up to ``max_attempts``, then the job is ``dead`` and an alert row
is written by the worker.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update

from concierge.clock import Clock
from concierge.db.models import Job
from concierge.db.session import Database

LEASE_SECONDS = 120
MAX_ERROR_LEN = 500


@dataclass(frozen=True)
class Enqueued:
    id: str
    created: bool


class JobService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        run_at: datetime | None = None,
        tenant_id: str | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
    ) -> Enqueued:
        """Schedule a job; with a dedupe key an existing pending job is kept and its time updated."""
        now = self.clock.now()
        when = run_at or now
        async with self.db.scope() as session:
            if dedupe_key:
                existing = await session.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
                if existing is not None:
                    if existing.status == "pending":
                        existing.run_at = when
                        existing.payload = payload or existing.payload
                        await session.commit()
                    return Enqueued(existing.id, created=False)
            job = Job(
                tenant_id=tenant_id,
                kind=kind,
                run_at=when,
                payload=payload or {},
                dedupe_key=dedupe_key,
                max_attempts=max_attempts,
            )
            session.add(job)
            await session.commit()
            return Enqueued(job.id, created=True)

    async def cancel(self, *, dedupe_prefix: str | None = None, job_id: str | None = None) -> int:
        """Cancel pending jobs by id or by dedupe-key prefix; returns the count."""
        now = self.clock.now()
        async with self.db.scope() as session:
            stmt = (
                update(Job).where(Job.status == "pending").values(status="cancelled", done_at=now)
            )
            if job_id:
                stmt = stmt.where(Job.id == job_id)
            elif dedupe_prefix:
                stmt = stmt.where(Job.dedupe_key.like(dedupe_prefix.replace("%", r"\%") + "%"))
            else:
                return 0
            result = await session.execute(stmt)
            await session.commit()
            return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def lease_next(self, worker_id: str, *, kinds: list[str] | None = None) -> Job | None:
        """Atomically lease the next due pending job (or a job whose lease expired)."""
        now = self.clock.now()
        async with self.db.scope() as session:
            stmt = (
                select(Job)
                .where(
                    Job.run_at <= now,
                    (Job.status == "pending")
                    | ((Job.status == "leased") & (Job.lease_until < now)),
                )
                .order_by(Job.run_at, Job.created_at)
                .limit(5)
            )
            if kinds:
                stmt = stmt.where(Job.kind.in_(kinds))
            candidates = list((await session.execute(stmt)).scalars())
            for candidate in candidates:
                claimed = await session.execute(
                    update(Job)
                    .where(
                        Job.id == candidate.id,
                        (Job.status == "pending")
                        | ((Job.status == "leased") & (Job.lease_until < now)),
                    )
                    .values(
                        status="leased",
                        lease_until=now + timedelta(seconds=LEASE_SECONDS),
                        worker_id=worker_id,
                        attempts=Job.attempts + 1,
                    )
                )
                if cast("CursorResult[Any]", claimed).rowcount == 1:
                    await session.commit()
                    await session.refresh(candidate)
                    return candidate
            await session.rollback()
            return None

    async def complete(self, job_id: str) -> None:
        now = self.clock.now()
        async with self.db.scope() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(status="done", done_at=now, lease_until=None)
            )
            await session.commit()

    async def fail(self, job_id: str, error: str, *, retry: bool = True) -> str:
        """Record a failure; returns the new status (``pending`` for a retry or ``dead``)."""
        now = self.clock.now()
        async with self.db.scope() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return "missing"
            job.last_error = error[:MAX_ERROR_LEN]
            if retry and job.attempts < job.max_attempts:
                delay = backoff_seconds(job.attempts)
                job.status = "pending"
                job.run_at = now + timedelta(seconds=delay)
                job.lease_until = None
            else:
                job.status = "dead"
                job.done_at = now
                job.lease_until = None
            await session.commit()
            return job.status

    async def get(self, job_id: str) -> Job | None:
        async with self.db.scope() as session:
            return await session.get(Job, job_id)

    async def pending(self, *, tenant_id: str | None = None, kind: str | None = None) -> list[Job]:
        async with self.db.scope() as session:
            stmt = select(Job).where(Job.status == "pending").order_by(Job.run_at)
            if tenant_id:
                stmt = stmt.where(Job.tenant_id == tenant_id)
            if kind:
                stmt = stmt.where(Job.kind == kind)
            return list((await session.execute(stmt)).scalars())

    async def dead(self) -> list[Job]:
        async with self.db.scope() as session:
            return list((await session.execute(select(Job).where(Job.status == "dead"))).scalars())

    async def counts(self) -> dict[str, int]:
        async with self.db.scope() as session:
            rows = (await session.execute(select(Job.status))).all()
        counts: dict[str, int] = {}
        for (status,) in rows:
            counts[status] = counts.get(status, 0) + 1
        return counts


_jitter = random.SystemRandom()


def backoff_seconds(attempt: int, *, base: float = 5.0, cap: float = 900.0) -> float:
    """Exponential backoff with full jitter: 5 s, 10 s, 20 s ... capped at 15 min."""
    upper = min(cap, base * (2 ** max(attempt - 1, 0)))
    return _jitter.uniform(base / 2, upper)
