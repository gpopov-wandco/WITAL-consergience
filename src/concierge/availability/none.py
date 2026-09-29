"""The 'no live data' source (F09): always unknown, so the assistant hands over.

Implements: F09
"""

from __future__ import annotations

from collections.abc import Sequence

from concierge.availability.models import AvailabilityAnswer, Occupancy, Stay, unknown_answer
from concierge.availability.port import BaseAdapter
from concierge.clock import Clock


class NoLiveDataAdapter(BaseAdapter):
    name = "none"

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def quote(
        self, stay: Stay, occupancy: Occupancy, room_types: Sequence[str], promo: str = ""
    ) -> AvailabilityAnswer:
        return unknown_answer(
            stay, occupancy, self.clock.now(), ["no_live_data"], source_name=self.name
        )
