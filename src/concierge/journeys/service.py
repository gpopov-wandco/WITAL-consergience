"""Journey engine (F13): schedules and sends the guest journey as template messages.

Implements: F13, F14, F11, F18, C7, C9, C16, 10.3

Planning is a pure function of profile, reservation and clock: every step becomes a job
with ``dedupe_key = journey:{tenant}:{res_id}:{step}:{arrival}`` at a DST-correct local
time, shifted out of the quiet hours. Phases and caps: before arrival (booking follow-up,
T-7, T-2, registration link; cap ``before``, T-7 is dropped first), arrival day welcome
(one message by construction), during the stay (day 1 stay check; cap ``during``),
departure day and later (checkout reminder, review request; cap ``after``). At send time
the engine re-checks: not cancelled, opt-in with evidence (marketing opt-in for the
review), no suppression (STOP), not paused, quiet hours, caps, an APPROVED template in
the booking language (otherwise the consented e-mail address, otherwise skip and alert),
and no open complaint before a review request. One topic per message.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select

from concierge.agent.texts import GuestTexts
from concierge.channels.whatsapp.outbox import OutboxService
from concierge.clock import Clock, format_date, in_range, local_to_utc, to_local, zone
from concierge.conversations.service import ConversationService
from concierge.db.models import (
    Conversation,
    Guest,
    Handoff,
    Job,
    Message,
    Reservation,
    Suppression,
    Tenant,
)
from concierge.db.session import Database
from concierge.handoff.requests import RequestService
from concierge.handoff.service import HandoffService
from concierge.jobs.service import JobService
from concierge.kb.store import KbService
from concierge.log import get_logger
from concierge.notify.port import EmailMessage, EmailPort
from concierge.reservations.service import ReservationService
from concierge.security.crypto import Keyring
from concierge.templates.service import TemplateRefused, TemplateService, render
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

log = get_logger(__name__)

JOB_KIND = "journey_step"
ETA_SILENCE_KIND = "eta_silence"
ETA_SILENCE_AFTER = timedelta(hours=2)
STEP_ORDER = (
    "booking_followup",
    "t_minus_7",
    "t_minus_2",
    "registration_link",
    "t_0",
    "key_safe",
    "stay_check",
    "checkout",
    "review_request",
)
PHASES: dict[str, str] = {
    "booking_followup": "before",
    "t_minus_7": "before",
    "t_minus_2": "before",
    "registration_link": "before",
    "t_0": "arrival",
    "key_safe": "arrival",
    "stay_check": "during",
    "checkout": "after",
    "review_request": "after",
}
TEMPLATES: dict[str, str] = {
    "booking_followup": "gc_booking_followup",
    "t_minus_7": "gc_prearrival_7d",
    "t_minus_2": "gc_prearrival_2d",
    "registration_link": "gc_registration_link",
    "t_0": "gc_arrival_day",
    "key_safe": "gc_key_safe",
    "stay_check": "gc_stay_check",
    "checkout": "gc_checkout_reminder",
    "review_request": "gc_review_request",
}
ETA_WINDOW_END: dict[str, time] = {
    "before_18": time(18, 0),
    "18_22": time(22, 0),
    "after_22": time(23, 59),
}
DROP_ORDER: dict[str, tuple[str, ...]] = {
    "before": ("t_minus_7", "booking_followup", "t_minus_2"),
    "during": ("stay_check",),
    "after": ("review_request", "checkout"),
}
ETA_WINDOWS: dict[str, str] = {
    "eta_before_18": "before_18",
    "eta_18_22": "18_22",
    "eta_after_22": "after_22",
}
JOURNEY_BUTTONS: frozenset[str] = frozenset({*ETA_WINDOWS, "stay_ok", "stay_issue"})
AlertSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
LinkBuilder = Callable[[Tenant, TenantProfile, Reservation], Awaitable[str | None]]


@dataclass
class PlannedStep:
    step: str
    template: str
    category: str
    run_at: datetime
    local_at: datetime
    dedupe_key: str
    shifted: bool = False


@dataclass
class StepOutcome:
    step: str
    outcome: str
    detail: str = ""


@dataclass
class PreviewLine:
    step: str
    local_at: datetime
    template: str
    text: str
    category: str


def dedupe_key(tenant_id: str, res_id: str, step: str, arrival: date) -> str:
    return f"journey:{tenant_id}:{res_id}:{step}:{arrival.isoformat()}"


def shift_out_of_quiet_hours(local: datetime, window: tuple[time, time]) -> tuple[datetime, bool]:
    """Move a local time that falls into the quiet window to the window's end."""
    if not in_range(local.time(), window):
        return local, False
    end = window[1]
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate, True


