"""Moving-window rate limiting behind a small interface.

Implements: F31, F22

The service runs as one process, so an in-memory moving window is correct; the
interface allows a shared store later without touching callers.
"""

from __future__ import annotations

from typing import Protocol

from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter


class RateLimiter(Protocol):
    def hit(self, key: str, limit: str) -> bool:
        """Record one hit; return ``False`` when the limit is exhausted."""

    def remaining(self, key: str, limit: str) -> int: ...


class MovingWindowLimiter:
    """Limits such as ``"20/hour"`` or ``"5/minute"`` per key."""

    def __init__(self) -> None:
        self._storage = MemoryStorage()
        self._limiter = MovingWindowRateLimiter(self._storage)
        self._items: dict[str, RateLimitItem] = {}

    def _item(self, limit: str) -> RateLimitItem:
        item = self._items.get(limit)
        if item is None:
            item = parse(limit)
            self._items[limit] = item
        return item

    def hit(self, key: str, limit: str) -> bool:
        return bool(self._limiter.hit(self._item(limit), key))

    def remaining(self, key: str, limit: str) -> int:
        window = self._limiter.get_window_stats(self._item(limit), key)
        return int(window.remaining)

    def reset(self) -> None:
        self._storage.reset()
