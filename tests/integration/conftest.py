"""Shared integration fixtures: the seeded demo tenant behind an in-process app."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2
import pytest
from fastapi import FastAPI

from concierge.app import create_app
from concierge.channels.whatsapp.graph import GraphClient
from concierge.clock import FakeClock
from concierge.context import AppContext
from concierge.db.models import Tenant
from concierge.db.session import Database
from concierge.demo.mock_graph import MockGraphState
from concierge.demo.mock_graph import build_router as build_mock_router
from concierge.demo.seed import seed_demo
from concierge.notify.port import FakeEmail
from concierge.security.crypto import Keyring
from concierge.services import Services, build_services
from concierge.settings import Settings
from concierge.tenants.profile import TenantProfile


@dataclass
class DemoStack:
    client: httpx2.AsyncClient
    services: Services
    state: MockGraphState
    tenant: Tenant
    profile: TenantProfile
    clock: FakeClock

    @property
    def email(self) -> FakeEmail:
        assert isinstance(self.services.email, FakeEmail)
        return self.services.email


@pytest.fixture
async def demo_stack(
    db: Database, settings: Settings, keyring: Keyring, clock: FakeClock
) -> AsyncIterator[DemoStack]:
    """Hotel Beispiel seeded, mock Graph in process, worker drained by hand."""
    clock.set(datetime(2026, 9, 29, 10, 0, tzinfo=UTC))
    state = MockGraphState()
    mock_app = FastAPI()
    mock_app.include_router(build_mock_router(state))
    graph = GraphClient(
        version="v26.0",
        base_url="http://mock/mock-graph",
        transport=httpx2.ASGITransport(app=mock_app),
    )
    ctx = AppContext(settings=settings, db=db, keyring=keyring, clock=clock)
    services = build_services(ctx, llm=None, wa_port=graph, demo=True)
    result = await seed_demo(services)
    assert result.created
    tenant = await services.tenants.require("hotel-beispiel")
    app = create_app(services, start_worker=False, graph_state=state)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield DemoStack(
            client=client,
            services=services,
            state=state,
            tenant=tenant,
            profile=services.tenants.profile_of(tenant),
            clock=clock,
        )
