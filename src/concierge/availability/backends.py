"""Tool backends (F02, F07, F10, F11): the callables the agent tools run against.

Implements: F02, F07, F10, F11, F12, C15

Everything is re-authorised from the session: tenant, conversation and guest come
from the Brain, never from model arguments. ``set_eta`` exists only for a verified
reservation; the ``verify_reservation`` backend checks the local table first and then
the first configured PMS.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, time
from typing import Any, Protocol

from concierge.agent.responder import Slots
from concierge.agent.texts import GuestTexts
from concierge.agent.tools import ToolBackends, ToolError, ToolSession
from concierge.availability.booking import booking_link
from concierge.availability.chain import AvailabilityService
from concierge.availability.models import Occupancy, ReservationRecord, Stay
from concierge.availability.policy import asks_for_units, summarize, tool_result
from concierge.conversations.service import ConversationService
from concierge.db.models import Conversation, Guest, Tenant
from concierge.handoff.requests import RequestService
from concierge.handoff.service import HandoffService
from concierge.reservations.service import ReservationInput, ReservationService
from concierge.tenants.profile import TenantProfile

BackendsFactory = Callable[
    [Tenant, TenantProfile, Conversation, Guest, ToolSession], Awaitable[ToolBackends]
]
AvailabilityLookup = Callable[
    [Tenant, TenantProfile, Conversation, Slots, str, str], Awaitable[dict[str, Any] | None]
]


class VoiceHooks(Protocol):
    async def transfer(self, conversation_id: str, reason: str) -> bool: ...

    async def end_call(self, conversation_id: str, reason: str) -> None: ...


class ToolBackendBuilder:
    def __init__(
        self,
        *,
        availability: AvailabilityService,
        reservations: ReservationService,
        requests: RequestService,
        handoffs: HandoffService,
        conversations: ConversationService,
        texts: GuestTexts,
    ) -> None:
        self.availability = availability
        self.reservations = reservations
        self.requests = requests
        self.handoffs = handoffs
        self.conversations = conversations
        self.texts = texts
        self.voice_hooks: VoiceHooks | None = None
        """Set by the voice module: transfer and end-call backends for the phone channel."""

    async def __call__(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        conversation: Conversation,
        guest: Guest,
        session: ToolSession,
    ) -> ToolBackends:
        availability = self.availability
        reservations = self.reservations
        requests = self.requests
        handoffs = self.handoffs
        conversations = self.conversations
        texts = self.texts
        guest_text = session.guest_text
        state: dict[str, str | None] = {"reservation_id": conversation.verified_reservation_id}

        async def check_availability(
            arrival: date, departure: date, adults: int, children: int, room_type: str
        ) -> dict[str, Any]:
            stay = Stay(arrival, departure)
            occupancy = Occupancy(adults, children)
            answer = await availability.check(tenant, profile, stay, occupancy, room_type)
            return tool_result(
                answer,
                session.lang,
                texts,
                tz=profile.timezone,
                ask_units=asks_for_units(guest_text),
                booking_url=booking_link(profile, stay, occupancy, session.lang, room_type),
            )

        async def verify_reservation(
            reference: str, surname: str, arrival: date
        ) -> dict[str, Any] | None:
            found = await reservations.verify(tenant.id, reference, surname, arrival)
            if found is not None:
                await conversations.update(
                    tenant.id,
                    conversation.id,
                    verified_reservation_id=found["reservation_id"],
                )
                await conversations.merge_meta(
                    tenant.id, conversation.id, stay_date=str(found["arrival"])
                )
                await reservations.link_guest(tenant.id, found["reservation_id"], guest.id)
                state["reservation_id"] = str(found["reservation_id"])
                return found
            record = await availability.get_reservation(tenant, profile, reference)
            if record is None or record.arrival != arrival:
                return None
            if record.surname.strip().lower() != surname.strip().split()[-1].lower():
                return None
            # keep the PMS record locally so the verification outlives this turn (F11, F12)
            upserted = await reservations.upsert(
                tenant.id,
                _local_input(record, profile),
                actor="verify_reservation",
                phone_region=profile.contacts.phone_region,
            )
            row = upserted.reservation
            await conversations.update(tenant.id, conversation.id, verified_reservation_id=row.id)
            await conversations.merge_meta(
                tenant.id, conversation.id, stay_date=row.arrival.isoformat()
            )
            await reservations.link_guest(tenant.id, row.id, guest.id)
            state["reservation_id"] = str(row.id)
            return reservations.tool_view(row)

        async def get_booking_link(
            arrival: date, departure: date, adults: int, children: int, room_type: str
        ) -> str | None:
            return booking_link(
                profile,
                Stay(arrival, departure),
                Occupancy(adults, children),
                session.lang,
                room_type,
            )

        async def create_request(kind: str, details: str) -> str:
            row = await requests.create(
                tenant.id,
                conversation_id=conversation.id,
                kind=kind,
                details={"text": details, "lang": session.lang, "channel": session.channel},
                reservation_id=state["reservation_id"],
            )
            return row.id

        async def set_eta(eta: time) -> None:
            reservation_id = state["reservation_id"]
            if reservation_id is None:
                raise ToolError(
                    "arrival time cannot be stored without a verified reservation; ask for booking reference, surname and arrival date and call verify_reservation first"
                )
            await reservations.set_eta(tenant.id, reservation_id, eta, actor=f"guest:{guest.id}")
            await requests.record_eta(
                tenant.id,
                conversation_id=conversation.id,
                reservation_id=reservation_id,
                eta=eta,
            )

        async def create_handoff(priority: str, reason: str) -> str:
            created = await handoffs.create(
                tenant.id,
                conversation.id,
                priority=priority,
                trigger="tool",
                reason=reason,
                profile=profile,
            )
            session.events.append(f"handoff_id:{created.id}")
            return created.id

        async def request_callback(reason: str) -> str:
            phone = conversations.guest_phone(guest)
            row = await requests.create_callback(
                tenant.id,
                conversation_id=conversation.id,
                phone=phone,
                reason=reason,
                lang=session.lang,
            )
            return row.id

        async def set_language(lang: str) -> None:
            await conversations.update(tenant.id, conversation.id, lang=lang)

        voice = self.voice_hooks if session.channel == "voice" else None

        async def transfer_to_human(reason: str) -> bool:
            return voice is not None and await voice.transfer(conversation.id, reason)

        async def end_call(reason: str) -> None:
            if voice is not None:
                await voice.end_call(conversation.id, reason)

        return ToolBackends(
            check_availability=check_availability,
            verify_reservation=verify_reservation,
            booking_link=get_booking_link,
            create_request=create_request,
            set_eta=set_eta,
            create_handoff=create_handoff,
            request_callback=request_callback,
            set_language=set_language,
            transfer_to_human=transfer_to_human if voice is not None else None,
            end_call=end_call if voice is not None else None,
        )

    async def responder_lookup(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        conversation: Conversation,
        slots: Slots,
        lang: str,
        guest_text: str,
    ) -> dict[str, Any] | None:
        """The deterministic responder's availability summary (F04 + F07)."""
        if not slots.complete or slots.arrival is None or slots.departure is None:
            return None
        if slots.departure <= slots.arrival or (slots.departure - slots.arrival).days > 30:
            return None
        stay = Stay(slots.arrival, slots.departure)
        occupancy = Occupancy(slots.adults or 1, slots.children)
        answer = await self.availability.check(tenant, profile, stay, occupancy)
        summary = summarize(
            answer, lang, self.texts, tz=profile.timezone, ask_units=asks_for_units(guest_text)
        ).as_dict()
        summary["booking_url"] = booking_link(profile, stay, occupancy, lang)
        summary["window"] = profile.handoff.response_window_text.get(lang, "")
        return summary


_PMS_STATUS = {
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "checked_in": "checked_in",
    "in_house": "checked_in",
    "checked_out": "checked_out",
}


def _local_input(record: ReservationRecord, profile: TenantProfile) -> ReservationInput:
    """The local reservation row for a record the PMS returned (source ``pms``)."""
    status = _PMS_STATUS.get(record.status.strip().lower().replace("-", "_"), "booked")
    lang = record.lang.strip().lower()[:2]
    return ReservationInput(
        res_id=record.res_id.replace(" ", "") or record.confirmation_number.replace(" ", ""),
        guest_name=f"{record.first_name} {record.surname}".strip(),
        phone=record.phone or None,
        email=record.email or None,
        lang=lang if len(lang) == 2 and lang.isalpha() else profile.default_language,
        arrival=record.arrival,
        departure=record.departure,
        room_type=record.room_type or None,
        rate_plan=record.rate_plan or None,
        source=f"pms:{record.source}"[:32] if record.source else "pms",
        status=status,
        adults=min(max(record.adults, 1), 20),
        children=min(max(record.children, 0), 20),
        meta={"confirmation_number": record.confirmation_number},
    )
