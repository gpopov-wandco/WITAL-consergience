"""Tenant lifecycle: create, list, update, pause, kill switch, resolution, activation gates.

Implements: F20, F21, C10, C15, 6.3

Tenant resolution never trusts the client: WhatsApp resolves by ``phone_number_id``,
the widget by public key (and Origin, checked by the channel), voice by agent id or
dialled number. Activation refuses until every gate in F21 holds.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge import audit
from concierge.clock import Clock
from concierge.db.models import KbEntry, LegalText, Template, Tenant
from concierge.db.session import Database
from concierge.tenants.profile import TenantProfile

REQUIRED_LEGAL_KEYS: tuple[str, ...] = (
    "privacy_paragraph",
    "ai_disclosure",
    "whatsapp_optin",
    "chat_terms",
    "emergency_script",
    "complaint_script",
)
CORE_KB_KEYS: tuple[str, ...] = (
    "identity",
    "contact_hours",
    "checkin",
    "checkout",
    "late_arrival",
    "cancellation",
    "payment",
    "parking",
    "breakfast",
    "emergency",
    "handoff",
    "legal",
)
REQUIRED_TEMPLATES: tuple[str, ...] = (
    "gc_booking_followup",
    "gc_prearrival_7d",
    "gc_prearrival_2d",
    "gc_arrival_day",
    "gc_stay_check",
    "gc_checkout_reminder",
    "gc_registration_link",
    "gc_staff_alert",
)


class TenantError(ValueError):
    pass


@dataclass
class ActivationReport:
    ok: bool
    failures: list[str] = field(default_factory=list)


class TenantService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    # ------------------------------------------------------------- create
    async def create(self, profile: TenantProfile, *, actor: str) -> Tenant:
        async with self.db.scope() as session:
            existing = await session.scalar(select(Tenant).where(Tenant.slug == profile.slug))
            if existing is not None:
                raise TenantError(f"tenant {profile.slug!r} already exists")
            tenant = Tenant(
                slug=profile.slug, name=profile.name, public_key=profile.widget.public_key
            )
            self._mirror(tenant, profile)
            session.add(tenant)
            audit.record(session, actor=actor, action="tenant.create", target=profile.slug)
            await session.commit()
            await session.refresh(tenant)
            return tenant

    @staticmethod
    def _mirror(tenant: Tenant, profile: TenantProfile) -> None:
        tenant.name = profile.name
        tenant.timezone = profile.timezone
        tenant.status = profile.status
        tenant.ai_mode = profile.ai_mode
        tenant.paused = profile.status == "paused"
        tenant.public_key = profile.widget.public_key
        tenant.phone_number_id = profile.whatsapp.phone_number_id or None
        tenant.waba_id = profile.whatsapp.waba_id or None
        tenant.display_phone_number = profile.whatsapp.display_phone_number or None
        tenant.voice_agent_id = profile.voice.agent_id or None
        tenant.voice_number = profile.voice.number or None
        tenant.profile = profile.model_dump(mode="json")

    # --------------------------------------------------------------- read
    async def get(self, slug: str) -> Tenant | None:
        async with self.db.scope() as session:
            return await session.scalar(select(Tenant).where(Tenant.slug == slug))

    async def require(self, slug: str) -> Tenant:
        tenant = await self.get(slug)
        if tenant is None:
            raise TenantError(f"unknown tenant {slug!r}; `concierge tenant list` shows the tenants")
        return tenant

    async def get_by_id(self, tenant_id: str) -> Tenant | None:
        async with self.db.scope() as session:
            return await session.get(Tenant, tenant_id)

    async def list(self) -> list[Tenant]:
        async with self.db.scope() as session:
            return list((await session.execute(select(Tenant).order_by(Tenant.slug))).scalars())

    async def by_phone_number_id(self, phone_number_id: str) -> Tenant | None:
        if not phone_number_id:
            return None
        async with self.db.scope() as session:
            return await session.scalar(
                select(Tenant).where(Tenant.phone_number_id == phone_number_id)
            )

    async def by_public_key(self, public_key: str) -> Tenant | None:
        if not public_key:
            return None
        async with self.db.scope() as session:
            return await session.scalar(select(Tenant).where(Tenant.public_key == public_key))

    async def by_voice(
        self, *, agent_id: str | None = None, number: str | None = None
    ) -> Tenant | None:
        async with self.db.scope() as session:
            if agent_id:
                found = await session.scalar(
                    select(Tenant).where(Tenant.voice_agent_id == agent_id)
                )
                if found is not None:
                    return found
            if number:
                return await session.scalar(select(Tenant).where(Tenant.voice_number == number))
        return None

    @staticmethod
    def profile_of(tenant: Tenant) -> TenantProfile:
        return TenantProfile.model_validate(tenant.profile)

    # ------------------------------------------------------------- update
    async def update_profile(self, slug: str, profile: TenantProfile, *, actor: str) -> Tenant:
        if profile.slug != slug:
            raise TenantError("the profile slug must match the tenant")
        async with self.db.scope() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
            if tenant is None:
                raise TenantError(f"unknown tenant {slug!r}")
            self._mirror(tenant, profile)
            audit.record(
                session,
                actor=actor,
                action="tenant.update_profile",
                target=slug,
                tenant_id=tenant.id,
            )
            await session.commit()
            await session.refresh(tenant)
            return tenant

    async def set_paused(self, slug: str, paused: bool, *, actor: str) -> Tenant:
        async with self.db.scope() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
            if tenant is None:
                raise TenantError(f"unknown tenant {slug!r}")
            tenant.paused = paused
            if paused:
                tenant.status = "paused"
            elif tenant.status == "paused":
                tenant.status = "active" if tenant.activated_at else "pilot"
            profile = dict(tenant.profile)
            profile["status"] = tenant.status
            tenant.profile = profile
            audit.record(
                session,
                actor=actor,
                action="tenant.pause" if paused else "tenant.unpause",
                target=slug,
                tenant_id=tenant.id,
            )
            await session.commit()
            await session.refresh(tenant)
            return tenant

    async def set_kill_switch(self, slug: str, on: bool, *, actor: str) -> Tenant:
        async with self.db.scope() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
            if tenant is None:
                raise TenantError(f"unknown tenant {slug!r}")
            tenant.kill_switch = on
            audit.record(
                session,
                actor=actor,
                action="tenant.kill_switch",
                target=slug,
                tenant_id=tenant.id,
                details={"on": on},
            )
            await session.commit()
            await session.refresh(tenant)
            return tenant

    async def record_dpa(
        self, slug: str, *, signed_at: datetime | None, acknowledged_at: datetime | None, actor: str
    ) -> Tenant:
        async with self.db.scope() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
            if tenant is None:
                raise TenantError(f"unknown tenant {slug!r}")
            if signed_at is not None:
                tenant.dpa_signed_at = signed_at
            if acknowledged_at is not None:
                tenant.subprocessors_acknowledged_at = acknowledged_at
            audit.record(
                session, actor=actor, action="tenant.record_dpa", target=slug, tenant_id=tenant.id
            )
            await session.commit()
            await session.refresh(tenant)
            return tenant

    # --------------------------------------------------------- activation
    async def activation_report(
        self, slug: str, *, doctor_live_ok: bool | None
    ) -> ActivationReport:
        """Evaluate the F21 gates; every failure names the field or command that fixes it."""
        async with self.db.scope() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
            if tenant is None:
                raise TenantError(f"unknown tenant {slug!r}")
            profile = self.profile_of(tenant)
            failures: list[str] = []
            failures += await self._legal_gate(session, tenant.id, profile)
            if tenant.dpa_signed_at is None:
                failures.append(
                    "processor agreement not recorded: `concierge tenant record-dpa --signed-at`"
                )
            if tenant.subprocessors_acknowledged_at is None:
                failures.append(
                    "sub-processor acknowledgement (incl. US transfers) not recorded: `concierge tenant record-dpa --acknowledged-at`"
                )
            failures += await self._kb_gate(session, tenant.id, profile)
            if not tenant.phone_number_id:
                failures.append(
                    "whatsapp.phone_number_id is empty: map the hotel's WhatsApp number"
                )
            if doctor_live_ok is None:
                failures.append(
                    "doctor --live has not been run: `concierge doctor --live --tenant "
                    + slug
                    + "`"
                )
            elif not doctor_live_ok:
                failures.append("doctor --live reported failures")
            failures += await self._template_gate(session, tenant.id, profile)
            return ActivationReport(ok=not failures, failures=failures)

    async def activate(
        self, slug: str, *, actor: str, doctor_live_ok: bool | None
    ) -> ActivationReport:
        report = await self.activation_report(slug, doctor_live_ok=doctor_live_ok)
        if not report.ok:
            return report
        async with self.db.scope() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
            assert tenant is not None
            tenant.status = "active"
            tenant.paused = False
            tenant.activated_at = self.clock.now()
            profile = dict(tenant.profile)
            profile["status"] = "active"
            tenant.profile = profile
            audit.record(
                session, actor=actor, action="tenant.activate", target=slug, tenant_id=tenant.id
            )
            await session.commit()
        return report

    @staticmethod
    async def _legal_gate(
        session: AsyncSession, tenant_id: str, profile: TenantProfile
    ) -> builtins.list[str]:
        rows = (
            (await session.execute(select(LegalText).where(LegalText.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        approved = {
            (r.key, r.lang)
            for r in rows
            if r.status == "approved" and r.approved_by and r.approved_at
        }
        failures = []
        for key in REQUIRED_LEGAL_KEYS:
            for lang in profile.languages:
                if (key, lang) not in approved:
                    failures.append(
                        f"legal text {key!r} ({lang}) is not approved by the hotel (name and date)"
                    )
        return failures

    @staticmethod
    async def _kb_gate(
        session: AsyncSession, tenant_id: str, profile: TenantProfile
    ) -> builtins.list[str]:
        rows = (
            await session.execute(
                select(KbEntry.key, KbEntry.lang).where(
                    KbEntry.tenant_id == tenant_id, KbEntry.status == "approved"
                )
            )
        ).all()
        approved = {(key, lang) for key, lang in rows}
        failures = []
        for key in CORE_KB_KEYS:
            for lang in ("de", "en"):
                if (key, lang) not in approved:
                    failures.append(f"core KB key {key!r} ({lang}) is not approved")
        return failures

    @staticmethod
    async def _template_gate(
        session: AsyncSession, tenant_id: str, profile: TenantProfile
    ) -> builtins.list[str]:
        rows = (
            (await session.execute(select(Template).where(Template.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        approved = {(r.name, r.lang) for r in rows if r.status == "APPROVED"}
        failures = []
        for name in REQUIRED_TEMPLATES:
            for lang in profile.languages:
                if (name, lang) not in approved:
                    failures.append(f"template {name!r} ({lang}) is not APPROVED by Meta")
        return failures
