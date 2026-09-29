"""Admin and operator actions not covered by the page tests (F19, F20): logout, journey
pause, template sync, quiet hours, legal text edits, team activation, data requests,
registration export, reply modes and the operator usage export."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from tests.integration.conftest import DemoStack
from tests.integration.test_admin import OPERATOR, OWNER, csrf, guest_conversation, login

from concierge.security.sessions import SESSION_COOKIE

pytestmark = pytest.mark.feature("F19")


async def test_logout_ends_the_session(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, OWNER)
    token = await csrf(client)
    out = await client.post("/admin/logout", data={"csrf": token})
    assert out.status_code == 303 and out.headers["location"] == "/admin/login"
    assert SESSION_COOKIE not in {c.name for c in client.cookies.jar if c.value}
    assert (await client.get("/admin/")).status_code == 303  # back to the login page


async def test_journey_pause_and_resume(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    row = await services.reservations.by_res_id(demo_stack.tenant.id, "RES-1001")
    assert row is not None
    await login(client, OWNER)
    token = await csrf(client)
    paused = await client.post(
        f"/admin/reservations/{row.id}/pause", data={"csrf": token, "paused": "1"}
    )
    assert paused.status_code == 303
    row = await services.reservations.by_res_id(demo_stack.tenant.id, "RES-1001")
    assert row is not None and row.journey_paused is True
    resumed = await client.post(
        f"/admin/reservations/{row.id}/pause", data={"csrf": token, "paused": "0"}
    )
    assert resumed.status_code == 303
    row = await services.reservations.by_res_id(demo_stack.tenant.id, "RES-1001")
    assert row is not None and row.journey_paused is False
    unknown = await client.post(
        "/admin/reservations/res_nope/pause", data={"csrf": token, "paused": "1"}
    )
    assert unknown.status_code == 303


async def test_template_sync_quiet_hours_and_legal_text_edits(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    await login(client, OWNER)
    token = await csrf(client)
    synced = await client.post("/admin/templates/sync", data={"csrf": token})
    assert synced.status_code == 303 and "report=created" in synced.headers["location"]
    valid = await client.post(
        "/admin/settings/quiet-hours", data={"csrf": token, "quiet_hours": "22:00-07:00"}
    )
    assert valid.headers["location"] == "/admin/settings"
    profile = services.tenants.profile_of(await services.tenants.require("hotel-beispiel"))
    assert profile.journeys.quiet_hours == "22:00-07:00"
    invalid = await client.post(
        "/admin/settings/quiet-hours", data={"csrf": token, "quiet_hours": "25:00-x"}
    )
    assert "error=" in invalid.headers["location"]
    rows = await services.legal.rows(demo_stack.tenant.id)
    legal_id = rows[0].id
    edited = await client.post(
        f"/admin/legal/{legal_id}/text", data={"csrf": token, "text": "Neuer Entwurf."}
    )
    assert edited.headers["location"] == "/admin/legal"
    claim = await client.post(
        f"/admin/legal/{legal_id}/text",
        data={"csrf": token, "text": "Dieser Text ist DSGVO-konform."},
    )
    assert "error=" in claim.headers["location"]
    missing = await client.post("/admin/legal/legal_nope/text", data={"csrf": token, "text": "x"})
    assert "error=" in missing.headers["location"]


async def test_team_activation_and_self_protection(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    await login(client, OWNER)
    token = await csrf(client)
    invited = await client.post(
        "/admin/team/invite",
        data={"csrf": token, "email": "neu@hotel-beispiel.invalid", "name": "Neu", "role": "staff"},
    )
    assert invited.status_code == 303 and "link=" in invited.headers["location"]
    users = {u.email: u for u in await services.auth.users(demo_stack.tenant.id)}
    new_user = users["neu@hotel-beispiel.invalid"]
    off = await client.post(
        f"/admin/team/{new_user.id}/active", data={"csrf": token, "active": "0"}
    )
    assert off.headers["location"] == "/admin/team"
    users = {u.email: u for u in await services.auth.users(demo_stack.tenant.id)}
    assert users["neu@hotel-beispiel.invalid"].is_active is False
    on = await client.post(f"/admin/team/{new_user.id}/active", data={"csrf": token, "active": "1"})
    assert on.headers["location"] == "/admin/team"
    me = users[OWNER]
    own = await client.post(f"/admin/team/{me.id}/active", data={"csrf": token, "active": "0"})
    assert own.headers["location"].endswith("error=self")
    foreign = await client.post("/admin/team/usr_other/active", data={"csrf": token, "active": "0"})
    assert foreign.status_code == 403


async def test_data_requests_actions(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    await guest_conversation(demo_stack, "Gibt es einen Parkplatz?")
    await login(client, OWNER)
    token = await csrf(client)
    phone = "+4915112345678"
    base = {"csrf": token, "kind": "phone", "q": phone}
    invalid = await client.post("/admin/data-requests", data={**base, "q": "", "action": "export"})
    assert invalid.headers["location"].endswith("notice=invalid")
    for action in ("export", "restriction", "unrestrict", "erasure"):
        done = await client.post("/admin/data-requests", data={**base, "action": action})
        assert done.headers["location"].endswith("notice=saved"), action
    requests = await services.gdpr.requests(demo_stack.tenant.id)
    assert {r.kind for r in requests} == {"export", "restriction", "erasure"}
    task = next(r for r in requests if r.tasks)  # erasure requests carry sub-processor tasks
    completed = await client.post(
        "/admin/data-requests",
        data={**base, "action": "task", "request_id": task.id, "task_id": task.tasks[0]["id"]},
    )
    assert completed.headers["location"].endswith("notice=saved")
    unconfirmed = await client.post("/admin/data-requests", data={**base, "action": "erase"})
    assert unconfirmed.headers["location"].endswith("notice=confirm")
    erased = await client.post(
        "/admin/data-requests", data={**base, "action": "erase", "confirm": "LÖSCHEN"}
    )
    assert erased.headers["location"].endswith("notice=erased")
    subject = await services.gdpr.search(demo_stack.tenant.id, phone=phone)
    assert not subject.found
    export = await client.get(f"/admin/data-requests/export?kind=phone&q={phone}&fmt=json")
    assert export.status_code in {200, 303, 404}


async def test_registration_page_and_export(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, OWNER)
    assert (await client.get("/admin/registration")).status_code == 200
    token = await csrf(client, "/admin/registration")
    today = date(2026, 9, 29)
    exported = await client.post(
        "/admin/registration/export",
        data={
            "csrf": token,
            "start": (today - timedelta(days=30)).isoformat(),
            "end": today.isoformat(),
        },
    )
    assert exported.status_code == 200 and exported.json() == []
    bad = await client.post(
        "/admin/registration/export", data={"csrf": token, "start": "x", "end": "y"}
    )
    assert bad.status_code == 303 and bad.headers["location"] == "/admin/registration"


async def test_reply_modes(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    conversation_id = await guest_conversation(demo_stack, "Gibt es einen Parkplatz?")
    await login(client, OWNER)
    token = await csrf(client)
    empty = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "text", "text": ""},
    )
    assert empty.headers["location"].endswith("notice=empty")
    sent = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "text", "text": "Gern, bis morgen."},
    )
    assert sent.headers["location"].endswith("notice=sent")
    no_mail = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "email", "text": "Hallo"},
    )
    assert no_mail.headers["location"].endswith("notice=email_unavailable")
    unknown = await client.post(
        "/admin/conversations/conv_nope/reply", data={"csrf": token, "mode": "text", "text": "x"}
    )
    assert unknown.headers["location"].endswith("notice=unknown")


@pytest.mark.feature("F20")
async def test_operator_usage_export_formats(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, OPERATOR)
    for fmt, media in (
        ("json", "application/json"),
        ("csv", "text/csv"),
        ("pdf", "application/pdf"),
    ):
        response = await client.get(f"/operator/usage/hotel-beispiel/2026-09.{fmt}")
        assert response.status_code == 200 and response.headers["content-type"].startswith(media)
    assert (await client.get("/operator/usage/no-such-hotel/2026-09.json")).status_code == 404
    assert (await client.get("/operator/usage/hotel-beispiel/september.json")).status_code == 400
