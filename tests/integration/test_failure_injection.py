"""Failure injection (10.4): where each injected failure is proven.

| Failure                                   | Test                                                              |
|-------------------------------------------|-------------------------------------------------------------------|
| Model 429, 529, timeout, refusal, cap     | ``test_brain.py`` failover chain, spend cap, refusal, killed call  |
| Graph 131047, 130429, 190, timeouts       | ``test_whatsapp_pipeline.py`` window, throttle, token, unknown send|
| database busy                             | ``test_sqlite_busy_lock_waits_instead_of_failing`` (here)         |
| worker killed between sending and sent    | ``test_whatsapp_pipeline.py`` interrupted send, ``test_jobs.py``  |
| PMS 401, 429, timeouts, malformed bodies  | ``test_pms_adapters.py`` per adapter                              |
| malformed iCal                            | ``test_malformed_ical_feed_falls_through_to_none`` (here)         |
| duplicate and out-of-order webhooks       | ``tests/property/test_properties.py``                             |
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

import aiosqlite
import pytest

from concierge.availability.fake import StaticFetcher
from concierge.availability.models import Occupancy, Stay
from concierge.eval.golden import FEED_HOST, GoldenStack
from concierge.tools.backup import sqlite_path


@pytest.mark.feature("F31")
async def test_sqlite_busy_lock_waits_instead_of_failing(tmp_path: Path) -> None:
    """A concurrent writer holding the lock delays a write; ``busy_timeout`` waits it out."""
    stack = await GoldenStack.create(tmp_path / "busy.db", responder_only=True)
    try:
        services = stack.services
        path = sqlite_path(services.ctx.settings.database_url())
        assert path is not None
        lock_seconds = 0.6

        async def hold_lock() -> None:
            async with aiosqlite.connect(path.as_posix()) as raw:
                await raw.execute("BEGIN IMMEDIATE")
                await raw.execute("CREATE TABLE IF NOT EXISTS lock_probe (x INTEGER)")
                await asyncio.sleep(lock_seconds)
                await raw.rollback()

        holder = asyncio.create_task(hold_lock())
        await asyncio.sleep(0.1)  # the other connection holds the write lock now
        started = time.perf_counter()
        job = await services.jobs.enqueue("email_send", {"probe": True}, tenant_id=stack.tenant.id)
        waited = time.perf_counter() - started
        await holder
        assert job.id and waited >= lock_seconds - 0.2
        pending = await services.jobs.pending(tenant_id=stack.tenant.id, kind="email_send")
        assert len(pending) == 1
    finally:
        await stack.close()


@pytest.mark.feature("F09")
async def test_malformed_ical_feed_falls_through_to_none(tmp_path: Path) -> None:
    stack = await GoldenStack.create(tmp_path / "ical.db", responder_only=True)
    try:
        garbage = "BEGIN:VCALENDAR\r\nthis is not a calendar\r\nDTSTART:nope\r\n"
        await stack.apply_setup(
            {"chain": ["ical", "none"], "ical": {"units": {"dz": 1}, "feeds": {"dz_1": garbage}}}
        )
        fetcher = stack.adapters.fetcher
        assert isinstance(fetcher, StaticFetcher)
        profile = stack.services.tenants.profile_of(stack.tenant)
        today = stack.clock.now().date()
        answer = await stack.services.availability.check(
            stack.tenant,
            profile,
            Stay(today + timedelta(days=7), today + timedelta(days=9)),
            Occupancy(2),
        )
        assert answer.source == "none" and answer.status == "unknown"
        assert fetcher.requests[0] == f"{FEED_HOST}/dz_1.ics"
        adapter = await stack.adapters("ical", stack.tenant, profile)
        assert adapter is not None
        report = await adapter.doctor()
        assert report.ok is False or report.warnings
    finally:
        await stack.close()
