"""Load smoke (10.5): 200 concurrent signed webhooks against the ASGI app.

The harness drives the ASGI application the way a server does (scope, receive, send)
and measures the time until the response starts, which is the acknowledgement Meta
sees. Asserts median acknowledgement under 250 ms and the 99th percentile under 1 s,
no duplicate replies and no SQLite lock errors. Marked ``load``; run with
``pytest -m load tests/load -s`` to see the numbers.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from concierge.app import create_app
from concierge.db.models import Message, WebhookEvent
from concierge.demo.seed import DEMO_SECRETS
from concierge.eval.golden import GoldenStack
from concierge.security.signatures import sign_meta

pytestmark = [pytest.mark.load, pytest.mark.feature("F35")]

CONCURRENT = 200
WARM_UP = 20
MEDIAN_MS = 250
P99_MS = 1000


def payload(phone_number_id: str, index: int, ts: int) -> bytes:
    sender = f"49151300{index:05d}"
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "demo-waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "4969000000",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Load Guest"}, "wa_id": sender}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": f"wamid.load.{index}",
                                    "timestamp": str(ts),
                                    "type": "text",
                                    "text": {"body": "Gibt es einen Parkplatz?"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(body).encode()


async def post_webhook(app: Any, raw: bytes, secret: str) -> tuple[int, float]:
    """One signed POST through the ASGI interface; returns (status, ms until response start)."""
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(raw)).encode()),
        (b"x-hub-signature-256", sign_meta(raw, secret).encode()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/wa/webhook",
        "raw_path": b"/wa/webhook",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    delivered = False
    started: dict[str, float] = {"status": 0.0, "at": 0.0}

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started["status"] = float(message["status"])
            started["at"] = time.perf_counter()

    begun = time.perf_counter()
    await app(scope, receive, send)
    return int(started["status"]), (started["at"] - begun) * 1000


async def test_200_concurrent_webhooks(tmp_path: Path) -> None:
    stack = await GoldenStack.create(tmp_path / "load.db", responder_only=True)
    lock_errors: list[str] = []
    try:
        services = stack.services
        app = create_app(services, start_worker=False)
        phone_number_id = stack.tenant.phone_number_id or ""
        secret = DEMO_SECRETS["wa_app_secret"]
        ts = int(datetime(2026, 9, 29, 10, 0, tzinfo=UTC).timestamp())
        # a short warm-up fills the connection pool and statement caches, as a running
        # service has them; the burst below is the measured steady state
        for index in range(WARM_UP):
            status, _ = await post_webhook(
                app, payload(phone_number_id, CONCURRENT + index, ts), secret
            )
            assert status == 200
        results = await asyncio.gather(
            *(post_webhook(app, payload(phone_number_id, i, ts), secret) for i in range(CONCURRENT))
        )
        assert [status for status, _ in results] == [200] * CONCURRENT
        ordered = sorted(ms for _, ms in results)
        median = statistics.median(ordered)
        p99 = ordered[min(len(ordered) - 1, round(0.99 * (len(ordered) - 1)))]
        try:
            processed = await services.worker.drain()
        except Exception as exc:  # a lock error would surface here
            lock_errors.append(str(exc))
            raise
        async with services.ctx.db.scope() as session:
            events = int(await session.scalar(select(func.count()).select_from(WebhookEvent)) or 0)
            inbound = int(
                await session.scalar(
                    select(func.count()).select_from(Message).where(Message.direction == "in")
                )
                or 0
            )
            conversations = int(
                await session.scalar(
                    select(func.count(func.distinct(Message.conversation_id))).where(
                        Message.direction == "out"
                    )
                )
                or 0
            )
        sent = services.wa_port.sent  # type: ignore[attr-defined]
        per_guest: dict[str, int] = {}
        for message in sent:
            per_guest[message.to] = per_guest.get(message.to, 0) + 1
        print(
            f"\nload smoke: n={CONCURRENT} median={median:.1f}ms p99={p99:.1f}ms "
            f"processed={processed} events={events} inbound={inbound} conversations={conversations}"
        )
        assert median < MEDIAN_MS, f"median acknowledgement {median:.1f} ms"
        assert p99 < P99_MS, f"p99 acknowledgement {p99:.1f} ms"
        total = CONCURRENT + WARM_UP
        assert events == total and inbound == total and conversations == total
        assert len(per_guest) == total and all(1 <= n <= 3 for n in per_guest.values())
        assert lock_errors == []
    finally:
        await stack.close()
