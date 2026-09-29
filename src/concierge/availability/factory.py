"""Builds the availability adapter for a source from the tenant profile and secrets.

Implements: F07, F08, F09, C2, C3

Nothing here is a hotel fact: property ids come from the profile, credentials and
feed URLs from the encrypted secret store. Tests inject transports and base URLs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from concierge.availability.apaleo import ApaleoAdapter
from concierge.availability.apaleo import TokenState as ApaleoToken
from concierge.availability.beds24 import DEFAULT_BASE_URL as BEDS24_BASE_URL
from concierge.availability.beds24 import Beds24Adapter, CreditState
from concierge.availability.beds24 import TokenState as Beds24Token
from concierge.availability.ical import FeedCache, ICalAdapter
from concierge.availability.manual import ManualAdapter
from concierge.availability.mews import MewsAdapter
from concierge.availability.models import RoomTypeInfo
from concierge.availability.none import NoLiveDataAdapter
from concierge.availability.port import AvailabilityPort
from concierge.clock import Clock
from concierge.db.models import RoomType, Tenant
from concierge.db.session import Database
from concierge.security.ssrf import Fetcher, SafeFetcher
from concierge.tenants.profile import TenantProfile
from concierge.tenants.secrets import SecretStore


@dataclass
class AdapterFactory:
    db: Database
    clock: Clock
    secrets: SecretStore
    fetcher: Fetcher = field(default_factory=SafeFetcher)
    transports: dict[str, Any] = field(default_factory=dict)
    """Per-source httpx2 transports for tests (the adapters type them; core stays vendor-free)."""
    base_urls: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, AvailabilityPort] = field(default_factory=dict)
    """Ports used instead of the real adapter for a source (fakes in tests and golden runs)."""
    _beds24_tokens: dict[str, Beds24Token] = field(default_factory=dict)
    _beds24_credits: dict[str, CreditState] = field(default_factory=dict)
    _apaleo_tokens: dict[str, ApaleoToken] = field(default_factory=dict)
    _feed_cache: dict[str, dict[str, FeedCache]] = field(default_factory=dict)

    async def __call__(
        self, source: str, tenant: Tenant, profile: TenantProfile
    ) -> AvailabilityPort | None:
        config = profile.availability
        if source in self.overrides:
            return self.overrides[source]
        if source == "manual":
            return ManualAdapter(
                self.db,
                self.clock,
                tenant.id,
                stale_after_hours=config.stale_after_hours,
                currency=config.currency,
                tax_inclusive=config.tax_inclusive,
            )
        if source == "none":
            return NoLiveDataAdapter(self.clock)
        if source == "ical":
            return await self._ical(tenant, profile)
        if source == "beds24":
            if not config.beds24.property_id:
                return None
            token = await self.secrets.get(tenant.id, "beds24_refresh_token")
            if not token:
                return None
            return Beds24Adapter(
                property_id=config.beds24.property_id,
                refresh_token=token,
                clock=self.clock,
                currency=config.currency,
                tax_inclusive=config.tax_inclusive,
                allow_write=config.allow_pms_write,
                transport=self.transports.get("beds24"),
                token_state=self._beds24_tokens.setdefault(tenant.id, Beds24Token()),
                credit_state=self._beds24_credits.setdefault(tenant.id, CreditState()),
                base_url=self.base_urls.get("beds24", BEDS24_BASE_URL),
            )
        if source == "apaleo":
            if not config.apaleo.property_id:
                return None
            creds = await self.secrets.get_many(
                tenant.id, ["apaleo_client_id", "apaleo_client_secret"]
            )
            if not creds.get("apaleo_client_id") or not creds.get("apaleo_client_secret"):
                return None
            extra: dict[str, Any] = {}
            if "apaleo" in self.base_urls:
                extra["api_url"] = self.base_urls["apaleo"]
            if "apaleo_identity" in self.base_urls:
                extra["identity_url"] = self.base_urls["apaleo_identity"]
            return ApaleoAdapter(
                property_id=config.apaleo.property_id,
                client_id=creds["apaleo_client_id"],
                client_secret=creds["apaleo_client_secret"],
                clock=self.clock,
                transport=self.transports.get("apaleo"),
                token_state=self._apaleo_tokens.setdefault(tenant.id, ApaleoToken()),
                **extra,
            )
        if source == "mews":
            if not config.mews.hotel_id or not config.mews.configuration_id:
                return None
            tokens = await self.secrets.get_many(
                tenant.id, ["mews_client_token", "mews_access_token"]
            )
            return MewsAdapter(
                hotel_id=config.mews.hotel_id,
                configuration_id=config.mews.configuration_id,
                clock=self.clock,
                tz=profile.timezone,
                currency=config.currency,
                client_token=tokens.get("mews_client_token", ""),
                access_token=tokens.get("mews_access_token", ""),
                demo=config.mews.demo,
                allow_write=config.allow_pms_write,
                base_url=self.base_urls.get("mews"),
                transport=self.transports.get("mews"),
            )
        return None

    async def _ical(self, tenant: Tenant, profile: TenantProfile) -> AvailabilityPort | None:
        config = profile.availability
        if not config.ical_units:
            return None
        names = [
            f"ical_url_{code}_{index}"
            for code, count in config.ical_units.items()
            for index in range(1, count + 1)
        ]
        urls = await self.secrets.get_many(tenant.id, names)
        if not urls:
            return None
        feeds = {name.removeprefix("ical_url_"): url for name, url in urls.items()}
        units = {
            code: [f"{code}_{index}" for index in range(1, count + 1)]
            for code, count in config.ical_units.items()
        }
        async with self.db.tenant_scope(tenant.id) as session:
            rows = (
                await session.execute(
                    select(RoomType)
                    .where(RoomType.tenant_id == tenant.id)
                    .order_by(RoomType.sort_order, RoomType.code)
                )
            ).scalars()
            known = {row.code: row for row in rows}
        infos: list[RoomTypeInfo] = []
        for code, count in config.ical_units.items():
            row = known.get(code)
            infos.append(
                RoomTypeInfo(
                    code=code,
                    names=dict(row.names) if row is not None else {"de": code, "en": code},
                    max_occupancy=row.max_occupancy if row is not None else 2,
                    units_total=count,
                )
            )
        return ICalAdapter(
            self.fetcher,
            self.clock,
            feeds=feeds,
            units=units,
            room_types=infos,
            tz=profile.timezone,
            max_age_min=config.ical_max_age_min,
            cache=self._feed_cache.setdefault(tenant.id, {}),
        )
