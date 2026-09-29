"""Hotel admin and operator console (F19, F20, F17): auth, pages, inbox flow, roles."""

from __future__ import annotations

import re
from datetime import timedelta

import httpx2
import pytest
from tests.integration.conftest import DemoStack

from concierge.demo.seed import DEMO_SECRETS
from concierge.demo.wa_sim import PayloadFactory, WaSimClient
from concierge.security.sessions import SESSION_COOKIE
from concierge.settings import Settings
from concierge.tools.setup import _bootstrap_operator

pytestmark = pytest.mark.feature("F19")

OWNER = "owner@hotel-beispiel.invalid"
STAFF = "staff@hotel-beispiel.invalid"
OPERATOR = "operator@witalco.invalid"
PASSWORDS = {
    OWNER: "demo-owner-password",
    STAFF: "demo-staff-password",
    OPERATOR: "demo-operator-password",
}
GUEST = "4915112345678"
PAGES = (
    "/admin/",
    "/admin/inbox",
    "/admin/inbox?status=resolved&priority=P1",
    "/admin/kb",
    "/admin/kb?status=approved&lang=de",
    "/admin/questionnaire",
    "/admin/availability",
    "/admin/availability?month=2026-10",
    "/admin/reservations",
    "/admin/templates",
    "/admin/settings",
    "/admin/legal",
    "/admin/team",
    "/admin/data-requests",
    "/admin/reports",
    "/admin/registration",
)


async def login(client: httpx2.AsyncClient, email: str) -> str:
    response = await client.post(
        "/admin/login", data={"email": email, "password": PASSWORDS[email]}
    )
    assert response.status_code == 303, response.text
    return response.headers["location"]


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([0-9a-f]{64})"', html)
    assert match, "no csrf field in page"
    return match.group(1)


async def csrf(client: httpx2.AsyncClient, path: str = "/admin/") -> str:
    page = await client.get(path)
    assert page.status_code == 200, page.text[:300]
    return csrf_of(page.text)


async def guest_conversation(stack: DemoStack, text: str) -> str:
    """Send one guest message through the simulator; returns the conversation id."""
    factory = PayloadFactory(stack.tenant.phone_number_id or "", clock=stack.clock)
    sim = WaSimClient(
        "http://testserver", DEMO_SECRETS["wa_app_secret"], transport=stack.client._transport
    )
    response = await sim.post(factory.text(GUEST, text))
    assert response.status_code == 200
    await stack.services.worker.drain()
    guest = await stack.services.conversations.get_guest(stack.tenant.id, "whatsapp", GUEST)
    assert guest is not None
    conversation = await stack.services.conversations.open_conversation(
        stack.tenant.id, guest.id, "whatsapp", lang="de"
    )
    return conversation.id


