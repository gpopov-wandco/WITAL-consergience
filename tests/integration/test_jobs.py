"""Job queue: dedupe, leases, retries, dead jobs, cancellation, exactly-once recovery."""

from __future__ import annotations

from datetime import timedelta

import pytest

from concierge.clock import FakeClock
from concierge.db.models import Job
from concierge.db.session import Database
from concierge.jobs.service import JobService, backoff_seconds
from concierge.jobs.worker import JobWorker, Permanent, RetryLater

pytestmark = pytest.mark.feature("F13")


@pytest.fixture
def jobs(db: Database, clock: FakeClock) -> JobService:
    return JobService(db, clock)


async def test_enqueue_dedupe_and_reschedule(jobs: JobService, clock: FakeClock) -> None:
    first = await jobs.enqueue("journey", {"step": "t7"}, dedupe_key="journey:R1:t7:2026-10-10")
    second = await jobs.enqueue(
        "journey",
        {"step": "t7"},
        dedupe_key="journey:R1:t7:2026-10-10",
        run_at=clock.now() + timedelta(days=1),
    )
    assert first.created and not second.created and first.id == second.id
    pending = await jobs.pending(kind="journey")
    assert len(pending) == 1 and pending[0].run_at == clock.now() + timedelta(days=1)
    assert await jobs.cancel(dedupe_prefix="journey:R1:") == 1
    assert await jobs.pending() == []
    assert await jobs.cancel() == 0


async def test_worker_runs_due_jobs_once_and_retries_with_backoff(
    jobs: JobService, clock: FakeClock
) -> None:
    calls: list[str] = []
    worker = JobWorker(jobs, clock)

    async def handler(job: Job) -> None:
        calls.append(job.payload["n"])
        if job.payload["n"] == "flaky" and calls.count("flaky") == 1:
            raise RetryLater("try again")

    worker.register("work", handler)
    await jobs.enqueue("work", {"n": "now"})
    await jobs.enqueue("work", {"n": "later"}, run_at=clock.now() + timedelta(hours=1))
    await jobs.enqueue("work", {"n": "flaky"}, max_attempts=3)
    processed = await worker.drain()
    assert processed == 2 and calls == ["now", "flaky"]
    assert (await jobs.counts()) == {"done": 1, "pending": 2}
    clock.advance(timedelta(hours=1, minutes=30))
    await worker.drain()
    assert calls == ["now", "flaky", "flaky", "later"]
    assert (await jobs.counts()) == {"done": 3}
    assert 2.5 <= backoff_seconds(1) <= 5.0 and backoff_seconds(20) <= 900


async def test_dead_after_max_attempts_and_permanent_failure(
    jobs: JobService, clock: FakeClock
) -> None:
    dead: list[tuple[str, str]] = []

    async def on_dead(job: Job, error: str) -> None:
        dead.append((job.kind, error))

    worker = JobWorker(jobs, clock, on_dead=on_dead)

    async def boom(job: Job) -> None:
        raise ValueError("boom")

    async def permanent(job: Job) -> None:
        raise Permanent("bad payload")

    worker.register("boom", boom)
    worker.register("perm", permanent)
    await jobs.enqueue("boom", max_attempts=2)
    await jobs.enqueue("perm")
    await worker.drain()
    clock.advance(timedelta(minutes=20))
    await worker.drain()
    assert (await jobs.counts()) == {"dead": 2}
    assert {kind for kind, _ in dead} == {"boom", "perm"}
    assert any("ValueError" in error for _, error in dead)
    unknown = await jobs.enqueue("unknown_kind")
    await worker.drain()
    assert (
        await jobs.get(unknown.id)
    ).status == "pending"  # unknown kinds stay for a worker that knows them


async def test_lease_expiry_recovers_a_killed_worker_exactly_once(
    jobs: JobService, clock: FakeClock
) -> None:
    await jobs.enqueue("work", {"n": 1})
    killed = JobWorker(jobs, clock)
    leased = await jobs.lease_next(killed.worker_id)
    assert leased is not None and leased.status == "leased" and leased.attempts == 1
    # nobody else can take it while the lease holds
    assert await jobs.lease_next("other") is None
    clock.advance(timedelta(seconds=121))
    runs: list[str] = []
    survivor = JobWorker(jobs, clock)

    async def handler(job: Job) -> None:
        runs.append(job.id)

    survivor.register("work", handler)
    assert await survivor.drain() == 1
    assert runs == [leased.id]
    assert (await jobs.get(leased.id)).status == "done"
    assert await survivor.drain() == 0


async def test_heartbeat_and_snapshot(jobs: JobService, clock: FakeClock) -> None:
    worker = JobWorker(jobs, clock)
    assert worker.heartbeat_age_s() is None
    await worker.run_once()
    clock.advance(timedelta(seconds=5))
    assert worker.heartbeat_age_s() == 5.0
    assert worker.snapshot()["processed"] == 0


async def test_cancelled_key_is_revived_but_a_done_key_never_runs_twice(
    jobs: JobService, clock: FakeClock
) -> None:
    first = await jobs.enqueue("k", {"n": 1}, dedupe_key="dup:1")
    assert await jobs.cancel(dedupe_prefix="dup:") == 1
    revived = await jobs.enqueue("k", {"n": 2}, dedupe_key="dup:1")
    assert revived.created and revived.id == first.id
    job = await jobs.get(first.id)
    assert (
        job is not None
        and job.status == "pending"
        and job.payload == {"n": 2}
        and job.attempts == 0
    )
    await jobs.complete(first.id)
    again = await jobs.enqueue("k", {"n": 3}, dedupe_key="dup:1")
    assert not again.created and (await jobs.get(first.id)).status == "done"  # type: ignore[union-attr]
    assert await jobs.pending() == []


async def test_jobs_without_a_handler_and_final_retries_reach_the_dead_hook(
    jobs: JobService, clock: FakeClock
) -> None:
    dead: list[tuple[str, str]] = []

    async def on_dead(job: Job, error: str) -> None:
        dead.append((job.kind, error))

    bare = JobWorker(jobs, clock, on_dead=on_dead)  # no handler registered at all
    await jobs.enqueue("orphan", {})
    assert await bare.run_once() is True
    assert dead == [("orphan", "no handler for kind orphan")]

    worker = JobWorker(jobs, clock, on_dead=on_dead)

    async def always_busy(job: Job) -> None:
        raise RetryLater("busy")

    worker.register("retry", always_busy)
    await jobs.enqueue("retry", {}, max_attempts=1)  # the first failure is the last one
    assert await worker.run_once() is True
    assert dead[-1] == ("retry", "busy") and len(await jobs.dead()) == 2


async def test_worker_loop_starts_processes_survives_errors_and_stops(
    jobs: JobService, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    seen: list[int] = []
    worker = JobWorker(jobs, clock, poll_interval_s=0.01)

    async def handler(job: Job) -> None:
        seen.append(int(job.payload["n"]))

    worker.register("work", handler)
    await jobs.enqueue("work", {"n": 1})
    task = worker.start()
    for _ in range(300):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == [1] and worker.heartbeat_age_s() == 0.0  # frozen clock, fresh heartbeat

    original = jobs.lease_next
    calls = {"n": 0}

    async def flaky(*args: object, **kwargs: object) -> Job | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(jobs, "lease_next", flaky)
    await jobs.enqueue("work", {"n": 2})
    for _ in range(300):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.01)
    assert seen == [1, 2] and calls["n"] >= 2  # one logged loop error, then business as usual
    await worker.stop()
    assert task.done() and worker.snapshot()["processed"] == 2
