"""The availability chain (F07): first healthy, fresh source wins.

Implements: F07, F09, C2, 6.6

Per tenant and source: a circuit breaker (3 consecutive failures open it for 60 s), a
rate budget below the vendor limit, single-flight per cache key and a cache whose
validity follows the source (live 120 s, then served as "as of HH:MM" for up to 15
minutes; iCal and manual carry their own ``valid_until``). An expired answer forces a
re-query (rule 5). On high-demand dates no indicative price without live data.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from concierge.agent.resilience import CircuitBreaker
from concierge.availability.models import (
    PMS_SOURCES,
    AvailabilityAnswer,
    AvailabilityError,
    NotSupported,
    Occupancy,
    ReservationRecord,
    Stay,
    unknown_answer,
)
from concierge.availability.port import AvailabilityPort
from concierge.clock import Clock
from concierge.db.models import Tenant
from concierge.log import get_logger
from concierge.security.limiter import MovingWindowLimiter, RateLimiter
from concierge.tenants.profile import TenantProfile

log = get_logger(__name__)

AdapterFactory = Callable[[str, Tenant, TenantProfile], Awaitable[AvailabilityPort | None]]
AlertSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]

# Own budget per tenant and source, below the vendor limits recorded in
# docs/VERIFIED_FACTS.md (VERIFY); a tenant profile may override per source.
DEFAULT_BUDGETS: dict[str, str] = {
    "beds24": "30 per 5 minutes",
    "apaleo": "100 per minute",
    "mews": "100 per 30 seconds",
    "ical": "30 per minute",
}


@dataclass
class ChainConfig:
    breaker_failures: int = 3
    breaker_open_s: float = 60.0
    live_ttl_s: float = 120.0
    cached_ttl_s: float = 900.0
    call_timeout_s: float = 8.0


@dataclass
class _Entry:
    answer: AvailabilityAnswer
    source: str


@dataclass
class AvailabilityService:
    clock: Clock
    factory: AdapterFactory
    config: ChainConfig = field(default_factory=ChainConfig)
    limiter: RateLimiter = field(default_factory=MovingWindowLimiter)
    alert_sink: AlertSink | None = None
    _breakers: dict[tuple[str, str], CircuitBreaker] = field(default_factory=dict)
    _cache: dict[tuple[Any, ...], _Entry] = field(default_factory=dict)
    _inflight: dict[tuple[Any, ...], asyncio.Future[AvailabilityAnswer]] = field(
        default_factory=dict
    )
    calls: dict[str, int] = field(default_factory=dict)
    """Adapter calls per source, for tests and the doctor."""

    def breaker(self, tenant_id: str, source: str) -> CircuitBreaker:
        key = (tenant_id, source)
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(
                self.clock,
                failure_threshold=self.config.breaker_failures,
                window_s=self.config.breaker_open_s,
                open_s=self.config.breaker_open_s,
            )
            self._breakers[key] = breaker
        return breaker

    def reset(self) -> None:
        self._breakers.clear()
        self._cache.clear()
        self._inflight.clear()
        self.calls.clear()

    async def check(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        stay: Stay,
        occupancy: Occupancy,
        room_type: str = "",
    ) -> AvailabilityAnswer:
        now = self.clock.now()
        tried: list[str] = []
        for source in profile.availability.chain:
            if source == "none":
                break
            key = (
                tenant.id,
                source,
                stay.arrival,
                stay.departure,
                occupancy.adults,
                occupancy.children,
                room_type.lower(),
            )
            cached = self._from_cache(key, now)
            if cached is not None:
                return self._policy(cached, profile, tried)
            breaker = self.breaker(tenant.id, source)
            if not breaker.allows():
                tried.append(f"{source}:breaker_open")
                continue
            adapter = await self.factory(source, tenant, profile)
            if adapter is None:
                tried.append(f"{source}:not_configured")
                continue
            budget = profile.availability.rate_limits.get(source) or DEFAULT_BUDGETS.get(source)
            if budget and not self.limiter.hit(f"avail:{tenant.id}:{source}", budget):
                tried.append(f"{source}:budget")
                continue
            try:
                answer = await self._single_flight(
                    key, adapter, stay, occupancy, [room_type] if room_type else []
                )
            except (AvailabilityError, TimeoutError) as exc:
                breaker.record_failure()
                tried.append(f"{source}:{type(exc).__name__}")
                log.warning(
                    "availability.source_failed",
                    tenant=tenant.id,
                    source=source,
                    error=type(exc).__name__,
                    breaker_open=breaker.is_open,
                )
                if breaker.is_open and self.alert_sink is not None:
                    await self.alert_sink(
                        "availability_breaker",
                        tenant.id,
                        {"kind": source, "error": type(exc).__name__},
                    )
                continue
            breaker.record_success()
            if answer.status == "unknown":
                tried.append(f"{source}:unknown")
                continue
            answer.source_name = source
            self._cache[key] = _Entry(answer, source)
            return self._policy(answer, profile, tried)
        answer = unknown_answer(stay, occupancy, now, [*tried, "no_live_data"])
        return answer

    def _from_cache(self, key: tuple[Any, ...], now: datetime) -> AvailabilityAnswer | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        answer = entry.answer
        if answer.source in {"live_pms", "live_pms_cached"}:
            age = (now - answer.fetched_at).total_seconds()
            if age < self.config.live_ttl_s:
                return answer
            if age < self.config.cached_ttl_s and answer.valid_until > now - timedelta(
                seconds=self.config.cached_ttl_s
            ):
                cached = replace(
                    answer,
                    source="live_pms_cached",
                    quotes=[replace(q, source="live_pms_cached") for q in answer.quotes],
                )
                return cached
            del self._cache[key]
            return None
        if answer.valid_until > now:
            return answer
        del self._cache[key]
        return None

    async def _single_flight(
        self,
        key: tuple[Any, ...],
        adapter: AvailabilityPort,
        stay: Stay,
        occupancy: Occupancy,
        room_types: list[str],
    ) -> AvailabilityAnswer:
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[AvailabilityAnswer] = loop.create_future()
        self._inflight[key] = future
        try:
            self.calls[adapter.name] = self.calls.get(adapter.name, 0) + 1
            async with asyncio.timeout(self.config.call_timeout_s):
                answer = await adapter.quote(stay, occupancy, room_types)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if not future.done():
                future.set_result(answer)
            return answer
        finally:
            self._inflight.pop(key, None)

    def _policy(
        self, answer: AvailabilityAnswer, profile: TenantProfile, tried: list[str]
    ) -> AvailabilityAnswer:
        for reason in tried:
            if reason not in answer.reasons:
                answer.reasons.append(reason)
        high_demand = set(profile.availability.high_demand_dates)
        if (
            high_demand
            and answer.source != "live_pms"
            and any(d in high_demand for d in answer.stay.dates())
        ):
            answer.without_prices("high_demand_no_live_price")
        return answer

    # ----------------------------------------------------------- reservations
    async def first_pms(self, tenant: Tenant, profile: TenantProfile) -> AvailabilityPort | None:
        for source in profile.availability.chain:
            if source in PMS_SOURCES:
                adapter = await self.factory(source, tenant, profile)
                if adapter is not None:
                    return adapter
        return None

    async def get_reservation(
        self, tenant: Tenant, profile: TenantProfile, ref: str
    ) -> ReservationRecord | None:
        adapter = await self.first_pms(tenant, profile)
        if adapter is None:
            return None
        breaker = self.breaker(tenant.id, adapter.name)
        if not breaker.allows():
            return None
        try:
            async with asyncio.timeout(self.config.call_timeout_s):
                record = await adapter.get_reservation(ref)
        except NotSupported:
            return None
        except (AvailabilityError, TimeoutError) as exc:
            breaker.record_failure()
            log.warning(
                "availability.reservation_lookup_failed",
                tenant=tenant.id,
                source=adapter.name,
                error=type(exc).__name__,
            )
            return None
        breaker.record_success()
        return record

    async def movements(
        self, tenant: Tenant, profile: TenantProfile, start: date, end: date
    ) -> tuple[list[ReservationRecord], list[ReservationRecord]]:
        """Arrivals and departures from the first configured PMS (empty when none)."""
        adapter = await self.first_pms(tenant, profile)
        if adapter is None:
            return [], []
        try:
            async with asyncio.timeout(self.config.call_timeout_s * 2):
                arrivals = await adapter.arrivals(start, end)
                departures = await adapter.departures(start, end)
        except NotSupported:
            return [], []
        return arrivals, departures
