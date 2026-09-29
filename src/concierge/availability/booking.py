"""Booking handoff (F10): the tenant's booking link or an inquiry that becomes a handoff.

Implements: F10, C8, C17, F17

The booking URL template comes from the tenant profile; every placeholder value is
URL-escaped. An inquiry (name, contact, dates, guests, wish, consent) becomes a
``guest_request`` row and a P2 handoff; card data is refused before storage; the same
inquiry sent twice is recognised by its idempotency key. A PMS inquiry is created only
when the tenant allows PMS writes and the source supports non-binding inquiries
(Beds24, Mews); apaleo never, because its bookings are confirmed reservations.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from concierge import audit
from concierge.agent.guardrails import Guardrails
from concierge.availability.chain import AvailabilityService
from concierge.availability.models import (
    AvailabilityError,
    InquiryRequest,
    NotSupported,
    Occupancy,
    Stay,
)
from concierge.clock import Clock
from concierge.db.models import GuestRequest, Tenant
from concierge.db.session import Database
from concierge.handoff.service import HandoffService
from concierge.log import get_logger
from concierge.security.crypto import Keyring
from concierge.tenants.profile import TenantProfile

log = get_logger(__name__)

INQUIRY_WRITE_SOURCES: frozenset[str] = frozenset({"beds24", "mews"})
MEWS_PARAMS = ("mewsStart", "mewsEnd", "mewsAdultCount", "mewsChildCount")


def render_booking_url(
    template: str, stay: Stay, occupancy: Occupancy, lang: str, room: str = ""
) -> str:
    values = {
        "arrival": stay.arrival.isoformat(),
        "departure": stay.departure.isoformat(),
        "adults": str(occupancy.adults),
        "children": str(occupancy.children),
        "lang": lang,
        "room": room,
    }
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", quote(value, safe=""))
    return out


def mews_booking_url(
    base: str, stay: Stay, occupancy: Occupancy, lang: str, currency: str = "EUR"
) -> str:
    """Mews booking engine deep link (parameter names VERIFY per client)."""
    parts = urlsplit(base)
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in MEWS_PARAMS
    ]
    query += [
        ("mewsStart", stay.arrival.isoformat()),
        ("mewsEnd", stay.departure.isoformat()),
        ("mewsAdultCount", str(occupancy.adults)),
        ("mewsChildCount", str(occupancy.children)),
        ("language", lang),
        ("currency", currency),
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def booking_link(
    profile: TenantProfile, stay: Stay, occupancy: Occupancy, lang: str, room: str = ""
) -> str | None:
    template = profile.availability.booking_url
    if not template:
        return None
    if "{" not in template and "mews" in urlsplit(template).netloc.lower():
        return mews_booking_url(template, stay, occupancy, lang, profile.availability.currency)
    return render_booking_url(template, stay, occupancy, lang, room)


class InquiryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InquiryForm:
    name: str
    email: str
    phone: str
    stay: Stay
    occupancy: Occupancy
    wish: str
    consent: bool
    lang: str
    room_type: str = ""


InquiryNotifier = Callable[[Tenant, TenantProfile, "InquiryForm", str], Awaitable[bool]]


@dataclass
class InquiryResult:
    request_id: str
    handoff_id: str | None
    duplicate: bool
    pms_ref: str | None = None


class InquiryService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        keyring: Keyring,
        handoffs: HandoffService,
        availability: AvailabilityService,
        guardrails: Guardrails | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.keyring = keyring
        self.handoffs = handoffs
        self.availability = availability
        self.guardrails = guardrails or Guardrails()
        self.notify: InquiryNotifier | None = None
        """Queues the inquiry e-mail to the hotel; returns True when queued (F18)."""

    @staticmethod
    def idempotency_key(tenant_id: str, form: InquiryForm) -> str:
        raw = "|".join(
            [
                tenant_id,
                form.stay.arrival.isoformat(),
                form.stay.departure.isoformat(),
                str(form.occupancy.adults),
                str(form.occupancy.children),
                form.email.strip().lower(),
                "".join(ch for ch in form.phone if ch.isdigit()),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def create(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        form: InquiryForm,
        *,
        conversation_id: str | None,
        actor: str = "guest",
    ) -> InquiryResult:
        if not form.consent:
            raise InquiryError("consent_required", "the guest must agree to be contacted")
        if not form.name.strip():
            raise InquiryError("name_required", "a name is required")
        if not form.email.strip() and not form.phone.strip():
            raise InquiryError(
                "contact_required", "an e-mail address or a WhatsApp number is required"
            )
        for text in (form.wish, form.name, form.email):  # the phone field is a phone number
            outcome = self.guardrails.check(text, form.lang)
            if outcome.redactions:
                raise InquiryError("card_data", "card or ID data is not accepted in an inquiry")
        key = self.idempotency_key(tenant.id, form)
        async with self.db.tenant_scope(tenant.id) as session:
            existing = await session.scalar(
                select(GuestRequest).where(
                    GuestRequest.tenant_id == tenant.id,
                    GuestRequest.kind == "booking_inquiry",
                    GuestRequest.idempotency_key == key,
                )
            )
            if existing is not None:
                return InquiryResult(
                    existing.id,
                    str(existing.details.get("handoff_id") or "") or None,
                    True,
                    str(existing.details.get("pms_ref") or "") or None,
                )
        contact = json.dumps(
            {"name": form.name.strip(), "email": form.email.strip(), "phone": form.phone.strip()},
            ensure_ascii=False,
        )
        contact_enc = base64.b64encode(
            self.keyring.encrypt_str(contact, tenant.id, "inquiry_contact")
        ).decode("ascii")
        details: dict[str, Any] = {
            "arrival": form.stay.arrival.isoformat(),
            "departure": form.stay.departure.isoformat(),
            "adults": form.occupancy.adults,
            "children": form.occupancy.children,
            "room_type": form.room_type,
            "wish": form.wish.strip()[:600],
            "lang": form.lang,
            "consent_at": self.clock.now().isoformat(),
            "contact_enc": contact_enc,
            "contact_ref": self.keyring.blind_index(
                form.email.strip() or form.phone.strip(), tenant.id, "contact"
            ),
            "email_pending": True,
        }
        handoff_id: str | None = None
        if conversation_id:
            created = await self.handoffs.create(
                tenant.id,
                conversation_id,
                priority="P2",
                trigger="booking_inquiry",
                reason="booking inquiry",
                profile=profile,
                summary=(
                    f"Booking inquiry {form.stay.arrival.isoformat()} to "
                    f"{form.stay.departure.isoformat()}, {form.occupancy.adults} adults, "
                    f"{form.occupancy.children} children, lang {form.lang}: {form.wish.strip()[:200]}"
                ),
            )
            handoff_id = created.id
            details["handoff_id"] = handoff_id
        pms_ref = await self._pms_inquiry(tenant, profile, form, key)
        if pms_ref:
            details["pms_ref"] = pms_ref
        queued = False
        if self.notify is not None:
            queued = await self.notify(tenant, profile, form, key)
        details["email_pending"] = not queued
        async with self.db.tenant_scope(tenant.id) as session:
            row = GuestRequest(
                tenant_id=tenant.id,
                conversation_id=conversation_id,
                kind="booking_inquiry",
                details=details,
                idempotency_key=key,
            )
            session.add(row)
            audit.record(
                session,
                actor=actor,
                action="inquiry.create",
                target=key[:16],
                tenant_id=tenant.id,
                details={"handoff_id": handoff_id, "pms_ref": pms_ref or ""},
            )
            await session.commit()
            await session.refresh(row)
            return InquiryResult(row.id, handoff_id, False, pms_ref)

    async def _pms_inquiry(
        self, tenant: Tenant, profile: TenantProfile, form: InquiryForm, key: str
    ) -> str | None:
        config = profile.availability
        if not config.allow_pms_write:
            return None
        source = next(
            (s for s in config.chain if s in {"beds24", "apaleo", "mews", "smoobu", "ibelsa"}), None
        )
        if source is None or source not in INQUIRY_WRITE_SOURCES:
            return None
        adapter = await self.availability.factory(source, tenant, profile)
        if adapter is None:
            return None
        first, _, last = form.name.strip().rpartition(" ")
        request = InquiryRequest(
            stay=form.stay,
            occupancy=form.occupancy,
            room_type=form.room_type,
            first_name=first or "",
            last_name=last or form.name.strip(),
            email=form.email.strip(),
            phone=form.phone.strip(),
            lang=form.lang,
            wish=form.wish.strip(),
            idempotency_key=key,
        )
        try:
            return await adapter.create_inquiry(request)
        except NotSupported:
            return None
        except AvailabilityError as exc:
            log.warning(
                "inquiry.pms_failed", tenant=tenant.id, source=source, error=type(exc).__name__
            )
            return None

    def contact_of(self, tenant_id: str, request: GuestRequest) -> dict[str, str]:
        blob = request.details.get("contact_enc")
        if not blob:
            return {}
        data = json.loads(
            self.keyring.decrypt_str(base64.b64decode(blob), tenant_id, "inquiry_contact")
        )
        return {str(k): str(v) for k, v in data.items()}
