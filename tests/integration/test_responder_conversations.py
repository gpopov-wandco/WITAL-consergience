"""Deterministic responder (15 intents in six languages), conversations and handoff services."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.unit.test_profile import SAMPLE

from concierge.agent.responder import (
    Responder,
    ResponderContext,
    Slots,
    extract_slots,
    intent_table,
)
from concierge.agent.texts import guest_texts
from concierge.clock import FakeClock
from concierge.conversations.service import ConversationService, Inbound
from concierge.db.models import Guest, Message, MessageText
from concierge.db.session import Database
from concierge.handoff.service import HandoffService
from concierge.kb.fixtures import FIXTURES_DIR, import_kb_file
from concierge.kb.store import KbService
from concierge.security.crypto import Keyring
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

pytestmark = pytest.mark.feature("F04")

CASES: dict[str, list[tuple[str, str, str]]] = {
    "de": [
        ("Ab wann kann ich einchecken?", "checkin", "15:00"),
        ("Bis wann muss ich auschecken?", "checkout", "11:00"),
        ("Wie lange ist die Rezeption besetzt?", "hours", "22:00"),
        ("Gibt es einen Parkplatz?", "parking", "18"),
        ("Wann gibt es Frühstück?", "breakfast", "16"),
        ("Darf ich meinen Hund mitbringen?", "pets", "15"),
        ("Wie komme ich vom Flughafen zum Hotel?", "directions", "S8"),
        ("Wir kommen erst spät nach 22 Uhr an", "late_arrival", "Schlüsselsafe"),
        ("Kann ich mein Gepäck abgeben?", "luggage", "Gepäckraum"),
        ("Gibt es eine Kurtaxe?", "city_tax", "2 EUR"),
        ("Kann ich kostenlos stornieren?", "cancellation", "18:00"),
        ("Kann ich mit Kreditkarte bezahlen?", "payment", "Visa"),
        ("Wie ist Ihre Telefonnummer?", "contact", "+49 69 000000"),
        ("Ich brauche eine Rechnung auf meine Firma", "invoice", "Umsatzsteuer"),
        ("Ist das Hotel barrierefrei?", "accessibility", "Aufzug"),
    ],
    "en": [
        ("What time is check-in?", "checkin", "15:00"),
        ("When do I have to check out?", "checkout", "11:00"),
        ("What are the reception hours?", "hours", "22:00"),
        ("Is there parking at the hotel?", "parking", "18"),
        ("When is breakfast served?", "breakfast", "16"),
        ("Can I bring my dog?", "pets", "15"),
        ("How do I get from the airport to the hotel?", "directions", "S8"),
        ("We will arrive late, after 22:00", "late_arrival", "key safe"),
        ("Can I store my luggage?", "luggage", "luggage room"),
        ("Is there a city tax?", "city_tax", "EUR 2"),
        ("Can I cancel free of charge?", "cancellation", "18:00"),
        ("Can I pay by credit card?", "payment", "Visa"),
        ("What is your phone number?", "contact", "+49 69 000000"),
        ("I need an invoice for my company", "invoice", "VAT"),
        ("Is the hotel accessible by wheelchair?", "accessibility", "lift"),
    ],
    "ru": [
        ("Во сколько заезд?", "checkin", "15:00"),
        ("До скольки выезд?", "checkout", "11:00"),
        ("Часы работы ресепшен?", "hours", "22:00"),
        ("Есть ли парковка?", "parking", "18"),
        ("Во сколько завтрак?", "breakfast", "16"),
        ("Можно с собакой?", "pets", "15"),
        ("Как добраться из аэропорта?", "directions", "S8"),
        ("Мы приедем поздно, после 22:00", "late_arrival", "сейфе"),
        ("Можно оставить багаж?", "luggage", "камере"),
        ("Есть ли туристический сбор?", "city_tax", "2 EUR"),
        ("Можно ли отменить бронирование бесплатно?", "cancellation", "18:00"),
        ("Можно оплатить картой?", "payment", "Visa"),
        ("Какой у вас телефон?", "contact", "+49 69 000000"),
        ("Мне нужен счёт на компанию", "invoice", "НДС"),
        ("Есть ли доступная среда для инвалидной коляски?", "accessibility", "лифт"),
    ],
    "uk": [
        ("О котрій годині заїзд?", "checkin", "15:00"),
        ("До котрої години виїзд?", "checkout", "11:00"),
        ("Години роботи рецепції?", "hours", "22:00"),
        ("Чи є паркування?", "parking", "18"),
        ("О котрій сніданок?", "breakfast", "16"),
        ("Чи можна з собакою?", "pets", "15"),
        ("Як дістатися з аеропорту?", "directions", "S8"),
        ("Ми приїдемо пізно, після 22:00", "late_arrival", "сейфі"),
        ("Чи можна залишити багаж?", "luggage", "камері"),
        ("Чи є туристичний збір?", "city_tax", "2 EUR"),
        ("Чи можна скасувати бронювання безкоштовно?", "cancellation", "18:00"),
        ("Чи можна оплатити карткою?", "payment", "Visa"),
        ("Який у вас телефон?", "contact", "+49 69 000000"),
        ("Мені потрібен рахунок на компанію", "invoice", "ПДВ"),
        ("Чи є доступність для інвалідного візка?", "accessibility", "ліфт"),
    ],
    "tr": [
        ("Giriş saati kaç?", "checkin", "15:00"),
        ("Çıkış saati kaç?", "checkout", "11:00"),
        ("Resepsiyon saat kaça kadar açık?", "hours", "22:00"),
        ("Otopark var mı?", "parking", "18"),
        ("Kahvaltı saat kaçta?", "breakfast", "16"),
        ("Köpek getirebilir miyim?", "pets", "15"),
        ("Havalimanından otele nasıl gelirim?", "directions", "S8"),
        ("Geç geleceğiz, 22:00'den sonra", "late_arrival", "anahtar kasası"),
        ("Bagajımı bırakabilir miyim?", "luggage", "bagaj odası"),
        ("Turizm vergisi var mı?", "city_tax", "2 EUR"),
        ("Ücretsiz iptal edebilir miyim?", "cancellation", "18:00"),
        ("Kredi kartıyla ödeyebilir miyim?", "payment", "Visa"),
        ("Telefon numaranız nedir?", "contact", "+49 69 000000"),
        ("Şirketim için fatura istiyorum", "invoice", "KDV"),
        ("Otel tekerlekli sandalye için erişilebilir mi?", "accessibility", "asansör"),
    ],
    "it": [
        ("A che ora è il check-in?", "checkin", "15:00"),
        ("Entro che ora devo fare il check-out?", "checkout", "11:00"),
        ("La reception fino a che ora è aperta?", "hours", "22:00"),
        ("C'è un parcheggio?", "parking", "18"),
        ("A che ora è la colazione?", "breakfast", "16"),
        ("Posso portare il mio cane?", "pets", "15"),
        ("Come arrivo dall'aeroporto?", "directions", "S8"),
        ("Arriveremo tardi, dopo le 22", "late_arrival", "cassetta delle chiavi"),
        ("Posso lasciare i bagagli?", "luggage", "deposito"),
        ("C'è una tassa di soggiorno?", "city_tax", "2 EUR"),
        ("Posso cancellare gratuitamente?", "cancellation", "18:00"),
        ("Posso pagare con carta di credito?", "payment", "Visa"),
        ("Qual è il vostro numero di telefono?", "contact", "+49 69 000000"),
        ("Ho bisogno di una fattura per la mia azienda", "invoice", "IVA"),
        ("L'hotel è accessibile in sedia a rotelle?", "accessibility", "ascensore"),
    ],
}


@pytest.fixture
async def seeded(db: Database, clock: FakeClock) -> tuple[Responder, str]:
    tenant = await TenantService(db, clock).create(TenantProfile.model_validate(SAMPLE), actor="t")
    kb = KbService(db, clock)
    await import_kb_file(kb, tenant.id, FIXTURES_DIR / "hotel_beispiel.yaml", approve=True)
    return Responder(kb, guest_texts()), str(tenant.id)


def _ctx(tenant_id: str, lang: str, **kw: object) -> ResponderContext:
    base: dict[str, object] = {
        "tenant_id": tenant_id,
        "lang": lang,
        "channel": "whatsapp",
        "verified": False,
        "stay_date": None,
        "hotel_phone": "+49 69 000000",
    }
    base.update(kw)
    return ResponderContext(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("lang", "text", "intent", "fragment"),
    [(lang, *case) for lang, cases in CASES.items() for case in cases],
)
async def test_fifteen_intents_in_six_languages(
    seeded: tuple[Responder, str], lang: str, text: str, intent: str, fragment: str
) -> None:
    responder, tenant_id = seeded
    reply, views, _ = await responder.respond(text, _ctx(tenant_id, lang))
    assert reply.intent == f"A:{intent}", (lang, text, reply.intent)
    assert fragment in reply.reply_text, (lang, text, reply.reply_text)
    assert reply.language == lang and not reply.handoff.needed
    assert views and views[0].lang == lang


async def test_wifi_requires_verification(seeded: tuple[Responder, str]) -> None:
    responder, tenant_id = seeded
    reply, _, _ = await responder.respond("Wie lautet das WLAN Passwort?", _ctx(tenant_id, "de"))
    assert reply.intent == "Q:wifi" and "Buchungsnummer" in reply.reply_text
    reply, views, _ = await responder.respond(
        "Wie lautet das WLAN Passwort?", _ctx(tenant_id, "de", verified=True)
    )
    assert (
        reply.intent == "A:wifi"
        and "beispiel2026" in reply.reply_text
        and views[0].sensitivity == "verified_guest_only"
    )


async def test_availability_slot_filling_and_wording(seeded: tuple[Responder, str]) -> None:
    responder, tenant_id = seeded
    reply, _, slots = await responder.respond(
        "Haben Sie noch ein Zimmer frei?", _ctx(tenant_id, "de")
    )
    assert reply.intent == "Q:availability" and "Anreise" in reply.reply_text
    reply, _, slots = await responder.respond(
        "Vom 12.10.2026 bis 14.10.2026", _ctx(tenant_id, "de", previous_slots=slots)
    )
    assert reply.intent == "Q:availability" and "Erwachsene" in reply.reply_text
    assert slots is not None and slots.arrival == date(2026, 10, 12)
    reply, _, slots = await responder.respond(
        "2 Erwachsene und 1 Kind", _ctx(tenant_id, "de", previous_slots=slots)
    )
    assert reply.intent == "H:availability" and reply.handoff.priority == "P2"
    summary = {"source": "manual", "summary": "1 Doppelzimmer frei", "updated_date": "28.09.2026"}
    reply, _, _ = await responder.respond(
        "2 Erwachsene und 1 Kind",
        _ctx(tenant_id, "de", previous_slots=slots, availability_summary=summary),
    )
    assert (
        reply.intent == "P:availability"
        and "Kalender" in reply.reply_text
        and "unverbindlich" in reply.reply_text
    )
    assert [b.id for b in reply.buttons] == ["book", "person"]
    fresh = {"source": "live_pms", "summary": "2 rooms available"}
    reply, _, _ = await responder.respond(
        "Do you have a room from 2026-10-12 to 2026-10-14 for 2 adults?",
        _ctx(tenant_id, "en", availability_summary=fresh),
    )
    assert reply.reply_text.startswith("The booking system currently shows")


def test_slot_extraction_edge_cases() -> None:
    slots = extract_slots("vom 3.10.2026 bis 5.10.2026 für 3 Personen mit 2 Kindern")
    assert slots == Slots(date(2026, 10, 3), date(2026, 10, 5), 3, 2)
    partial = extract_slots("am 2026-11-01 für 2 adults")
    assert partial.arrival == date(2026, 11, 1) and partial.departure is None
    completed = extract_slots("bis 2026-11-03", partial)
    assert completed.departure == date(2026, 11, 3) and completed.adults == 2
    assert extract_slots("am 31.02.2026").arrival is None


async def test_group_and_unknown(seeded: tuple[Responder, str]) -> None:
    responder, tenant_id = seeded
    reply, _, _ = await responder.respond(
        "Wir sind eine Gruppe mit 8 Zimmern", _ctx(tenant_id, "de")
    )
    assert reply.intent == "H:group" and reply.handoff.priority == "P2"
    reply, _, _ = await responder.respond("Wie wird das Wetter morgen?", _ctx(tenant_id, "de"))
    assert reply.intent == "H:kb_miss" and reply.handoff.needed and reply.handoff.priority == "P3"
    assert intent_table().version


async def test_directions_pick_origin(seeded: tuple[Responder, str]) -> None:
    responder, tenant_id = seeded
    reply, views, _ = await responder.respond(
        "Wie komme ich mit dem Auto zu Ihnen?", _ctx(tenant_id, "de")
    )
    assert views[0].key == "directions.car" and "A5" in reply.reply_text
    reply, views, _ = await responder.respond("Come arrivo dalla stazione?", _ctx(tenant_id, "it"))
    assert views[0].key == "directions.hbf"


# ------------------------------------------------------------- conversations
@pytest.fixture
def conversations(db: Database, keyring: Keyring, clock: FakeClock) -> ConversationService:
    return ConversationService(db, keyring, clock)


@pytest.fixture
async def tenant_id(db: Database, clock: FakeClock) -> str:
    tenant = await TenantService(db, clock).create(TenantProfile.model_validate(SAMPLE), actor="t")
    return str(tenant.id)


async def test_guest_identity_and_encryption(
    conversations: ConversationService, tenant_id: str, db: Database, keyring: Keyring
) -> None:
    guest = await conversations.get_or_create_guest(
        tenant_id,
        "whatsapp",
        "wa-user-1",
        phone="+49 151 12345678",
        name="Anna Muster",
        wa_user_id="wa-user-1",
    )
    again = await conversations.get_or_create_guest(tenant_id, "whatsapp", "wa-user-1")
    assert again.id == guest.id
    async with db.scope() as session:
        row = (await session.execute(select(Guest))).scalar_one()
    assert row.phone_enc is not None and b"151" not in row.phone_enc
    assert row.phone_bidx == keyring.blind_index("+4915112345678", tenant_id, "phone")
    assert conversations.guest_phone(row) == "+49 151 12345678"
    other = await conversations.get_or_create_guest(tenant_id, "web", "wa-user-1")
    assert other.id != guest.id


async def test_conversation_reuse_messages_history_and_status(
    conversations: ConversationService, tenant_id: str, db: Database, clock: FakeClock
) -> None:
    guest = await conversations.get_or_create_guest(tenant_id, "whatsapp", "u1")
    conversation = await conversations.open_conversation(tenant_id, guest.id, "whatsapp", lang="de")
    assert (
        await conversations.open_conversation(tenant_id, guest.id, "whatsapp", lang="de")
    ).id == conversation.id
    inbound = Inbound(channel="whatsapp", channel_key="u1", text="Hallo", external_id="wamid.1")
    first = await conversations.record_inbound(
        tenant_id, conversation.id, inbound, lang="de", retention_days=30
    )
    clock.advance(timedelta(seconds=1))
    await conversations.record_outbound(
        tenant_id, conversation.id, "Guten Tag", lang="de", intent="A:greeting", retention_days=30
    )
    clock.advance(timedelta(seconds=1))
    await conversations.record_inbound(
        tenant_id,
        conversation.id,
        Inbound(channel="whatsapp", channel_key="u1", text="Parken?", external_id="wamid.2"),
        lang="de",
        retention_days=30,
    )
    history = await conversations.history(tenant_id, conversation.id)
    assert history == [("user", "Hallo"), ("assistant", "Guten Tag"), ("user", "Parken?")]
    async with db.scope() as session:
        text_row = await session.get(MessageText, first.id)
        assert text_row is not None and text_row.expires_at == first.created_at + timedelta(days=30)
        message_count = len((await session.execute(select(Message))).scalars().all())
    assert message_count == 3
    updated = await conversations.update(
        tenant_id, conversation.id, status="needs_staff", priority="P2"
    )
    assert updated.status == "needs_staff"
    await conversations.mark_disclosed(tenant_id, conversation.id)
    assert (await conversations.get(tenant_id, conversation.id)).ai_disclosed_at == clock.now()
    meta = await conversations.merge_meta(tenant_id, conversation.id, slots={"adults": 2})
    assert meta == {"slots": {"adults": 2}}


async def test_status_rank_and_verify_attempts(
    conversations: ConversationService, tenant_id: str, clock: FakeClock
) -> None:
    guest = await conversations.get_or_create_guest(tenant_id, "whatsapp", "u1")
    conversation = await conversations.open_conversation(tenant_id, guest.id, "whatsapp", lang="de")
    sent = await conversations.record_outbound(
        tenant_id, conversation.id, "x", lang="de", retention_days=30
    )
    await conversations.update(tenant_id, conversation.id)
    from concierge.db.models import Message as M

    async with conversations.db.scope() as session:
        row = await session.get(M, sent.id)
        assert row is not None
        row.wamid = "wamid.out.1"
        await session.commit()
    assert (
        await conversations.apply_status(tenant_id, "wamid.out.1", "delivered")
    ).status == "delivered"
    assert (
        await conversations.apply_status(tenant_id, "wamid.out.1", "sent")
    ).status == "delivered"
    assert (await conversations.apply_status(tenant_id, "wamid.out.1", "read")).status == "read"
    assert (await conversations.apply_status(tenant_id, "wamid.out.1", "failed")).status == "failed"
    assert (await conversations.apply_status(tenant_id, "wamid.out.1", "read")).status == "failed"
    assert await conversations.apply_status(tenant_id, "wamid.unknown", "read") is None
    assert await conversations.verify_attempt_state(tenant_id, conversation.id) == 0
    await conversations.set_verify_attempts(tenant_id, conversation.id, 3)
    assert await conversations.verify_attempt_state(tenant_id, conversation.id) == 3
    clock.advance(timedelta(hours=1, seconds=1))
    assert await conversations.verify_attempt_state(tenant_id, conversation.id) == 0


# ------------------------------------------------------------------ handoff
async def test_handoff_sla_dedupe_and_timers(
    db: Database, clock: FakeClock, tenant_id: str, conversations: ConversationService
) -> None:
    profile = TenantProfile.model_validate(SAMPLE)
    service = HandoffService(db, clock)
    guest = await conversations.get_or_create_guest(tenant_id, "whatsapp", "u1")
    conversation = await conversations.open_conversation(tenant_id, guest.id, "whatsapp", lang="de")
    clock.set(datetime(2026, 9, 29, 10, 0, tzinfo=UTC))  # 12:00 Berlin, staffed
    created = await service.create(
        tenant_id, conversation.id, priority="P2", trigger="kb_miss", reason="pool", profile=profile
    )
    assert created.created and created.sla_due_at == clock.now() + timedelta(minutes=120)
    assert (await conversations.get(tenant_id, conversation.id)).status == "needs_staff"
    upgraded = await service.create(
        tenant_id,
        conversation.id,
        priority="P1",
        trigger="complaint",
        reason="cold room",
        profile=profile,
    )
    assert not upgraded.created and upgraded.priority == "P1" and upgraded.id == created.id
    assert upgraded.sla_due_at == clock.now() + timedelta(minutes=10)
    lower = await service.create(
        tenant_id, conversation.id, priority="P3", trigger="x", reason="y", profile=profile
    )
    assert lower.priority == "P1"
    clock.advance(timedelta(minutes=11))
    changed = await service.check_timers(tenant_id, profile)
    assert len(changed) == 1 and changed[0].sla_breached and changed[0].status == "escalated"
    acked = await service.acknowledge(tenant_id, created.id, user_id="usr_1")
    assert (
        acked.status == "acknowledged"
        and (await conversations.get(tenant_id, conversation.id)).status == "staff_active"
    )
    resolved = await service.resolve(tenant_id, created.id, user_id="usr_1")
    assert (
        resolved.status == "resolved"
        and (await conversations.get(tenant_id, conversation.id)).status == "bot_handling"
    )
    # outside staffed hours a P2 timer starts at the next staffed start
    clock.set(datetime(2026, 9, 29, 21, 0, tzinfo=UTC))  # 23:00 Berlin
    night = await service.create(
        tenant_id, conversation.id, priority="P2", trigger="x", reason="late", profile=profile
    )
    assert night.sla_due_at == datetime(2026, 9, 30, 5, 0, tzinfo=UTC) + timedelta(minutes=120)
    p0 = await service.create(
        tenant_id,
        conversation.id,
        priority="P0",
        trigger="emergency",
        reason="fire",
        profile=profile,
    )
    assert p0.priority == "P0" and p0.sla_due_at == clock.now()
    repeats = await service.check_timers(tenant_id, profile)
    assert repeats and repeats[0].escalation_level >= 1
