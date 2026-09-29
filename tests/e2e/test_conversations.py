"""One full conversation per language through wa-sim and the widget (10.3):
question, availability, booking link, late arrival, complaint, handoff, staff reply.

The demo tenant answers with the deterministic responder, so the flow is the same
on every run; the staff reply goes through the admin route and reaches the guest
on WhatsApp through the mock Graph, on the widget through the updates endpoint.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.integration.conftest import DemoStack
from tests.integration.test_admin import OWNER, csrf, login
from tests.integration.test_responder_conversations import CASES

from concierge.db.models import Conversation, Handoff, Message, MessageText
from concierge.demo.seed import DEMO_SECRETS
from concierge.demo.wa_sim import PayloadFactory, WaSimClient

pytestmark = pytest.mark.feature("F35")

LANGS = ["de", "en", "ru", "uk", "tr", "it"]
GUEST = "4915120007777"
ORIGIN = "http://localhost:8000"
AVAILABILITY = {
    "de": "Haben Sie vom 06.10.2026 bis 08.10.2026 ein Zimmer für 2 Erwachsene?",
    "en": "Do you have a room from 06.10.2026 to 08.10.2026 for 2 adults?",
    "ru": "Есть ли у вас номер с 06.10.2026 по 08.10.2026 на 2 человек?",
    "uk": "Чи є у вас номер з 06.10.2026 по 08.10.2026 на 2 особи?",
    "tr": "06.10.2026 - 08.10.2026 tarihleri için 2 kişi için boş oda var mı?",
    "it": "Avete una camera libera dal 06.10.2026 al 08.10.2026 per 2 persone?",
}
COMPLAINT = {
    "de": "Die Dusche ist kaputt und das Zimmer ist dreckig!",
    "en": "The shower is broken and the room is dirty!",
    "ru": "Душ сломан, и в номере грязно!",
    "uk": "Душ зламано, і в номері брудно!",
    "tr": "Duş bozuk ve oda kirli!",
    "it": "La doccia è rotta e la camera è sporca!",
}
STAFF_TEXT = "Guten Tag, hier ist die Rezeption. Wir kümmern uns sofort um die Dusche."


def case(lang: str, intent: str) -> tuple[str, str]:
    """(guest text, expected fact) for a responder intent in the language table."""
    text, _name, fact = next(row for row in CASES[lang] if row[1] == intent)
    return text, fact


async def outbound_texts(stack: DemoStack, conversation_id: str) -> list[str]:
    async with stack.services.ctx.db.scope() as session:
        rows = (
            await session.execute(
                select(Message, MessageText)
                .join(MessageText, MessageText.id == Message.id)
                .where(Message.conversation_id == conversation_id, Message.direction == "out")
                .order_by(Message.id)
            )
        ).all()
    return [t.text for _, t in rows]


async def latest_conversation(stack: DemoStack, channel: str) -> Conversation | None:
    async with stack.services.ctx.db.scope() as session:
        return await session.scalar(
            select(Conversation)
            .where(Conversation.tenant_id == stack.tenant.id, Conversation.channel == channel)
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )


async def conversation_of(stack: DemoStack, channel: str) -> Conversation:
    row = await latest_conversation(stack, channel)
    assert row is not None
    return row


async def handoff_of(stack: DemoStack, conversation_id: str) -> Handoff:
    async with stack.services.ctx.db.scope() as session:
        row = await session.scalar(
            select(Handoff)
            .where(Handoff.conversation_id == conversation_id)
            .order_by(Handoff.created_at.desc())
            .limit(1)
        )
    assert row is not None
    return row


async def staff_reply(stack: DemoStack, conversation_id: str) -> str:
    client = stack.client
    await login(client, OWNER)
    token = await csrf(client)
    response = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"csrf": token, "mode": "text", "text": STAFF_TEXT},
    )
    assert response.status_code == 303, response.text
    notice = response.headers["location"].split("notice=")[-1]
    await client.post("/admin/logout", data={"csrf": token})
    await stack.services.worker.drain()
    return notice


@pytest.mark.parametrize("lang", LANGS)
async def test_whatsapp_conversation_per_language(demo_stack: DemoStack, lang: str) -> None:
    stack = demo_stack
    factory = PayloadFactory(stack.tenant.phone_number_id or "")
    sim = WaSimClient(
        "http://testserver", DEMO_SECRETS["wa_app_secret"], transport=stack.client._transport
    )

    async def say(text: str) -> list[str]:
        existing = await latest_conversation(stack, "whatsapp")
        before = len(await outbound_texts(stack, existing.id)) if existing else 0
        assert (await sim.post(factory.text(GUEST, text))).status_code == 200
        await stack.services.worker.drain()
        texts = await outbound_texts(stack, (await conversation_of(stack, "whatsapp")).id)
        return texts[before:]

    question, fact = case(lang, "parking")
    replies = await say(question)
    conversation = await conversation_of(stack, "whatsapp")
    assert conversation.lang == lang
    assert any(fact in t for t in replies), replies

    replies = await say(AVAILABILITY[lang])
    assert any("120" in t for t in replies), replies  # manual inventory: the weekday price
    assert any("example.invalid/book" in t for t in replies), replies  # the booking link

    late, late_fact = case(lang, "late_arrival")
    replies = await say(late)
    assert any(late_fact in t for t in replies), replies

    replies = await say(COMPLAINT[lang])
    handoff = await handoff_of(stack, conversation.id)
    assert handoff.priority == "P1" and handoff.status == "open"
    conversation = await conversation_of(stack, "whatsapp")
    assert conversation.status == "needs_staff" and replies

    assert await staff_reply(stack, conversation.id) == "sent"
    sent = stack.state.messages_to(GUEST)
    assert sent and STAFF_TEXT in str(sent[-1])
    async with stack.services.ctx.db.scope() as session:
        staff_rows = list(
            (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == conversation.id, Message.intent == "S:reply"
                    )
                )
            ).scalars()
        )
    assert len(staff_rows) == 1 and staff_rows[0].status in {"sent", "delivered", "read"}


@pytest.mark.parametrize("lang", LANGS)
async def test_widget_conversation_per_language(demo_stack: DemoStack, lang: str) -> None:
    stack = demo_stack
    client = stack.client
    headers = {"origin": ORIGIN}
    session = await client.post(
        "/w/api/session", json={"key": "pk_demo", "lang": lang}, headers=headers
    )
    assert session.status_code == 200, session.text
    token = session.json()["session"]
    last_id = session.json()["last_id"]
    assert last_id.startswith("msg_")

    async def say(text: str) -> dict:  # type: ignore[type-arg]
        nonlocal last_id
        response = await client.post(
            "/w/api/message",
            json={"key": "pk_demo", "session": token, "text": text, "lang": lang},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        last_id = body["last_id"] or last_id
        return body

    question, fact = case(lang, "parking")
    body = await say(question)
    assert body["lang"] == lang and any(fact in m["text"] for m in body["messages"])

    body = await say(AVAILABILITY[lang])
    texts = " ".join(m["text"] for m in body["messages"])
    assert "120" in texts and "example.invalid/book" in texts

    late, late_fact = case(lang, "late_arrival")
    body = await say(late)
    assert any(late_fact in m["text"] for m in body["messages"])

    body = await say(COMPLAINT[lang])
    assert body["handoff"] is True
    conversation = await conversation_of(stack, "web")
    handoff = await handoff_of(stack, conversation.id)
    assert handoff.priority == "P1" and conversation.status == "needs_staff"

    assert await staff_reply(stack, conversation.id) == "sent"
    updates = await client.get(
        "/w/api/updates",
        params={"key": "pk_demo", "session": token, "after": last_id},
        headers=headers,
    )
    assert updates.status_code == 200, updates.text
    data = updates.json()
    assert data["handoff"] is True and data["status"] == "staff_active"
    assert [m["role"] for m in data["messages"]] == ["staff"]
    assert data["messages"][0]["text"] == STAFF_TEXT and data["last_id"] > last_id
    again = await client.get(
        "/w/api/updates",
        params={"key": "pk_demo", "session": token, "after": data["last_id"]},
        headers=headers,
    )
    assert again.json()["messages"] == []
    refused = await client.get(
        "/w/api/updates",
        params={"key": "pk_demo", "session": token},
        headers={"origin": "https://evil.invalid"},
    )
    assert refused.status_code == 403