# ------------------------------------------------------------------- auth
async def test_login_page_invalid_credentials_and_lockout(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    page = await client.get("/admin/login")
    assert page.status_code == 200 and 'name="password"' in page.text
    assert "content-security-policy" in page.headers

    wrong = await client.post("/admin/login", data={"email": OWNER, "password": "nope"})
    assert wrong.status_code == 303 and wrong.headers["location"].endswith(
        "error=invalid_credentials"
    )
    shown = await client.get(wrong.headers["location"])
    assert shown.status_code == 200 and "E-Mail oder Passwort ist falsch." in shown.text
    assert SESSION_COOKIE not in client.cookies

    for _ in range(4):
        await client.post("/admin/login", data={"email": OWNER, "password": "nope"})
    locked = await client.post("/admin/login", data={"email": OWNER, "password": PASSWORDS[OWNER]})
    assert locked.headers["location"].endswith("error=locked")
    demo_stack.clock.advance(timedelta(minutes=16))
    assert await login(client, OWNER) == "/admin/"
    assert SESSION_COOKIE in client.cookies


async def test_unauthenticated_requests_redirect_to_login(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    response = await client.get("/admin/inbox")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?next=/admin/inbox"
    posted = await client.post("/admin/conversations/conv_x/note", data={"text": "x"})
    assert posted.status_code == 303 and posted.headers["location"].startswith("/admin/login")


async def test_open_redirect_is_refused(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    response = await client.post(
        "/admin/login",
        data={"email": OWNER, "password": PASSWORDS[OWNER], "next": "//evil.invalid/x"},
    )
    assert response.headers["location"] == "/admin/"


async def test_logout_ends_the_session(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, OWNER)
    token = csrf(client)
    response = await client.post("/admin/logout", data={"csrf": await token})
    assert response.status_code == 303
    assert (await client.get("/admin/")).status_code == 303


async def test_session_idle_and_absolute_expiry(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, OWNER)
    demo_stack.clock.advance(timedelta(hours=1, minutes=59))
    assert (await client.get("/admin/")).status_code == 200
    demo_stack.clock.advance(timedelta(hours=2, minutes=1))
    assert (await client.get("/admin/")).status_code == 303


# ------------------------------------------------------------------ pages
async def test_owner_opens_every_page(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, OWNER)
    for path in PAGES:
        response = await client.get(path)
        assert response.status_code == 200, (path, response.text[:300])
        assert "<html" in response.text and "Guest Concierge" in response.text
    reservation = await demo_stack.services.reservations.by_res_id(demo_stack.tenant.id, "RES-1001")
    assert reservation is not None
    journey = await client.get(f"/admin/reservations/{reservation.id}/journey")
    assert journey.status_code == 200 and "gc_" in journey.text
    missing = await client.get("/admin/reservations/res_missing/journey")
    assert missing.status_code == 404
    conversation_id = await guest_conversation(demo_stack, "Gibt es einen Parkplatz?")
    page = await client.get(f"/admin/conversations/{conversation_id}")
    assert page.status_code == 200 and "Parkplatz" in page.text
    assert "5678" in page.text and GUEST not in page.text  # only the last four digits


async def test_english_ui_for_english_locale_user(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, OWNER)
    token = await csrf(client, "/admin/team")
    invited = await client.post(
        "/admin/team/invite",
        data={
            "csrf": token,
            "name": "Pat Example",
            "email": "pat@hotel-beispiel.invalid",
            "role": "staff",
            "locale": "en",
        },
    )
    assert invited.status_code == 303
    link = re.search(r"link=(\S+)", invited.headers["location"])
    assert link
    await client.post("/admin/logout", data={"csrf": token})
    invite_path = link.group(1).split("http://127.0.0.1:8000", 1)[-1]
    form = await client.get(invite_path)
    assert form.status_code == 200 and "Set password" in form.text
    accepted = await client.post(invite_path, data={"password": "correct-horse-battery-staple"})
    assert accepted.status_code == 303
    response = await client.post(
        "/admin/login",
        data={"email": "pat@hotel-beispiel.invalid", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 303
    dashboard = await client.get("/admin/")
    assert dashboard.status_code == 200 and "Dashboard" in dashboard.text
    assert 'lang="en"' in dashboard.text and "Inbox" in dashboard.text


# ------------------------------------------------------------------ roles
async def test_staff_role_is_limited(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, STAFF)
    assert (await client.get("/admin/inbox")).status_code == 200
    assert (await client.get("/admin/registration")).status_code == 403
    assert (await client.get("/operator/")).status_code == 403
    token = await csrf(client, "/admin/team")
    invite = await client.post(
        "/admin/team/invite",
        data={"csrf": token, "name": "X", "email": "x@hotel-beispiel.invalid", "role": "staff"},
    )
    assert invite.status_code == 403 and "403" in invite.text
    approve = await client.post(
        "/admin/legal/legal_x/approve", data={"csrf": token, "approver": "A B"}
    )
    assert approve.status_code == 403


async def test_csrf_is_required_for_mutations(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, OWNER)
    conversation_id = await guest_conversation(demo_stack, "Gibt es einen Parkplatz?")
    missing = await client.post(f"/admin/conversations/{conversation_id}/note", data={"text": "x"})
    assert missing.status_code == 403
    wrong = await client.post(
        f"/admin/conversations/{conversation_id}/note", data={"text": "x", "csrf": "0" * 64}
    )
    assert wrong.status_code == 403
    token = await csrf(client)
    header = await client.post(
        f"/admin/conversations/{conversation_id}/note",
        data={"text": "Gast kommt spät"},
        headers={"X-CSRF-Token": token},
    )
    assert header.status_code == 303
    notes = await demo_stack.services.handoffs.notes(demo_stack.tenant.id, conversation_id)
    assert [n.text for n in notes] == ["Gast kommt spät"]


# ------------------------------------------------------------- inbox flow
async def test_inbox_flow_acknowledge_reply_resolve(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    conversation_id = await guest_conversation(demo_stack, "Gibt es einen Parkplatz?")
    created = await services.handoffs.create(
        demo_stack.tenant.id,
        conversation_id,
        priority="P1",
        trigger="guest_request",
        reason="Gast möchte mit einer Person sprechen",
        profile=demo_stack.profile,
    )
    assert created.created
    await login(client, OWNER)
    inbox = await client.get("/admin/inbox")
    assert inbox.status_code == 200 and conversation_id in inbox.text and "P1" in inbox.text
    assert "SLA-Konfiguration" in inbox.text  # C12: no promised response time
    page = await client.get(f"/admin/conversations/{conversation_id}")
    assert page.status_code == 200 and "Parkplatz" in page.text and "Übernehmen" in page.text
    assert "Parkhaus" in page.text or "Parkplatz" in page.text  # suggested reply from the KB
    token = csrf_of(page.text)

    acknowledged = await client.post(
        f"/admin/conversations/{conversation_id}/acknowledge", data={"csrf": token}
    )
    assert acknowledged.status_code == 303
    handoff = await services.handoffs.get(demo_stack.tenant.id, created.id)
    assert handoff is not None and handoff.status == "acknowledged"
    conversation = await services.conversations.get(demo_stack.tenant.id, conversation_id)
    assert conversation is not None and conversation.status == "staff_active"

    replied = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "text", "text": "Ja, der Parkplatz ist frei."},
    )
    assert replied.headers["location"].endswith("notice=sent")
    await services.worker.drain()
    sent = demo_stack.state.messages_to(GUEST)
    assert "Ja, der Parkplatz ist frei." in str(sent[-1]["payload"])
    transcript = await client.get(f"/admin/conversations/{conversation_id}")
    assert "S:staff" in transcript.text or "Ja, der Parkplatz ist frei." in transcript.text

    empty = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "text", "text": "   "},
    )
    assert empty.headers["location"].endswith("notice=empty")

    resolved = await client.post(
        f"/admin/conversations/{conversation_id}/resolve",
        data={"csrf": token, "return_to_bot": "1"},
    )
    assert resolved.status_code == 303
    conversation = await services.conversations.get(demo_stack.tenant.id, conversation_id)
    assert conversation is not None and conversation.status == "bot_handling"
    handoff = await services.handoffs.get(demo_stack.tenant.id, created.id)
    assert handoff is not None and handoff.status == "resolved"
    assert conversation_id not in (await client.get("/admin/inbox")).text
    assert conversation_id in (await client.get("/admin/inbox?status=resolved")).text


async def test_reply_outside_window_needs_template_or_email(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    conversation_id = await guest_conversation(demo_stack, "Gibt es einen Parkplatz?")
    demo_stack.clock.advance(timedelta(hours=25))  # the 24 h customer service window is over
    await login(client, OWNER)
    token = await csrf(client)
    closed = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "text", "text": "Hallo"},
    )
    assert closed.headers["location"].endswith("notice=window_closed")
    page = await client.get(f"/admin/conversations/{conversation_id}")
    assert ">geschlossen<" in page.text and "gc_booking_followup" in page.text

    template = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "template", "template": "gc_booking_followup", "text": ""},
    )
    assert template.headers["location"].endswith("notice=sent_template")
    await services.worker.drain()  # also runs the journey steps that became due meanwhile
    names = [
        m["payload"].get("template", {}).get("name")
        for m in demo_stack.state.messages_to(GUEST)
        if m["payload"].get("type") == "template"
    ]
    assert "gc_booking_followup" in names

    unknown = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "template", "template": "gc_not_there", "text": ""},
    )
    assert unknown.headers["location"].endswith("notice=template_refused")

    no_address = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "email", "text": "Hallo"},
    )
    assert no_address.headers["location"].endswith("notice=email_unavailable")


