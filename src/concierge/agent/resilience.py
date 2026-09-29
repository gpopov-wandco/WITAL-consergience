"""Circuit breaker and daily model budget for the conversation engine (8.1).

Implements: F01, F04, F24, 8.1

The breaker opens after five failures inside sixty seconds and stays open for sixty
seconds; while open, turns go to the deterministic responder. The budget guard keeps
the day's model spend per tenant (hotel-local day) and stops the model path when the
tenant's ``limits.daily_llm_budget_usd`` is reached.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime

from concierge.clock import Clock, local_date


@dataclass
class CircuitBreaker:
    clock: Clock
    failure_threshold: int = 5
    window_s: float = 60.0
    open_s: float = 60.0
    _failures: deque[datetime] = field(default_factory=deque)
    _opened_at: datetime | None = None

    def allows(self) -> bool:
        if self._opened_at is None:
            return True
        if (self.clock.now() - self._opened_at).total_seconds() >= self.open_s:
            self._opened_at = None
            self._failures.clear()
            return True
        return False

    @property
    def is_open(self) -> bool:
        return not self.allows()

    def record_failure(self) -> None:
        now = self.clock.now()
        self._failures.append(now)
        while self._failures and (now - self._failures[0]).total_seconds() > self.window_s:
            self._failures.popleft()
        if len(self._failures) >= self.failure_threshold:
            self._opened_at = now

    def record_success(self) -> None:
        self._failures.clear()


@dataclass
class DailyBudget:
    clock: Clock
    _spent: dict[tuple[str, date], float] = field(default_factory=dict)

    def day(self, tz: str) -> date:
        return local_date(self.clock.now(), tz)

    def spent(self, tenant_id: str, tz: str) -> float:
        return self._spent.get((tenant_id, self.day(tz)), 0.0)

    def record(self, tenant_id: str, tz: str, usd: float) -> None:
        key = (tenant_id, self.day(tz))
        self._spent[key] = self._spent.get(key, 0.0) + max(usd, 0.0)

    def allows(self, tenant_id: str, tz: str, limit_usd: float) -> bool:
        return limit_usd <= 0 or self.spent(tenant_id, tz) < limit_usd
