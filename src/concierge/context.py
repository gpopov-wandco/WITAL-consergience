"""Application context: settings, database, keyring and clock wired once.

Implements: F28, F31, C16

CLI commands and the web application build one :class:`AppContext`; tests build it
from temporary settings. Nothing here reads hotel facts.
"""

from __future__ import annotations

from dataclasses import dataclass

from concierge.clock import Clock, SystemClock
from concierge.db.session import Database
from concierge.security.crypto import Keyring
from concierge.settings import Settings, get_settings


@dataclass
class AppContext:
    settings: Settings
    db: Database
    keyring: Keyring
    clock: Clock

    @classmethod
    def build(cls, settings: Settings | None = None, clock: Clock | None = None) -> AppContext:
        settings = settings or get_settings()
        return cls(
            settings=settings,
            db=Database(settings.database_url()),
            keyring=Keyring.from_settings(settings),
            clock=clock or SystemClock(),
        )

    async def close(self) -> None:
        await self.db.dispose()
