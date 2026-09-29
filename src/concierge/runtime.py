"""Build the running service from settings: adapters, services, app (production and demo).

Implements: F28, F29, F33, 6.2

Production: the Anthropic adapter when ``ANTHROPIC_API_KEY`` is set (otherwise the
deterministic responder), the Graph client against ``graph.facebook.com``. Demo: the
Graph client against the in-process mock Graph server, demo pages, the seeded
fixture hotel, and the responder unless a key exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI

from concierge.app import create_app
from concierge.channels.whatsapp.graph import GraphClient
from concierge.channels.whatsapp.port import WhatsAppPort
from concierge.context import AppContext
from concierge.demo.mock_graph import MockGraphState
from concierge.demo.seed import seed_demo
from concierge.llm.port import LLMPort
from concierge.services import Services, build_services
from concierge.settings import Settings


@dataclass
class Runtime:
    services: Services
    app: FastAPI
    graph_state: MockGraphState | None
    llm_configured: bool


def build_llm(settings: Settings) -> LLMPort | None:
    if settings.gc_llm_provider != "anthropic":
        return None
    key = settings.anthropic_api_key.get_secret_value()
    if not key:
        return None
    from concierge.llm.anthropic_adapter import AnthropicAdapter

    return AnthropicAdapter(key, timeout_s=min(settings.gc_llm_turn_timeout_s, 20.0))


def build_wa_port(settings: Settings, *, demo_base_url: str | None = None) -> WhatsAppPort:
    if demo_base_url:
        return GraphClient(
            version=settings.gc_wa_graph_version, base_url=f"{demo_base_url}/mock-graph"
        )
    return GraphClient(version=settings.gc_wa_graph_version)


def build_runtime(
    settings: Settings,
    *,
    demo: bool,
    bind_url: str | None = None,
    start_worker: bool = True,
) -> Runtime:
    ctx = AppContext.build(settings)
    llm = build_llm(settings)
    graph_state = MockGraphState() if demo else None
    wa_port = build_wa_port(settings, demo_base_url=bind_url if demo else None)
    services = build_services(ctx, llm=llm, wa_port=wa_port, demo=demo and llm is None)
    services.demo = demo
    startup = []
    if demo:

        async def seed() -> None:
            await seed_demo(services)

        startup.append(seed)
    app = create_app(services, start_worker=start_worker, graph_state=graph_state, startup=startup)
    return Runtime(
        services=services, app=app, graph_state=graph_state, llm_configured=llm is not None
    )
