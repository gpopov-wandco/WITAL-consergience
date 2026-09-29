"""Staff alerts (F17, F18): reach the hotel team when a handoff needs a person.

Implements: F17, F18, F14, C9

A new handoff alerts staff by WhatsApp (``gc_staff_alert`` to opted-in contact
numbers) and by e-mail. Inside staffed hours every priority is delivered; outside
them only P0 reaches the on-call contacts by WhatsApp, the rest goes by e-mail and
waits for the next staffed morning. A P0 that stays unacknowledged is repeated every
``p0_repeat_min`` minutes to the next contact. Alert text never promises a reply time
that the tenant's SLA configuration does not state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from concierge.channels.whatsapp.outbox import OutboxService
from concierge.clock import Clock, to_local
from concierge.db.models import Handoff, Tenant
from concierge.log import get_logger
from concierge.notify.outbox import EmailOutbox
from concierge.reservations.service import to_e164
from concierge.templates.service import TemplateRefused, TemplateService
from concierge.tenants.profile import HandoffContact, TenantProfile

log = get_logger(__name__)


@dataclass
class AlertRecord:
    handoff_id: str
    priority: str
    channel: str
    contact: str
    outcome: str


@dataclass
class StaffAlerts:
    clock: Clock
    templates: TemplateService
    wa_outbox: OutboxService
    email_outbox: EmailOutbox
    public_base_url: str
    sent: list[AlertRecord] = field(default_factory=list)

    def contacts_for(
        self, profile: TenantProfile, priority: str, now: datetime, *, level: int = 0
    ) -> list[HandoffContact]:
        contacts = list(profile.handoff.contacts)
        if not contacts:
            return []
        staffed = profile.is_staffed(now)
        if priority == "P0":
            pool = contacts if staffed else [c for c in contacts if c.on_call] or contacts
            if level > 0:
                return [pool[level % len(pool)]]
            return pool
        if not staffed:
            return []
        return contacts

    async def on_handoff(
        self, tenant: Tenant, profile: TenantProfile, handoff: Handoff, *, level: int = 0
    ) -> list[AlertRecord]:
        now = self.clock.now()
        records: list[AlertRecord] = []
        lang = profile.default_language
        link = f"{self.public_base_url}/admin/conversations/{handoff.conversation_id}"
        summary = (handoff.summary or handoff.reason or handoff.trigger)[:120]
        local = to_local(now, profile.timezone).strftime("%H:%M")
        alert_text = f"{summary} ({local})"
        wa_contacts = self.contacts_for(profile, handoff.priority, now, level=level)
        for contact in wa_contacts:
            number = (
                to_e164(contact.whatsapp, profile.contacts.phone_region)
                if contact.whatsapp
                else None
            )
            if number is None:
                continue
            outcome = await self._whatsapp(
                tenant, handoff, lang, number, alert_text, link, level=level
            )
            records.append(
                AlertRecord(handoff.id, handoff.priority, "whatsapp", contact.name, outcome)
            )
        # e-mail goes to every contact with an address, at every priority
        for contact in profile.handoff.contacts:
            if not contact.email:
                continue
            outcome = await self._email(
                tenant, profile, handoff, contact.email, alert_text, link, level=level
            )
            records.append(
                AlertRecord(handoff.id, handoff.priority, "email", contact.name, outcome)
            )
        self.sent.extend(records)
        return records

    async def _whatsapp(
        self,
        tenant: Tenant,
        handoff: Handoff,
        lang: str,
        number: str,
        alert_text: str,
        link: str,
        *,
        level: int,
    ) -> str:
        profile = TenantProfile.model_validate(tenant.profile)
        values = {
            "hotel": profile.name,
            "alert": alert_text,
            "priority": handoff.priority,
            "link": link,
        }
        try:
            payload, _text = await self.templates.payload(
                tenant.id, number.lstrip("+"), "gc_staff_alert", lang, values
            )
        except TemplateRefused as exc:
            return f"refused:{exc.reason}"
        if not tenant.phone_number_id:
            return "no_whatsapp_number"
        await self.wa_outbox.enqueue(
            tenant.id,
            payload=payload,
            idempotency_key=f"alert:{handoff.id}:{level}:{number}",
            conversation_id=None,
            category="utility",
        )
        return "queued"

    async def _email(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        handoff: Handoff,
        address: str,
        alert_text: str,
        link: str,
        *,
        level: int,
    ) -> str:
        if not self.email_outbox.configured:
            return "email_not_configured"
        text = (
            f"{profile.name} Guest Concierge: {alert_text}\n"
            f"Priorität/priority {handoff.priority}, Auslöser/trigger {handoff.trigger}.\n"
            f"Posteingang/inbox: {link}\n"
        )
        await self.email_outbox.enqueue(
            tenant.id,
            profile,
            to=address,
            subject=f"[{handoff.priority}] {profile.name}: {alert_text[:60]}",
            text=text,
            idempotency_key=f"alert:{handoff.id}:{level}:{address}",
            conversation_id=handoff.conversation_id,
            business_initiated=False,
            category="alert",
        )
        return "queued"


def summary_for_alert(details: dict[str, Any]) -> str:
    return str(details.get("summary") or details.get("reason") or "")[:120]
