"""End of call (F26, F24, F25): transcript with expiry, summary, callback, usage.

Implements: F26, F24, F25, F17, C16

Idempotent per call: the second ``call_ended`` for the same call changes nothing.
The transcript (text only) expires after ``voice.transcript_retention_days``; a
declined call keeps no transcript. Billable seconds beyond the tenant's included
minutes in the local month cost ``overage_eur_per_min``; the usage event carries the
provider's own cost when the webhook reports one.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import func, select

from concierge.channels.voice.port import CallEnd
from concierge.channels.voice.session import VoiceEngine
from concierge.clock import Clock, local_to_utc, to_local
from concierge.db.models import CallbackTask, Tenant, UsageEvent, VoiceCall
from concierge.db.session import Database
from concierge.handoff.requests import RequestService
from concierge.handoff.service import HandoffService
from concierge.log import get_logger
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

log = get_logger(__name__)

RECONCILE_AFTER = timedelta(hours=6)
OUTCOME_DE = {
    "completed": "beantwortet",
    "in_progress": "beendet",
    "transferred": "an die Rezeption verbunden",
    "callback": "Rückruf notiert",
    "declined": "Assistent abgelehnt, Rückruf notiert",
    "refused_eu_only": "abgewiesen (EU-only-Einstellung)",
    "refused_unavailable": "abgewiesen (Assistent pausiert)",
    "unreconciled": "Ende vom Anbieter nicht gemeldet",
}


def summary_de(
    *, lang: str, duration_s: int, transcript: list[tuple[str, str]], outcome: str
) -> str:
    minutes = max(1, math.ceil(duration_s / 60)) if duration_s else 0
    user_turns = [text for role, text in transcript if role == "user" and text.strip()]
    first = user_turns[0].strip()[:100] if user_turns else "-"
    result = OUTCOME_DE.get(outcome, outcome)
    return (
        f"Anruf ({lang}), {minutes} Min., {len(user_turns)} Anliegen. "
        f"Erstes Anliegen: „{first}“. Ergebnis: {result}."
    )


def billable_seconds(duration_s: int, rounding: str) -> int:
    if duration_s <= 0:
        return 0
    if rounding == "minute":
        return math.ceil(duration_s / 60) * 60
    return duration_s


class PostCall:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        tenants: TenantService,
        handoffs: HandoffService,
        requests: RequestService,
        engine: VoiceEngine,
    ) -> None:
        self.db = db
        self.clock = clock
        self.tenants = tenants
        self.handoffs = handoffs
        self.requests = requests
        self.engine = engine

    async def end_by_provider(self, tenant: Tenant, end: CallEnd) -> VoiceCall | None:
        """The provider reported the end: finish the live state or the stored row."""
        state = self.engine.state_for(end.provider, end.call_id)
        if state is not None:
            self.engine.forget(state)
            transcript = state.transcript if state.transcript else end.transcript
            return await self.finish(
                tenant,
                state.profile,
                call_row_id=state.call_row_id,
                duration_s=end.duration_s,
                transcript=transcript,
                provider_cost_micro_eur=end.provider_cost_micro_eur,
                outcome=state.outcome if state.outcome != "in_progress" else "completed",
                retain_transcript=not state.declined,
                callback_needed=state.transfer_failed and state.callback_id is None,
            )
        async with self.db.tenant_scope(tenant.id) as session:
            row = await session.scalar(
                select(VoiceCall).where(
                    VoiceCall.tenant_id == tenant.id,
                    VoiceCall.provider == end.provider,
                    VoiceCall.call_id == end.call_id,
                )
            )
        if row is None:
            log.warning("voice.unknown_call", tenant=tenant.id, provider=end.provider)
            return None
        return await self.finish(
            tenant,
            self.tenants.profile_of(tenant),
            call_row_id=row.id,
            duration_s=end.duration_s,
            transcript=end.transcript,
            provider_cost_micro_eur=end.provider_cost_micro_eur,
            outcome=row.outcome if row.outcome not in {"", "in_progress"} else "completed",
            retain_transcript=row.outcome != "declined",
            callback_needed=False,
        )

    async def finish(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        *,
        call_row_id: str,
        duration_s: int,
        transcript: list[tuple[str, str]],
        provider_cost_micro_eur: int | None,
        outcome: str,
        retain_transcript: bool,
        callback_needed: bool,
    ) -> VoiceCall | None:
        now = self.clock.now()
        async with self.db.tenant_scope(tenant.id) as session:
            row = await session.get(VoiceCall, call_row_id)
            if row is None or row.tenant_id != tenant.id:
                return None
            if row.ended_at is not None:
                return row  # replayed end event
            row.ended_at = now
            row.duration_s = max(0, duration_s)
            row.billable_s = billable_seconds(row.duration_s, profile.voice.rounding)
            row.provider_cost_micro_eur = provider_cost_micro_eur
            row.outcome = outcome
            if retain_transcript and transcript:
                row.transcript = "\n".join(f"{role}: {text}" for role, text in transcript)
                row.transcript_expires_at = now + timedelta(
                    days=profile.voice.transcript_retention_days
                )
            else:
                row.transcript = None
                row.transcript_expires_at = None
            row.summary_de = summary_de(
                lang=row.lang,
                duration_s=row.duration_s,
                transcript=transcript if retain_transcript else [],
                outcome=outcome,
            )
            await session.commit()
            await session.refresh(row)
        if callback_needed and row.conversation_id:
            phone = None
            async with self.db.tenant_scope(tenant.id) as session:
                from concierge.db.models import Conversation, Guest

                conversation = await session.get(Conversation, row.conversation_id)
                guest = await session.get(Guest, conversation.guest_id) if conversation else None
                if guest is not None and guest.phone_enc is not None:
                    phone = self.engine.keyring.decrypt_str(guest.phone_enc, tenant.id, "phone")
            await self.requests.create_callback(
                tenant.id,
                conversation_id=row.conversation_id,
                phone=phone,
                reason="transfer failed during the call",
                lang=row.lang,
                voice_call_id=row.id,
            )
        await self._notify_staff(tenant, profile, row)
        await self._record_usage(tenant, profile, row)
        log.info(
            "voice.call_ended",
            tenant=tenant.id,
            call=row.call_id,
            outcome=outcome,
            duration_s=row.duration_s,
        )
        return row

    async def _notify_staff(self, tenant: Tenant, profile: TenantProfile, row: VoiceCall) -> None:
        """A callback task or an emergency handoff is what staff hear about; the rest waits in the inbox."""
        if row.conversation_id is None:
            return
        async with self.db.tenant_scope(tenant.id) as session:
            callbacks = list(
                (
                    await session.execute(
                        select(CallbackTask).where(
                            CallbackTask.tenant_id == tenant.id,
                            CallbackTask.conversation_id == row.conversation_id,
                            CallbackTask.status == "open",
                        )
                    )
                ).scalars()
            )
        if not callbacks:
            return
        reasons = "; ".join(c.reason for c in callbacks[:3])
        await self.handoffs.create(
            tenant.id,
            row.conversation_id,
            priority="P2",
            trigger="voice_callback",
            reason=f"callback requested on the phone: {reasons}"[:300],
            profile=profile,
            summary=row.summary_de,
        )

    async def _record_usage(self, tenant: Tenant, profile: TenantProfile, row: VoiceCall) -> None:
        now = self.clock.now()
        local_now = to_local(now, profile.timezone)
        month_start = local_to_utc(
            local_now.date().replace(day=1), local_now.time().min, profile.timezone
        )
        async with self.db.tenant_scope(tenant.id) as session:
            existing = await session.scalar(
                select(UsageEvent).where(
                    UsageEvent.tenant_id == tenant.id, UsageEvent.ref == f"voice:{row.id}"
                )
            )
            if existing is not None:
                return
            used = await session.scalar(
                select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                    UsageEvent.tenant_id == tenant.id,
                    UsageEvent.kind == "voice",
                    UsageEvent.occurred_at >= month_start,
                )
            )
            used_s = int(used or 0)
            included_s = profile.voice.included_minutes * 60
            overage_s = max(0, used_s + row.billable_s - max(included_s, used_s))
            cost_micro = round(overage_s / 60 * profile.voice.overage_eur_per_min * 1_000_000)
            session.add(
                UsageEvent(
                    tenant_id=tenant.id,
                    kind="voice",
                    category=row.provider,
                    quantity=row.billable_s,
                    unit="second",
                    cost_micro_eur=cost_micro,
                    occurred_at=now,
                    ref=f"voice:{row.id}",
                    meta={
                        "call_id": row.call_id,
                        "duration_s": row.duration_s,
                        "overage_s": overage_s,
                        "included_minutes": profile.voice.included_minutes,
                        "provider_cost_micro_eur": row.provider_cost_micro_eur,
                        "outcome": row.outcome,
                    },
                )
            )
            await session.commit()

    async def reconcile(self, tenant: Tenant) -> int:
        """Close calls whose end the provider never reported (F26 voice reconciliation)."""
        cutoff = self.clock.now() - RECONCILE_AFTER
        profile = self.tenants.profile_of(tenant)
        async with self.db.tenant_scope(tenant.id) as session:
            stale = list(
                (
                    await session.execute(
                        select(VoiceCall).where(
                            VoiceCall.tenant_id == tenant.id,
                            VoiceCall.ended_at.is_(None),
                            VoiceCall.started_at <= cutoff,
                        )
                    )
                ).scalars()
            )
        count = 0
        for row in stale:
            state = self.engine.state_for(row.provider, row.call_id)
            transcript = state.transcript if state else []
            if state is not None:
                self.engine.forget(state)
            await self.finish(
                tenant,
                profile,
                call_row_id=row.id,
                duration_s=0,
                transcript=transcript,
                provider_cost_micro_eur=None,
                outcome="unreconciled",
                retain_transcript=bool(state and not state.declined),
                callback_needed=False,
            )
            count += 1
        return count


def month_start_utc(now: datetime, tz: str) -> datetime:
    local_now = to_local(now, tz)
    return local_to_utc(local_now.date().replace(day=1), local_now.time().min, tz)
