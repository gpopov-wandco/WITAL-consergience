"""The single in-process worker loop.

Implements: F13, F17, F32, 6.6

Handlers are registered per job kind. The loop leases one job at a time, runs the
handler, completes or retries it, and keeps a heartbeat that ``/readyz`` checks
(under 30 s). It never calls ``loop.add_signal_handler`` and works on the Windows
Proactor loop. ``run_once`` drives the loop deterministically in tests and in the
demo time warp.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from concierge.clock import Clock
from concierge.db.models import Job
from concierge.ids import new_id
from concierge.jobs.service import JobService
from concierge.log import bind_context, clear_context, get_logger

log = get_logger(__name__)

Handler = Callable[[Job], Awaitable[None]]
DeadHook = Callable[[Job, str], Awaitable[None]]


class RetryLater(Exception):
    """Raised by a handler to retry without counting as a defect."""


class Permanent(Exception):
    """Raised by a handler when retrying cannot help."""


class JobWorker:
    def __init__(
        self,
        jobs: JobService,
        clock: Clock,
        *,
        poll_interval_s: float = 1.0,
        on_dead: DeadHook | None = None,
    ) -> None:
        self.jobs = jobs
        self.clock = clock
        self.poll_interval_s = poll_interval_s
        self.on_dead = on_dead
        self.worker_id = new_id("wrk")
        self.handlers: dict[str, Handler] = {}
        self.last_heartbeat: datetime | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.processed = 0

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    async def run_once(self) -> bool:
        """Process at most one job; returns whether a job was processed."""
        self.last_heartbeat = self.clock.now()
        job = await self.jobs.lease_next(self.worker_id, kinds=list(self.handlers) or None)
        if job is None:
            return False
        bind_context(job_id=job.id, job_kind=job.kind, tenant=job.tenant_id or "")
        try:
            handler = self.handlers.get(job.kind)
            if handler is None:
                await self._dead(job, f"no handler for kind {job.kind}")
                return True
            await handler(job)
        except RetryLater as exc:
            status = await self.jobs.fail(job.id, f"retry: {exc}")
            if status == "dead":
                await self._dead(job, str(exc))
        except Permanent as exc:
            await self.jobs.fail(job.id, str(exc), retry=False)
            await self._dead(job, str(exc))
        except Exception as exc:
            log.exception("job.failed", error=type(exc).__name__)
            status = await self.jobs.fail(job.id, f"{type(exc).__name__}: {exc}")
            if status == "dead":
                await self._dead(job, f"{type(exc).__name__}: {exc}")
        else:
            await self.jobs.complete(job.id)
        finally:
            self.processed += 1
            clear_context()
        return True

    async def drain(self, *, max_jobs: int = 1000) -> int:
        """Run until no due job remains (tests and the demo time warp)."""
        count = 0
        while count < max_jobs and await self.run_once():
            count += 1
        return count

    async def _dead(self, job: Job, error: str) -> None:
        log.error("job.dead", error=error)
        if self.on_dead is not None:
            await self.on_dead(job, error)

    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
            except Exception:
                log.exception("worker.loop_error")
                processed = False
            if not processed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self.run_forever(), name="job-worker")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    def heartbeat_age_s(self) -> float | None:
        if self.last_heartbeat is None:
            return None
        return (self.clock.now() - self.last_heartbeat).total_seconds()

    def snapshot(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "processed": self.processed,
            "heartbeat_age_s": self.heartbeat_age_s(),
            "kinds": sorted(self.handlers),
        }