async def test_emergency_creates_p0_and_shows_on_dashboard(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    conversation_id = await guest_conversation(demo_stack, "Hilfe, es brennt in meinem Zimmer")
    handoff = await services.handoffs.open_for_conversation(demo_stack.tenant.id, conversation_id)
    assert handoff is not None and handoff.priority == "P0"
    await login(client, OWNER)
    dashboard = await client.get("/admin/")
    assert "P0 1" in dashboard.text
    inbox = await client.get("/admin/inbox?priority=P0")
    assert conversation_id in inbox.text


# --------------------------------------------------------- owner actions
async def test_kb_approval_availability_and_settings_actions(demo_stack: DemoStack) -> None:
    services, client, tenant = demo_stack.services, demo_stack.client, demo_stack.tenant
    await login(client, OWNER)
    token = await csrf(client)

    entry = await services.kb.upsert(
        tenant.id,
        "extras.sauna",
        "de",
        text="Die Sauna ist von 16 bis 21 Uhr geöffnet.",
        status="draft",
    )
    kb_page = await client.get("/admin/kb?status=draft")
    assert entry.id in kb_page.text
    approved = await client.post(
        f"/admin/kb/{entry.id}/status",
        data={"csrf": token, "status": "approved", "approver": "Maria Beispiel"},
    )
    assert approved.status_code == 303 and approved.headers["location"] == "/admin/kb"
    rows = await services.kb.entries(tenant.id, status="approved")
    assert any(r.key == "extras.sauna" and r.approver == "Maria Beispiel" for r in rows)
    illegal = await client.post(
        f"/admin/kb/{entry.id}/status", data={"csrf": token, "status": "draft"}
    )
    assert "error=KbError" in illegal.headers["location"]

    bulk = await client.post(
        "/admin/availability/bulk",
        data={
            "csrf": token,
            "room_type": "dz",
            "start": "2026-10-10",
            "end": "2026-10-12",
            "units_available": "0",
            "price_eur": "150,00",
        },
    )
    assert bulk.status_code == 303 and bulk.headers["location"].endswith("month=2026-10")
    grid = await services.inventory.grid(tenant.id, 2026, 10)
    cells = {c.date.isoformat(): c for c in grid["dz"]}
    assert cells["2026-10-10"].units_available == 0 and cells["2026-10-10"].price_cents == 15000
    assert cells["2026-10-13"].units_available > 0
    bad = await client.post(
        "/admin/availability/bulk",
        data={"csrf": token, "room_type": "dz", "start": "nope", "end": "2026-10-12"},
    )
    assert "report=" in bad.headers["location"]

    csv_text = "date;room_type;units_available;min_stay\n2026-10-20;ez;1;2\n"
    dry = await client.post(
        "/admin/availability/csv",
        data={"csrf": token, "apply": "0"},
        files={"file": ("inventory.csv", csv_text.encode(), "text/csv")},
    )
    assert dry.status_code == 303
    report_page = await client.get(dry.headers["location"])
    assert report_page.status_code == 200 and "Nur prüfen" in report_page.text
    applied = await client.post(
        "/admin/availability/csv",
        data={"csrf": token, "apply": "1"},
        files={"file": ("inventory.csv", csv_text.encode(), "text/csv")},
    )
    assert applied.status_code == 303
    grid = await services.inventory.grid(tenant.id, 2026, 10)
    cell = next(c for c in grid["ez"] if c.date.isoformat() == "2026-10-20")
    assert cell.units_available == 1 and cell.min_stay == 2

    quiet = await client.post(
        "/admin/settings/quiet-hours", data={"csrf": token, "quiet_hours": "22:00-07:00"}
    )
    assert quiet.headers["location"] == "/admin/settings"
    refreshed = await services.tenants.require("hotel-beispiel")
    assert services.tenants.profile_of(refreshed).journeys.quiet_hours == "22:00-07:00"
    invalid = await client.post(
        "/admin/settings/quiet-hours", data={"csrf": token, "quiet_hours": "late"}
    )
    assert "error=" in invalid.headers["location"]
    settings_page = await client.get("/admin/settings")
    assert "22:00-07:00" in settings_page.text and "demo-access-token" not in settings_page.text


async def test_legal_texts_approve_and_forbidden_claims(demo_stack: DemoStack) -> None:
    services, client, tenant = demo_stack.services, demo_stack.client, demo_stack.tenant
    await login(client, OWNER)
    page = await client.get("/admin/legal")
    assert page.status_code == 200 and "draft_unreviewed" in page.text
    assert "compliant" not in page.text.lower() and "rechtssicher" not in page.text.lower()
    token = csrf_of(page.text)
    rows = await services.legal.rows(tenant.id)
    row = next(r for r in rows if r.key == "ai_disclosure" and r.lang == "de")

    short = await client.post(
        f"/admin/legal/{row.id}/approve", data={"csrf": token, "approver": "M"}
    )
    assert "error=" in short.headers["location"]
    approved = await client.post(
        f"/admin/legal/{row.id}/approve", data={"csrf": token, "approver": "Maria Beispiel"}
    )
    assert approved.headers["location"] == "/admin/legal"
    row = next(r for r in await services.legal.rows(tenant.id) if r.id == row.id)
    assert row.status == "approved" and row.approved_by == "Maria Beispiel"

    claim = await client.post(
        f"/admin/legal/{row.id}/text",
        data={"csrf": token, "text": "Dieser Text ist rechtssicher und DSGVO-konform."},
    )
    assert "error=" in claim.headers["location"]
    row = next(r for r in await services.legal.rows(tenant.id) if r.id == row.id)
    assert row.status == "approved" and "rechtssicher" not in row.text

    edited = await client.post(
        f"/admin/legal/{row.id}/text", data={"csrf": token, "text": "Neuer Entwurf."}
    )
    assert edited.headers["location"] == "/admin/legal"
    row = next(r for r in await services.legal.rows(tenant.id) if r.id == row.id)
    assert row.status == "draft_unreviewed" and row.version == 2 and row.approved_by is None


async def test_team_invite_deactivate_and_registration_export(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    await login(client, OWNER)
    token = await csrf(client, "/admin/team")
    invited = await client.post(
        "/admin/team/invite",
        data={"csrf": token, "name": "Neu", "email": "neu@hotel-beispiel.invalid", "role": "staff"},
    )
    assert invited.status_code == 303 and "link=" in invited.headers["location"]
    team = await client.get(invited.headers["location"])
    assert "neu@hotel-beispiel.invalid" in team.text and "invited" in team.text
    assert [m.to for m in demo_stack.email.sent] == []  # e-mail goes out through the outbox
    await services.worker.drain()
    assert [m.to for m in demo_stack.email.sent] == ["neu@hotel-beispiel.invalid"]

    bad = await client.post(
        "/admin/team/invite", data={"csrf": token, "name": "X", "email": "nope", "role": "staff"}
    )
    assert bad.headers["location"].endswith("error=bad_email")

    users = {u.email: u for u in await services.auth.users(demo_stack.tenant.id)}
    staff = users[STAFF]
    off = await client.post(f"/admin/team/{staff.id}/active", data={"csrf": token, "active": "0"})
    assert off.status_code == 303
    other = httpx2.AsyncClient(transport=client._transport, base_url="http://testserver")
    refused = await other.post("/admin/login", data={"email": STAFF, "password": PASSWORDS[STAFF]})
    assert refused.headers["location"].endswith("error=invalid_credentials")
    self_off = await client.post(
        f"/admin/team/{users[OWNER].id}/active", data={"csrf": token, "active": "0"}
    )
    assert self_off.headers["location"].endswith("error=self")

    export = await client.post(
        "/admin/registration/export",
        data={"csrf": token, "start": "2026-09-01", "end": "2026-09-30"},
    )
    assert export.status_code == 200 and export.json() == []
    assert "meldeschein-2026-09-01-2026-09-30.json" in export.headers["content-disposition"]


# --------------------------------------------------------------- operator
@pytest.mark.feature("F20")
async def test_operator_console_switch_kill_switch_and_prices(demo_stack: DemoStack) -> None:
    services, client = demo_stack.services, demo_stack.client
    assert await login(client, OPERATOR) == "/operator/"
    assert (await client.get("/admin/")).headers["location"] == "/operator/"
    index = await client.get("/operator/")
    assert index.status_code == 200 and "hotel-beispiel" in index.text and "60/60" in index.text
    assert "Meta Platforms Ireland" in index.text and "Anthropic" in index.text
    token = csrf_of(index.text)

    switched = await client.post("/operator/switch/hotel-beispiel", data={"csrf": token})
    assert switched.headers["location"] == "/admin/"
    dashboard = await client.get("/admin/")
    assert dashboard.status_code == 200 and "Hotel Beispiel" in dashboard.text
    assert (await client.get("/admin/registration")).status_code == 200  # operator acts as owner
    unknown = await client.post("/operator/switch/no-such-hotel", data={"csrf": token})
    assert unknown.headers["location"] == "/operator/"

    on = await client.post(
        "/operator/tenants/hotel-beispiel/kill-switch", data={"csrf": token, "on": "1"}
    )
    assert on.status_code == 303
    tenant = await services.tenants.require("hotel-beispiel")
    assert tenant.kill_switch is True
    assert "Notaus" in (await client.get("/operator/")).text
    off = await client.post(
        "/operator/tenants/hotel-beispiel/kill-switch", data={"csrf": token, "on": "0"}
    )
    assert off.status_code == 303
    assert (await services.tenants.require("hotel-beispiel")).kill_switch is False

    activation = await client.get("/operator/tenants/hotel-beispiel/activation")
    assert activation.status_code == 200 and "doctor" in activation.text.lower()

    prices = await client.get("/operator/prices")
    assert prices.status_code == 200
    added = await client.post(
        "/operator/prices",
        data={
            "csrf": token,
            "region": "de",
            "category": "utility",
            "effective_from": "2026-10-01",
            "price_eur": "0,0521",
            "free_allowance": "0",
            "source_url": "https://developers.facebook.com/docs/whatsapp/pricing",
        },
    )
    assert added.headers["location"] == "/operator/prices"
    listed = await client.get("/operator/prices")
    assert "0.0521" in listed.text and "DE" in listed.text
    invalid = await client.post(
        "/operator/prices",
        data={"csrf": token, "category": "other", "effective_from": "2026-10-01", "price_eur": "1"},
    )
    assert invalid.headers["location"].endswith("error=invalid")


async def test_owner_cannot_reach_operator_actions(demo_stack: DemoStack) -> None:
    client = demo_stack.client
    await login(client, OWNER)
    token = await csrf(client)
    assert (await client.get("/operator/")).status_code == 403
    assert (await client.get("/operator/prices")).status_code == 403
    killed = await client.post(
        "/operator/tenants/hotel-beispiel/kill-switch", data={"csrf": token, "on": "1"}
    )
    assert killed.status_code == 403
    assert (await demo_stack.services.tenants.require("hotel-beispiel")).kill_switch is False


# -------------------------------------------------------------- bootstrap
async def test_bootstrap_link_sets_the_first_password_once(
    demo_stack: DemoStack, settings: Settings
) -> None:
    services, client = demo_stack.services, demo_stack.client
    async with services.ctx.db.scope() as session:
        from sqlalchemy import select

        from concierge.db.models import User

        operator = await session.scalar(select(User).where(User.email_lower == OPERATOR))
        assert operator is not None
        operator.password_hash = None
        await session.commit()
    link = await _bootstrap_operator(services.ctx.db, settings, demo_stack.clock, OPERATOR)
    assert link is not None and "/admin/bootstrap/" in link
    path = link.split("http://127.0.0.1:8000", 1)[-1]
    form = await client.get(path)
    assert form.status_code == 200 and OPERATOR in form.text
    weak = await client.post(path, data={"password": "short"})
    assert weak.status_code == 400
    done = await client.post(path, data={"password": "a-long-and-fresh-passphrase"})
    assert done.status_code == 303 and done.headers["location"] == "/admin/login"
    assert (await client.get(path)).status_code == 404  # single use
    response = await client.post(
        "/admin/login", data={"email": OPERATOR, "password": "a-long-and-fresh-passphrase"}
    )
    assert response.headers["location"] == "/operator/"
    assert (await client.get("/admin/bootstrap/not-a-token")).status_code == 404
    assert (await client.get("/admin/invite/not-a-token")).status_code == 404
