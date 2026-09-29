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

from concierge.app import Startup, create_app
from concierge.channels.whatsapp.graph import GraphClient, in_process_client
from concierge.channels.whatsapp.port import WhatsAppPort
from concierge.context import AppContext
from concierge.demo.mock_graph import MockGraphState
from concierge.demo.mock_graph import build_router as build_mock_graph_router
from concierge.demo.seed import seed_demo
from concierge.llm.port import LLMPort
from concierge.registration.proof import FakeProof, PaymentProofPort
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


def build_proof(settings: Settings) -> PaymentProofPort:
    """Stripe when a key is configured, else the fake (demo and development only)."""
    key = settings.stripe_secret_key.get_secret_value()
    if key:
        from concierge.registration.stripe_proof import StripeProof

        return StripeProof(key)
    return FakeProof(settings.public_base_url())


def build_wa_port(settings: Settings, *, graph_state: MockGraphState | None = None) -> WhatsAppPort:
    """The real Graph client; in demo mode its HTTP stack is routed into the mock server."""
    if graph_state is not None:
        mock_app = FastAPI()
        mock_app.include_router(build_mock_graph_router(graph_state))
        return in_process_client(
            settings.gc_wa_graph_version, mock_app, base_url="http://mock-graph.invalid/mock-graph"
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
    wa_port = build_wa_port(settings, graph_state=graph_state)
    services = build_services(
        ctx, llm=llm, wa_port=wa_port, demo=demo and llm is None, proof=build_proof(settings)
    )
    services.demo = demo
    if bind_url:
        services.widget.own_origins.add(bind_url.rstrip("/"))  # the demo page on a custom port
    startup: list[Startup] = []
    if demo:

        async def seed() -> None:
            await seed_demo(services)

        startup.append(seed)
    app = create_app(services, start_worker=start_worker, graph_state=graph_state, startup=startup)
    return Runtime(
        services=services, app=app, graph_state=graph_state, llm_configured=llm is not None
    )