@dataclass
class JourneyEngine:
    db: Database
    clock: Clock
    jobs: JobService
    tenants: TenantService
    reservations: ReservationService
    templates: TemplateService
    outbox: OutboxService
    conversations: ConversationService
    handoffs: HandoffService
    requests: RequestService
    kb: KbService
    keyring: Keyring
    texts: GuestTexts
    email: EmailPort | None = None
    email_from: str = ""
    alert_sink: AlertSink | None = None
    registration_link: LinkBuilder | None = None
    public_base_url: str = ""
    outcomes: list[StepOutcome] = field(default_factory=list)
    """Every send-time decision of this process, for tests and the demo."""

    # ----------------------------------------------------------- planning
    def schedule(
        self, profile: TenantProfile, reservation: Reservation, *, now: datetime
    ) -> list[PlannedStep]:
        """Pure planning: which steps, when (UTC), under caps and quiet hours."""
        if reservation.status == "cancelled" or not reservation.whatsapp_optin:
            return []
        tz = profile.timezone
        journeys = profile.journeys
        window = journeys.quiet_window()
        today = to_local(now, tz).date()
        nights = (reservation.departure - reservation.arrival).days
        candidates: list[tuple[str, date, time]] = []
        created_local = to_local(reservation.created_at, tz) if reservation.created_at else None
        if (
            journeys.booking_followup.enabled
            and created_local is not None
            and now - reservation.created_at <= timedelta(days=1)
            and reservation.arrival > today
        ):
            followup = to_local(now, tz) + timedelta(minutes=journeys.booking_followup.within_min)
            candidates.append(
                (
                    "booking_followup",
                    followup.date(),
                    followup.time().replace(second=0, microsecond=0),
                )
            )
        if journeys.t_minus_7:
            candidates.append(
                ("t_minus_7", reservation.arrival - timedelta(days=7), _hhmm(journeys.t_minus_7_at))
            )
        if journeys.t_minus_2.enabled:
            at = _hhmm(journeys.t_minus_2.at)
            candidates.append(("t_minus_2", reservation.arrival - timedelta(days=2), at))
            if profile.registration.enabled:
                later = (datetime.combine(date.today(), at) + timedelta(minutes=2)).time()  # noqa: DTZ011
                candidates.append(
                    ("registration_link", reservation.arrival - timedelta(days=2), later)
                )
        if journeys.t_0.enabled:
            candidates.append(("t_0", reservation.arrival, _hhmm(journeys.t_0.at)))
        if journeys.key_safe.enabled and reservation.meta.get("late_arrival") is True:
            candidates.append(("key_safe", reservation.arrival, _hhmm(journeys.key_safe.at)))
        if journeys.stay_check.enabled and nights >= journeys.stay_check.min_nights:
            candidates.append(
                (
                    "stay_check",
                    reservation.arrival + timedelta(days=1),
                    _hhmm(journeys.stay_check.at),
                )
            )
        if journeys.checkout.enabled:
            candidates.append(("checkout", reservation.departure, _hhmm(journeys.checkout.at)))
        review = journeys.review_request
        if review.enabled and review.review_url and reservation.marketing_optin:
            candidates.append(
                (
                    "review_request",
                    reservation.departure + timedelta(days=review.days_after),
                    _hhmm(review.at),
                )
            )
        planned: list[PlannedStep] = []
        for step, day, at in candidates:
            local = datetime.combine(day, at, tzinfo=zone(tz))
            local, shifted = shift_out_of_quiet_hours(local, window)
            run_at = local_to_utc(local.date(), local.time(), tz)
            if run_at < now:
                continue
            planned.append(
                PlannedStep(
                    step=step,
                    template=TEMPLATES[step],
                    category="marketing" if step == "review_request" else "utility",
                    run_at=run_at,
                    local_at=local,
                    dedupe_key=dedupe_key(
                        reservation.tenant_id, reservation.res_id, step, reservation.arrival
                    ),
                    shifted=shifted,
                )
            )
        caps = {
            "before": journeys.caps.before,
            "during": journeys.caps.during,
            "after": journeys.caps.after,
        }
        for phase, cap in caps.items():
            in_phase = [p for p in planned if PHASES[p.step] == phase]
            for drop in DROP_ORDER[phase]:
                if len(in_phase) <= cap:
                    break
                victim = next((p for p in in_phase if p.step == drop), None)
                if victim is not None:
                    planned.remove(victim)
                    in_phase.remove(victim)
        planned.sort(key=lambda p: (p.run_at, STEP_ORDER.index(p.step)))
        return planned

    async def plan(self, tenant: Tenant, reservation: Reservation) -> list[PlannedStep]:
        """Enqueue the plan (idempotent per dedupe key); drops pending steps no longer planned."""
        profile = self.tenants.profile_of(tenant)
        now = self.clock.now()
        planned = self.schedule(profile, reservation, now=now)
        prefix = self.reservations.job_prefix(tenant.id, reservation.res_id)
        wanted = {p.dedupe_key for p in planned}
        pending = await self.jobs.pending(tenant_id=tenant.id, kind=JOB_KIND)
        for job in pending:
            if (
                job.dedupe_key
                and job.dedupe_key.startswith(prefix)
                and job.dedupe_key not in wanted
            ):
                await self.jobs.cancel(job_id=job.id)
        for step in planned:
            await self.jobs.enqueue(
                JOB_KIND,
                {
                    "tenant_id": tenant.id,
                    "reservation_id": reservation.id,
                    "res_id": reservation.res_id,
                    "step": step.step,
                    "arrival": reservation.arrival.isoformat(),
                },
                run_at=step.run_at,
                tenant_id=tenant.id,
                dedupe_key=step.dedupe_key,
                max_attempts=3,
            )
        return planned

    async def on_reservation_change(
        self, tenant_id: str, reservation: Reservation, outcome: str
    ) -> None:
        tenant = await self.tenants.get_by_id(tenant_id)
        if tenant is None:
            return
        if outcome == "cancelled" or reservation.status == "cancelled":
            await self.jobs.cancel(
                dedupe_prefix=self.reservations.job_prefix(tenant_id, reservation.res_id)
            )
            return
        await self.plan(tenant, reservation)

    async def stop_for_subject(self, tenant_id: str, phone: str | None) -> int:
        """STOP ends automation: cancel every pending step of the number's reservations."""
        if not phone:
            return 0
        cancelled = 0
        for reservation in await self.reservations.find_by_phone(tenant_id, phone):
            cancelled += await self.jobs.cancel(
                dedupe_prefix=self.reservations.job_prefix(tenant_id, reservation.res_id)
            )
            async with self.db.tenant_scope(tenant_id) as session:
                row = await session.get(Reservation, reservation.id)
                if row is not None:
                    row.journey_paused = True
                    await session.commit()
        return cancelled

    # ---------------------------------------------------------- execution
    async def run_step(self, job: Job) -> None:
        payload = job.payload
        tenant_id = str(payload["tenant_id"])
        step = str(payload["step"])
        tenant = await self.tenants.get_by_id(tenant_id)
        reservation = await self.reservations.get(tenant_id, str(payload["reservation_id"]))
        if tenant is None or reservation is None:
            self._note(step, "missing", "tenant or reservation gone")
            return
        profile = self.tenants.profile_of(tenant)
        now = self.clock.now()
        outcome = await self._gate(tenant, profile, reservation, step, now)
        if outcome is not None:
            await self._log(reservation, step, outcome)
            return
        quiet_window = profile.journeys.quiet_window()
        local_now = to_local(now, profile.timezone)
        shifted, was_shifted = shift_out_of_quiet_hours(local_now, quiet_window)
        if was_shifted:
            retries = int(reservation.meta.get("journey_quiet_retries", 0)) + 1
            await self.jobs.enqueue(
                JOB_KIND,
                dict(payload),
                run_at=local_to_utc(shifted.date(), shifted.time(), profile.timezone),
                tenant_id=tenant_id,
                dedupe_key=f"{job.dedupe_key or dedupe_key(tenant_id, reservation.res_id, step, reservation.arrival)}:q{retries}",
                max_attempts=3,
            )
            async with self.db.tenant_scope(tenant_id) as session:
                row = await session.get(Reservation, reservation.id)
                if row is not None:
                    row.meta = {**row.meta, "journey_quiet_retries": retries}
                    await session.commit()
            self._note(step, "quiet_hours_rescheduled", shifted.isoformat())
            return
        lang = (
            reservation.lang if reservation.lang in profile.languages else profile.default_language
        )
        values = await self._values(tenant, profile, reservation, lang)
        if step == "key_safe" and not values.get("code"):
            await self._log(reservation, step, "no_code", "late_arrival.key_safe_code missing")
            return
        personal = self.reservations.decrypt(reservation)
        phone = personal.get("phone")
        if not phone:
            await self._log(reservation, step, "no_phone")
            return
        template = TEMPLATES[step]
        category = "marketing" if step == "review_request" else "utility"
        key = job.dedupe_key or dedupe_key(tenant_id, reservation.res_id, step, reservation.arrival)
        try:
            wa_payload, text = await self.templates.payload(
                tenant_id, phone.lstrip("+"), template, lang, values
            )
        except TemplateRefused as exc:
            await self._fallback(
                tenant, profile, reservation, step, lang, values, personal, key, reason=exc.reason
            )
            return
        if not tenant.phone_number_id or tenant_id in self.outbox.token_invalid_tenants():
            await self._fallback(
                tenant,
                profile,
                reservation,
                step,
                lang,
                values,
                personal,
                key,
                reason="whatsapp unavailable",
            )
            return
        guest, conversation = await self._conversation(
            tenant, profile, reservation, phone, personal, lang
        )
        message = await self.conversations.record_outbound(
            tenant_id,
            conversation.id,
            text,
            lang=lang,
            kind="template",
            intent=f"J:{step}",
            meta={
                "template": template,
                "step": step,
                "reservation_id": reservation.id,
                "category": category,
            },
            retention_days=profile.retention.chat_days,
        )
        await self.outbox.enqueue(
            tenant_id,
            payload=wa_payload,
            idempotency_key=key,
            conversation_id=conversation.id,
            category=category,
            message_id=message.id,
        )
        await self.reservations.link_guest(tenant_id, reservation.id, guest.id)
        await self._log(reservation, step, "sent", template)

    async def _gate(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        reservation: Reservation,
        step: str,
        now: datetime,
    ) -> str | None:
        if reservation.status == "cancelled":
            return "cancelled"
        if reservation.journey_paused:
            return "paused"
        if (
            not reservation.whatsapp_optin
            or not reservation.optin_source
            or reservation.optin_at is None
        ):
            return "no_optin"
        if step == "review_request" and not reservation.marketing_optin:
            return "no_marketing_optin"
        log_entries = [e for e in reservation.meta.get("journey_log", []) if isinstance(e, dict)]
        if any(
            e.get("step") == step and e.get("outcome") in {"sent", "email_fallback"}
            for e in log_entries
        ):
            return "already_sent"
        phase = PHASES[step]
        caps = {
            "before": profile.journeys.caps.before,
            "during": profile.journeys.caps.during,
            "after": profile.journeys.caps.after,
        }
        if phase in caps:
            sent_in_phase = sum(
                1
                for e in log_entries
                if e.get("outcome") in {"sent", "email_fallback"}
                and PHASES.get(str(e.get("step")), "") == phase
            )
            if sent_in_phase >= caps[phase]:
                return "cap"
        if reservation.phone_bidx is not None:
            async with self.db.tenant_scope(tenant.id) as session:
                suppressed = await session.scalar(
                    select(Suppression).where(
                        Suppression.tenant_id == tenant.id,
                        Suppression.channel == "whatsapp",
                        Suppression.subject_bidx == reservation.phone_bidx,
                    )
                )
            if suppressed is not None:
                return "suppressed"
        if step == "review_request" and await self._open_complaint(tenant.id, reservation):
            return "open_complaint"
        if step == "key_safe":
            if reservation.meta.get("late_arrival") is not True:
                return "no_late_eta"
            if not await self._verified_conversation(tenant.id, reservation):
                return "not_verified"
        return None

    async def _verified_conversation(self, tenant_id: str, reservation: Reservation) -> bool:
        """The key-safe code goes only into a conversation verified for this reservation."""
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(Conversation.id).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.verified_reservation_id == reservation.id,
                )
            )
        return row is not None

    async def _open_complaint(self, tenant_id: str, reservation: Reservation) -> bool:
        if reservation.guest_id is None:
            return False
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(Handoff)
                    .join(Conversation, Conversation.id == Handoff.conversation_id)
                    .where(
                        Handoff.tenant_id == tenant_id,
                        Conversation.guest_id == reservation.guest_id,
                        Handoff.status.in_(["open", "acknowledged", "escalated"]),
                    )
                )
            ).scalars()
            return any(
                h.priority in {"P0", "P1"} or h.trigger in {"complaint", "stay_check", "emergency"}
                for h in rows
            )

    async def _fallback(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        reservation: Reservation,
        step: str,
        lang: str,
        values: dict[str, str],
        personal: dict[str, str],
        key: str,
        *,
        reason: str,
    ) -> None:
        email = personal.get("email")
        definition = self.templates.definition(TEMPLATES[step], lang)
        if (
            profile.journeys.email_fallback
            and email
            and self.email is not None
            and definition is not None
        ):
            text = render(definition, values)
            result = await self.email.send(
                EmailMessage(
                    to=email,
                    subject=f"{profile.name}: {values.get('arrival', '')}",
                    text=text,
                    reply_to=profile.contacts.email or None,
                    idempotency_key=f"{key}:email",
                    tenant_id=tenant.id,
                ),
                sender=self.email_from or profile.contacts.email,
            )
            if result.ok:
                await self._log(reservation, step, "email_fallback", reason)
                return
        await self._log(reservation, step, "skipped", reason)
        if self.alert_sink is not None:
            await self.alert_sink(
                "journey_step_skipped",
                tenant.id,
                {"kind": step, "template": TEMPLATES[step], "lang": lang, "reason": reason},
            )

    async def _conversation(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        reservation: Reservation,
        phone: str,
        personal: dict[str, str],
        lang: str,
    ) -> tuple[Guest, Conversation]:
        first_name = (personal.get("name") or "").split(" ")[0]
        guest = await self.conversations.get_or_create_guest(
            tenant.id,
            "whatsapp",
            phone.lstrip("+"),
            phone=phone,
            name=personal.get("name") or None,
            lang=lang,
        )
        conversation = await self.conversations.open_conversation(
            tenant.id, guest.id, "whatsapp", lang=lang
        )
        if first_name and conversation.lang != lang:
            conversation = await self.conversations.update(tenant.id, conversation.id, lang=lang)
        return guest, conversation

    async def _values(
        self, tenant: Tenant, profile: TenantProfile, reservation: Reservation, lang: str
    ) -> dict[str, str]:
        personal = self.reservations.decrypt(reservation)
        checkin = await self.kb.lookup(tenant.id, "checkin", lang)
        checkout = await self.kb.lookup(tenant.id, "checkout", lang)
        identity = await self.kb.lookup(tenant.id, "identity", lang)
        values = {
            "guest_name": (personal.get("name") or "").split(" ")[0] or profile.name,
            "hotel": profile.name,
            "arrival": format_date(reservation.arrival, lang),
            "departure": format_date(reservation.departure, lang),
            "checkin_from": str((checkin.value if checkin else {}).get("from", "")) or "-",
            "checkout_until": str((checkout.value if checkout else {}).get("until", "")) or "-",
            "address": str((identity.value if identity else {}).get("address", "")) or "-",
            "phone": profile.contacts.reception_phone
            or str((identity.value if identity else {}).get("phone", ""))
            or "-",
            "review_url": profile.journeys.review_request.review_url,
            "link": "",
            "latest_reception": "-",
            "code": "",
        }
        if self.registration_link is not None and profile.registration.enabled:
            values["link"] = await self.registration_link(tenant, profile, reservation) or ""
        late = await self.kb.lookup(tenant.id, "late_arrival", lang)
        if late is not None:
            values["latest_reception"] = str(late.value.get("latest_reception", "")) or "-"
        if reservation.meta.get("late_arrival") is True:
            code = await self.kb.lookup(
                tenant.id, "late_arrival.key_safe_code", lang, verified=True
            )
            if code is not None:
                values["code"] = str(code.value.get("code", ""))
        return values

    async def _log(
        self, reservation: Reservation, step: str, outcome: str, detail: str = ""
    ) -> None:
        self._note(step, outcome, detail)
        async with self.db.tenant_scope(reservation.tenant_id) as session:
            row = await session.get(Reservation, reservation.id)
            if row is None:
                return
            entries = [e for e in row.meta.get("journey_log", []) if isinstance(e, dict)]
            entries.append(
                {
                    "step": step,
                    "outcome": outcome,
                    "at": self.clock.now().isoformat(),
                    "detail": detail[:120],
                }
            )
            row.meta = {**row.meta, "journey_log": entries}
            await session.commit()

    def _note(self, step: str, outcome: str, detail: str = "") -> None:
        self.outcomes.append(StepOutcome(step, outcome, detail))
        log.info("journey.step", step=step, outcome=outcome, detail=detail[:120])

    # ------------------------------------------------------------ replies
    async def eta_window(
        self, tenant: Tenant, guest: Guest, conversation: Conversation, button_id: str, lang: str
    ) -> str | None:
        window = ETA_WINDOWS.get(button_id)
        if window is None:
            return None
        reservation = await self._reservation_for_guest(tenant.id, guest)
        label = self._button_label("gc_prearrival_2d", button_id, lang)
        if reservation is None:
            return self.texts.render("eta_window_no_booking", lang, window=label)
        async with self.db.tenant_scope(tenant.id) as session:
            row = await session.get(Reservation, reservation.id)
            if row is not None:
                row.meta = {
                    **row.meta,
                    "eta_window": window,
                    "eta_window_at": self.clock.now().isoformat(),
                }
                row.eta_set_at = self.clock.now()
                await session.commit()
        await self.requests.create(
            tenant.id,
            conversation_id=conversation.id,
            kind="eta",
            details={"window": window, "label": label, "lang": lang},
            reservation_id=reservation.id,
            idempotency_key=f"eta:{conversation.id}:{window}",
        )
        await self.reservations.link_guest(tenant.id, reservation.id, guest.id)
        refreshed = await self.reservations.get(tenant.id, reservation.id)
        if refreshed is not None:
            await self.on_eta(tenant.id, refreshed)
        return self.texts.render("eta_window_saved", lang, window=label)

    # -------------------------------------------------- ETA consequences
    async def on_eta(self, tenant_id: str, reservation: Reservation) -> None:
        """After an ETA: flag a late arrival, plan the key-safe step, schedule the silence check."""
        tenant = await self.tenants.get_by_id(tenant_id)
        if tenant is None:
            return
        profile = self.tenants.profile_of(tenant)
        lang = (
            reservation.lang if reservation.lang in profile.languages else profile.default_language
        )
        window = str(reservation.meta.get("eta_window") or "")
        end = ETA_WINDOW_END.get(window) or reservation.eta
        cutoff = await self._latest_reception(tenant_id, lang)
        late = window == "after_22" or (
            reservation.eta is not None and cutoff is not None and reservation.eta >= cutoff
        )
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(Reservation, reservation.id)
            if row is None:
                return
            row.meta = {**row.meta, "late_arrival": late}
            await session.commit()
            await session.refresh(row)
        await self.plan(tenant, row)
        if end is not None:
            await self._schedule_eta_silence(tenant, profile, row, end)

    async def _latest_reception(self, tenant_id: str, lang: str) -> time | None:
        entry = await self.kb.lookup(tenant_id, "late_arrival", lang)
        raw = str((entry.value if entry else {}).get("latest_reception", "")).strip()
        if not raw:
            return None
        try:
            return _hhmm(raw)
        except ValueError:
            return None

    async def _schedule_eta_silence(
        self, tenant: Tenant, profile: TenantProfile, reservation: Reservation, end: time
    ) -> None:
        """Two hours after the announced window a silent guest becomes a P1 for staff."""
        tz = profile.timezone
        window_end = local_to_utc(reservation.arrival, end, tz)
        run_local = datetime.combine(reservation.arrival, end, tzinfo=zone(tz)) + ETA_SILENCE_AFTER
        run_at = max(local_to_utc(run_local.date(), run_local.time(), tz), self.clock.now())
        await self.jobs.enqueue(
            ETA_SILENCE_KIND,
            {
                "tenant_id": tenant.id,
                "reservation_id": reservation.id,
                "res_id": reservation.res_id,
                "arrival": reservation.arrival.isoformat(),
                "window_end": window_end.isoformat(),
            },
            run_at=run_at,
            tenant_id=tenant.id,
            dedupe_key=(
                f"{self.reservations.job_prefix(tenant.id, reservation.res_id)}"
                f"eta_silence:{reservation.arrival.isoformat()}"
            ),
            max_attempts=3,
        )

    async def run_eta_silence(self, job: Job) -> None:
        payload = job.payload
        tenant_id = str(payload["tenant_id"])
        tenant = await self.tenants.get_by_id(tenant_id)
        reservation = await self.reservations.get(tenant_id, str(payload["reservation_id"]))
        if tenant is None or reservation is None:
            self._note("eta_silence", "missing", "tenant or reservation gone")
            return
        if reservation.status == "cancelled":
            await self._log(reservation, "eta_silence", "cancelled")
            return
        if reservation.guest_id is None:
            await self._log(reservation, "eta_silence", "no_conversation")
            return
        window_end = datetime.fromisoformat(str(payload["window_end"]))
        async with self.db.tenant_scope(tenant_id) as session:
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.guest_id == reservation.guest_id,
                )
                .order_by(Conversation.created_at.desc())
            )
            wrote = None
            if conversation is not None:
                wrote = await session.scalar(
                    select(Message.id)
                    .where(
                        Message.tenant_id == tenant_id,
                        Message.conversation_id == conversation.id,
                        Message.direction == "in",
                        Message.occurred_at >= window_end,
                    )
                    .limit(1)
                )
        if conversation is None:
            await self._log(reservation, "eta_silence", "no_conversation")
            return
        if wrote is not None:
            await self._log(reservation, "eta_silence", "guest_wrote")
            return
        profile = self.tenants.profile_of(tenant)
        window = str(reservation.meta.get("eta_window") or "")
        label = (
            self._button_label("gc_prearrival_2d", f"eta_{window}", profile.default_language)
            if window
            else (reservation.eta.strftime("%H:%M") if reservation.eta else "-")
        )
        created = await self.handoffs.create(
            tenant_id,
            conversation.id,
            priority="P1",
            trigger="eta_silence",
            reason="no message from the guest two hours after the announced arrival time",
            profile=profile,
            summary=(
                f"Arrival announced for {label} on {reservation.arrival.isoformat()}, "
                f"no word from the guest since (reservation {reservation.res_id})"
            ),
        )
        await self._log(reservation, "eta_silence", "alerted", created.id)

    async def stay_feedback(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        guest: Guest,
        conversation: Conversation,
        ok: bool,
        lang: str,
    ) -> tuple[str, str | None]:
        if ok:
            return self.texts.render("feedback_thanks", lang), None
        reservation = await self._reservation_for_guest(tenant.id, guest)
        created = await self.handoffs.create(
            tenant.id,
            conversation.id,
            priority="P1",
            trigger="stay_check",
            reason="guest reports a problem during the stay",
            profile=profile,
            summary=f"Stay check: problem reported (reservation {reservation.res_id if reservation else 'unknown'})",
        )
        return self.texts.render("complaint_ack", lang), created.id

    async def _reservation_for_guest(self, tenant_id: str, guest: Guest) -> Reservation | None:
        today = self.clock.now().date()
        async with self.db.tenant_scope(tenant_id) as session:
            rows = list(
                (
                    await session.execute(
                        select(Reservation)
                        .where(
                            Reservation.tenant_id == tenant_id,
                            Reservation.status != "cancelled",
                            Reservation.departure >= today - timedelta(days=1),
                        )
                        .order_by(Reservation.arrival)
                    )
                ).scalars()
            )
        linked = [r for r in rows if r.guest_id == guest.id]
        if linked:
            return linked[0]
        if guest.phone_bidx:
            matching = [r for r in rows if r.phone_bidx == guest.phone_bidx]
            if matching:
                return matching[0]
        return None

    def _button_label(self, template: str, button_id: str, lang: str) -> str:
        definition = self.templates.definition(template, lang)
        if definition is None:
            return button_id
        return next((label for i, label in definition.buttons if i == button_id), button_id)

    # ------------------------------------------------------------ preview
    async def preview(self, tenant: Tenant, reservation: Reservation) -> list[PreviewLine]:
        profile = self.tenants.profile_of(tenant)
        lang = (
            reservation.lang if reservation.lang in profile.languages else profile.default_language
        )
        values = await self._values(tenant, profile, reservation, lang)
        lines: list[PreviewLine] = []
        for step in self.schedule(profile, reservation, now=self.clock.now()):
            definition = self.templates.definition(step.template, lang)
            lines.append(
                PreviewLine(
                    step=step.step,
                    local_at=step.local_at,
                    template=step.template,
                    text=render(definition, values) if definition else "",
                    category=step.category,
                )
            )
        return lines

    async def test_send(
        self, tenant: Tenant, to_phone: str, step: str, lang: str
    ) -> tuple[str, str]:
        """Send one journey step with sample values to a staff phone; returns (outbox id, text)."""
        profile = self.tenants.profile_of(tenant)
        template = TEMPLATES[step]
        definition = self.templates.definition(template, lang)
        if definition is None:
            raise ValueError(f"unknown step {step}")
        values = {
            **definition.samples,
            "hotel": profile.name,
            "phone": profile.contacts.reception_phone or "-",
        }
        payload, text = await self.templates.payload(
            tenant.id, to_phone.lstrip("+"), template, lang, values
        )
        row = await self.outbox.enqueue(
            tenant.id,
            payload=payload,
            idempotency_key=f"journey-test:{tenant.id}:{step}:{lang}:{self.clock.now().timestamp()}",
            category="utility",
        )
        return row.id, text


def _hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))
