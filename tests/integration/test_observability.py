"""Observability and analytics (F32, F27): metrics, health states, operator alerts,
seeded aggregates, digest job, QA queue, search, flagging, redacted export."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.integration.conftest import DemoStack
from tests.integration.test_admin import OPERATOR, OWNER, csrf, login

from concierge.db.models import OperatorAlert, QaReview
from concierge.demo.seed import DEMO_SECRETS
from concierge.demo.wa_sim import PayloadFactory, WaSimClient
from concierge.maintenance import DIGEST_KIND, RETENTION_KIND, next_daily, next_weekly

pytestmark = pytest.mark.feature("F32")

GUEST = "4915112345678"


async def chat(stack: DemoStack, text: str, *, sender: str = GUEST) -> None:
    factory = PayloadFactory(stack.tenant.phone_number_id or "", clock=stack.clock)
    sim = WaSimClient(
        "http://testserver", DEMO_SECRETS["wa_app_secret"], transport=stack.client._transport
    )
    assert (await sim.post(factory.text(sender, text))).status_code == 200
    await stack.services.worker.drain()


async def test_metrics_endpoint_is_bearer_protected(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    assert (await client.get("/metrics")).status_code == 401
    assert (
        await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    ok = await client.get("/metrics", headers={"Authorization": "Bearer metrics-token-for-tests"})
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/plain")
    assert "gc_build_info" in ok.text and "gc_worker_heartbeat_age_seconds" in ok.text
    assert "gc_llm_breaker_open 0.0" in ok.text
    await chat(demo_stack, "Hilfe, es brennt in meinem Zimmer")
    body = (
        await client.get("/metrics", headers={"Authorization": "Bearer metrics-token-for-tests"})
    ).text
    assert 'gc_handoffs_open{priority="P0",tenant="hotel-beispiel"} 1.0' in body
    assert "4915112345678" not in body and "brennt" not in body


async def test_health_states(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    assert (await client.get("/healthz")).json()["status"] == "ok"
    idle = await client.get("/readyz")
    assert idle.status_code == 503 and idle.json()["checks"]["worker"] == "not started"
    demo_stack.services.worker.last_heartbeat = demo_stack.clock.now()
    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"]["database"] == "ok"
    assert (
        ready.json()["checks"]["migration"] == "ok" and ready.json()["checks"]["master_key"] == "ok"
    )
    demo_stack.services.worker.last_heartbeat = demo_stack.clock.now() - timedelta(minutes=5)
    stale = await client.get("/readyz")
    assert stale.status_code == 503 and "stale heartbeat" in stale.json()["checks"]["worker"]


async def test_operator_alerts_are_deduplicated_and_mailed_once(demo_stack: DemoStack) -> None:
    services = demo_stack.services
    notifier = services.operator_alerts
    for _ in range(3):
        await services.brain.alert_sink(
            "spend_cap", demo_stack.tenant.id, {"model": "m", "code": "x"}
        )  # type: ignore[misc]
    async with services.ctx.db.scope() as session:
        rows = list((await session.execute(select(OperatorAlert))).scalars())
    assert len(rows) == 1 and rows[0].count == 3 and rows[0].kind == "spend_cap"
    assert await notifier.run(force=True) == 1
    assert len(notifier.sent) == 1 and "Model spend cap reached" in notifier.sent[0].text
    assert notifier.sent[0].to == "operator@example.invalid"
    assert await notifier.run(force=True) == 0  # already sent
    demo_stack.clock.advance(timedelta(hours=25))
    assert await notifier.run(force=True) == 1  # still open after a day: sent again
    assert await notifier.resolve("spend_cap", rows[0].alert_key)
    assert await notifier.run(force=True) == 0
    # the throttle: without force a run happens at most every 15 minutes
    notifier.last_run = None
    assert await notifier.due() and not await notifier.due()
    await notifier.check_price_tables((demo_stack.clock.now().date() - timedelta(days=200), None))
    async with services.ctx.db.scope() as session:
        stale = await session.scalar(
            select(OperatorAlert).where(OperatorAlert.kind == "price_table_stale")
        )
    assert stale is not None


async def test_seeded_aggregates_digest_and_qa_queue(demo_stack: DemoStack) -> None:
    services, tenant, profile = demo_stack.services, demo_stack.tenant, demo_stack.profile
    await chat(demo_stack, "Gibt es einen Parkplatz?")
    await chat(demo_stack, "Do you have parking?", sender="4915120000002")
    await chat(demo_stack, "Hilfe, es brennt in meinem Zimmer", sender="4915120000003")
    await chat(demo_stack, "Ich bin schwanger, gibt es Frühstück?", sender="4915120000004")
    end = demo_stack.clock.now() + timedelta(minutes=1)
    summary = await services.analytics.summary(tenant.id, profile, end - timedelta(days=7), end)
    assert summary.conversations == 3 and summary.excluded_special == 1
    assert summary.by_language == {"de": 2, "en": 1} and summary.by_channel == {"whatsapp": 3}
    assert summary.topics.get("parking") == 2 and summary.handoffs == {"P0": 1}
    assert summary.wa_by_category == {} and summary.llm_cost_eur == 0.0
    charts = services.analytics.charts(summary, "de")
    assert charts["languages"].count("<rect") == 2
    subject, text, html = services.analytics.digest(
        summary, profile, dashboard_url="http://x/admin/reports"
    )
    assert subject.startswith("Hotel Beispiel") and "Gespräche: 3" in text and "<svg" in html

    # the digest job mails the owner once per week
    run_at = await services.maintenance.schedule(tenant, DIGEST_KIND)
    assert run_at == next_weekly(demo_stack.clock.now(), profile.timezone, 0, 8)
    demo_stack.clock.set(run_at)
    await services.worker.drain()
    mails = demo_stack.email.to("reception@example.invalid")
    assert (
        len(mails) == 1
        and mails[0].subject == subject
        and mails[0].html
        and "<svg" in mails[0].html
    )
    pending = await services.jobs.pending(tenant_id=tenant.id, kind=DIGEST_KIND)
    assert len(pending) == 1 and pending[0].run_at == run_at + timedelta(days=7)
    assert next_daily(datetime(2026, 9, 29, 10, 0, tzinfo=UTC), "Europe/Berlin", 3) == datetime(
        2026, 9, 30, 1, 0, tzinfo=UTC
    )
    assert next_daily(datetime(2026, 9, 29, 0, 30, tzinfo=UTC), "Europe/Berlin", 3) == datetime(
        2026, 9, 29, 1, 0, tzinfo=UTC
    )
    await services.maintenance.schedule(tenant, RETENTION_KIND)
    await services.maintenance.schedule(tenant, RETENTION_KIND)
    assert len(await services.jobs.pending(tenant_id=tenant.id, kind=RETENTION_KIND)) == 1

    # QA queue: every handoff, sampled conversations
    async with services.ctx.db.scope() as session:
        reviews = list((await session.execute(select(QaReview))).scalars())
    reasons = {r.reason for r in reviews}
    assert "handoff" in reasons and reasons <= {"handoff", "sample", "validator"}
    assert len([r for r in reviews if r.reason == "handoff"]) == 1


@pytest.mark.feature("F20")
async def test_admin_search_flag_export_and_operator_qa_pages(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    await chat(demo_stack, "Gibt es einen Parkplatz?")
    await login(client, OWNER)
    search = await client.get("/admin/search?q=Parkplatz")
    assert (
        search.status_code == 200
        and "Parkplatz" in search.text
        and "/admin/conversations/" in search.text
    )
    conversation_id = re.search(r"/admin/conversations/(conv_[a-z0-9]+)", search.text).group(1)  # type: ignore[union-attr]
    page = await client.get(f"/admin/conversations/{conversation_id}")
    assert "Antwort als falsch markieren" in page.text and "/admin/kb?q=parking" in page.text
    token = await csrf(client)
    flagged = await client.post(
        f"/admin/conversations/{conversation_id}/flag",
        data={"csrf": token, "note": "Preis veraltet"},
    )
    assert flagged.headers["location"].endswith("notice=flagged")
    reviews = await services.qa.queue()
    assert any(r.reason == "flagged" and "Preis veraltet" in r.notes for r in reviews)
    export = await client.get(f"/admin/conversations/{conversation_id}/export?redacted=1")
    assert export.status_code == 200 and export.json()["redacted"] is True
    texts = [m["text"] for m in export.json()["messages"] if m["text"]]
    assert any("Parkplatz" in t for t in texts) and not any("4915112345678" in t for t in texts)
    reports = await client.get("/admin/reports?days=7")
    assert reports.status_code == 200 and "<svg" in reports.text and "usage.csv" in reports.text
    csv = await client.get("/admin/reports/usage.csv?month=2026-09")
    assert csv.status_code == 200 and csv.text.startswith("month;kind;category")
    pdf = await client.get("/admin/reports/usage.pdf?month=2026-09")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    data_requests = await client.get("/admin/data-requests?kind=phone&q=%2B4915112345678")
    assert data_requests.status_code == 200 and "Betroffene Person gefunden" in data_requests.text
    created = await client.post(
        "/admin/data-requests",
        data={"csrf": token, "kind": "phone", "q": "+4915112345678", "action": "export"},
    )
    assert created.status_code == 303 and "notice=saved" in created.headers["location"]
    download = await client.get(
        "/admin/data-requests/export?kind=phone&q=%2B4915112345678&fmt=json"
    )
    assert download.status_code == 200 and download.json()["guests"][0]["phone"] == "+4915112345678"
    await client.post("/admin/logout", data={"csrf": token})
    await login(client, OPERATOR)
    queue = await client.get("/operator/qa")
    assert queue.status_code == 200 and "flagged" in queue.text
    review = next(r for r in await services.qa.queue() if r.reason == "flagged")
    op_token = await csrf(client, "/operator/")
    done = await client.post(
        f"/operator/qa/{review.id}/done", data={"csrf": op_token, "notes": "ok"}
    )
    assert done.status_code == 303
    assert (await client.get("/operator/qa?status=done")).text.count("flagged") >= 1
    usage = await client.get("/operator/usage/hotel-beispiel/2026-09.json")
    assert usage.status_code == 200 and '"month": "2026-09"' in usage.text
    await client.post("/admin/logout", data={"csrf": op_token})
    await login(client, OWNER)
    token = await csrf(client)
    refused = await client.post(
        "/admin/data-requests",
        data={
            "csrf": token,
            "kind": "phone",
            "q": "+4915112345678",
            "action": "erase",
            "confirm": "no",
        },
    )
    assert "notice=confirm" in refused.headers["location"]
    erased = await client.post(
        "/admin/data-requests",
        data={
            "csrf": token,
            "kind": "phone",
            "q": "+4915112345678",
            "action": "erase",
            "confirm": "LÖSCHEN",
        },
    )
    assert "notice=erased" in erased.headers["location"]
    assert not (await services.gdpr.search(demo_stack.tenant.id, phone="+4915112345678")).found
