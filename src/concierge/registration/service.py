"""Registration service (F15): signed links, the closed field set, the arrival-day gate,
export and hard deletion.

Implements: F15, F25, C3, C8, C10, C16

Only the § 30 Abs. 2 BMG list is accepted (extra fields are rejected); the data is
stored encrypted in ``meldeschein`` and never reused for marketing. Confirmation is
allowed on the arrival day only, in the tenant's time zone, through the payment-proof
port. Rows are hard-deleted ``retention_days`` after departure (validated 365 to 455,
default 380) by a job that writes a content-free deletion log; erasure requests do not
override this.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

from concierge import audit
from concierge.clock import Clock, format_date, local_date
from concierge.db.models import DeletionLog, Meldeschein, PaymentProof, Reservation, Tenant
from concierge.db.session import Database
from concierge.registration.proof import PaymentProofPort, ProofCompletion, ProofSession
from concierge.security.crypto import Keyring
from concierge.security.links import LinkError, make_token, read_token
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService

TEXTS_FILE = Path(__file__).resolve().parent / "texts.yaml"
LINK_PURPOSE = "meldeschein"
LINK_MAX_AGE_S = 60 * 24 * 3600
FIELDS = (
    "arrival",
    "departure",
    "family_name",
    "first_name",
    "birth_date",
    "nationalities",
    "address_street",
    "address_postal_code",
    "address_city",
    "address_country",
    "companions_count",
    "companions_nationality",
    "id_document_number",
)


class RegistrationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MeldescheinData(BaseModel):
    """The closed § 30 Abs. 2 BMG field set; anything else is rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    arrival: date
    departure: date
    family_name: str = Field(min_length=1, max_length=100)
    first_name: str = Field(min_length=1, max_length=100)
    birth_date: date
    nationalities: list[str] = Field(min_length=1, max_length=5)
    address_street: str = Field(min_length=1, max_length=120)
    address_postal_code: str = Field(min_length=1, max_length=20)
    address_city: str = Field(min_length=1, max_length=80)
    address_country: str = Field(min_length=2, max_length=2)
    companions_count: int = Field(default=0, ge=0, le=20)
    companions_nationality: str | None = Field(default=None, max_length=60)
    id_document_number: str = Field(min_length=3, max_length=40)

    @field_validator("nationalities", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip().upper() for part in value.split(",") if part.strip()]
        return value

    @field_validator("nationalities")
    @classmethod
    def _codes(cls, value: list[str]) -> list[str]:
        for code in value:
            if len(code) != 2 or not code.isalpha():
                raise ValueError("nationalities are ISO 3166 alpha-2 codes")
        return [c.upper() for c in value]

    @field_validator("address_country")
    @classmethod
    def _country(cls, value: str) -> str:
        if not value.isalpha():
            raise ValueError("country is an ISO 3166 alpha-2 code")
        return value.upper()

    @model_validator(mode="after")
    def _dates(self) -> MeldescheinData:
        if self.departure <= self.arrival:
            raise ValueError("departure must be after arrival")
        if self.birth_date >= self.arrival:
            raise ValueError("birth date must be in the past")
        if self.companions_count and not self.companions_nationality:
            raise ValueError("companions need a nationality")
        return self


@dataclass
class RegistrationTexts:
    data: dict[str, Any]

    def lang(self, lang: str) -> dict[str, Any]:
        texts = self.data["texts"]
        return dict(texts.get(lang) or texts["en"])

    @property
    def status(self) -> str:
        return str(self.data.get("status", "draft_unreviewed"))


def load_texts(path: Path = TEXTS_FILE) -> RegistrationTexts:
    return RegistrationTexts(yaml.safe_load(path.read_text(encoding="utf-8")))


class RegistrationService:
    def __init__(
        self,
        db: Database,
        keyring: Keyring,
        clock: Clock,
        tenants: TenantService,
        *,
        signing_key: bytes,
        public_base_url: str,
        proof: PaymentProofPort,
        texts: RegistrationTexts | None = None,
    ) -> None:
        self.db = db
        self.keyring = keyring
        self.clock = clock
        self.tenants = tenants
        self.signing_key = signing_key
        self.public_base_url = public_base_url.rstrip("/")
        self.proof = proof
        self.texts = texts or load_texts()

    # ---------------------------------------------------------------- links
    def link(self, tenant: Tenant, reservation: Reservation) -> str:
        token = make_token({"t": tenant.id, "r": reservation.id}, LINK_PURPOSE, self.signing_key)
        return f"{self.public_base_url}/r/{token}"

    async def link_builder(
        self, tenant: Tenant, profile: TenantProfile, reservation: Reservation
    ) -> str | None:
        if not profile.registration.enabled:
            return None
        return self.link(tenant, reservation)

    async def resolve(self, token: str) -> tuple[Tenant, Reservation]:
        try:
            payload = read_token(token, LINK_PURPOSE, self.signing_key, LINK_MAX_AGE_S)
        except LinkError as exc:
            raise RegistrationError("invalid_link", str(exc)) from exc
        tenant = await self.tenants.get_by_id(str(payload.get("t", "")))
        if tenant is None:
            raise RegistrationError("invalid_link", "unknown tenant")
        async with self.db.tenant_scope(tenant.id) as session:
            reservation = await session.get(Reservation, str(payload.get("r", "")))
        if reservation is None or reservation.tenant_id != tenant.id:
            raise RegistrationError("invalid_link", "unknown reservation")
        if (
            reservation.status == "cancelled"
            or reservation.departure < self.clock.now().date() - timedelta(days=1)
        ):
            raise RegistrationError("expired", "the stay is over or cancelled")
        return tenant, reservation

    # ---------------------------------------------------------------- data
    async def get(self, tenant_id: str, reservation_id: str) -> Meldeschein | None:
        async with self.db.tenant_scope(tenant_id) as session:
            return await session.scalar(
                select(Meldeschein).where(
                    Meldeschein.tenant_id == tenant_id,
                    Meldeschein.reservation_id == reservation_id,
                    Meldeschein.deleted_at.is_(None),
                )
            )

    def decrypt(self, row: Meldeschein) -> dict[str, Any]:
        data = json.loads(self.keyring.decrypt_str(row.data_enc, row.tenant_id, "meldeschein"))
        return {str(k): v for k, v in data.items()}

    def parse(self, raw: dict[str, Any]) -> MeldescheinData:
        try:
            return MeldescheinData.model_validate(raw)
        except ValidationError as exc:
            messages = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}"
                for e in exc.errors()
            )
            raise RegistrationError("invalid_data", messages[:400]) from exc

    async def save(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        reservation: Reservation,
        data: MeldescheinData,
        *,
        actor: str,
    ) -> Meldeschein:
        if not profile.registration.enabled:
            raise RegistrationError("disabled", "registration is not enabled for this hotel")
        blob = self.keyring.encrypt_str(
            json.dumps(data.model_dump(mode="json"), ensure_ascii=False), tenant.id, "meldeschein"
        )
        delete_after = data.departure + timedelta(days=profile.registration.retention_days)
        async with self.db.tenant_scope(tenant.id) as session:
            row = await session.scalar(
                select(Meldeschein).where(
                    Meldeschein.tenant_id == tenant.id,
                    Meldeschein.reservation_id == reservation.id,
                    Meldeschein.deleted_at.is_(None),
                )
            )
            if row is None:
                row = Meldeschein(
                    tenant_id=tenant.id,
                    reservation_id=reservation.id,
                    data_enc=blob,
                    arrival=data.arrival,
                    departure=data.departure,
                    delete_after=delete_after,
                )
                session.add(row)
            elif row.confirmed_at is not None:
                raise RegistrationError("confirmed", "a confirmed form cannot be changed here")
            else:
                row.data_enc = blob
                row.arrival = data.arrival
                row.departure = data.departure
                row.delete_after = delete_after
            audit.record(
                session,
                actor=actor,
                action="meldeschein.save",
                target=reservation.res_id,
                tenant_id=tenant.id,
                details={
                    "arrival": data.arrival.isoformat(),
                    "delete_after": delete_after.isoformat(),
                },
            )
            await session.commit()
            await session.refresh(row)
            return row

    # ------------------------------------------------------------- confirm
    def arrival_day_ok(self, profile: TenantProfile, row: Meldeschein) -> bool:
        return local_date(self.clock.now(), profile.timezone) == row.arrival

    async def start_confirmation(
        self,
        tenant: Tenant,
        profile: TenantProfile,
        row: Meldeschein,
        *,
        success_url: str,
        cancel_url: str,
    ) -> ProofSession:
        if row.confirmed_at is not None:
            raise RegistrationError("confirmed", "already confirmed")
        if not self.arrival_day_ok(profile, row):
            raise RegistrationError(
                "not_arrival_day",
                self.texts.lang(profile.default_language)["not_arrival_day"].replace(
                    "{arrival}", format_date(row.arrival, profile.default_language)
                ),
            )
        async with self.db.tenant_scope(tenant.id) as session:
            proof = PaymentProof(
                tenant_id=tenant.id,
                meldeschein_id=row.id,
                provider=self.proof.name,
                session_ref="pending",
            )
            session.add(proof)
            await session.commit()
            await session.refresh(proof)
        session_info = await self.proof.start(
            proof_id=proof.id,
            amount_cents=profile.registration.preauth_cents,
            currency=profile.availability.currency,
            description=f"{profile.name}: Meldeschein {format_date(row.arrival, 'en')}",
            success_url=success_url,
            cancel_url=cancel_url,
        )
        async with self.db.tenant_scope(tenant.id) as session:
            stored = await session.get(PaymentProof, proof.id)
            if stored is not None:
                stored.session_ref = session_info.session_ref
                await session.commit()
        return session_info

    async def complete(self, completion: ProofCompletion) -> Meldeschein | None:
        """Record a provider confirmation; idempotent; releases the pre-authorisation."""
        async with self.db.scope() as session:
            proof = await session.get(PaymentProof, completion.proof_id)
            if proof is None:
                return None
            tenant_id = proof.tenant_id
        async with self.db.tenant_scope(tenant_id) as session:
            proof = await session.get(PaymentProof, completion.proof_id)
            assert proof is not None
            row = await session.get(Meldeschein, proof.meldeschein_id)
            if row is None:
                return None
            if proof.status == "confirmed":
                return row
            proof.status = "confirmed"
            proof.confirmed_at = self.clock.now()
            proof.assignment_number_enc = self.keyring.encrypt_str(
                completion.assignment_number, tenant_id, "assignment_number"
            )
            row.confirmed_at = self.clock.now()
            row.proof_id = proof.id
            audit.record(
                session,
                actor=f"psp:{proof.provider}",
                action="meldeschein.confirm",
                target=row.reservation_id,
                tenant_id=tenant_id,
                details={"provider": proof.provider},
            )
            await session.commit()
            await session.refresh(row)
        try:
            await self.proof.release(completion.payment_intent or completion.session_ref)
        except Exception:  # the hold expires on its own; log, never fail the confirmation
            from concierge.log import get_logger

            get_logger(__name__).warning("registration.release_failed", proof=completion.proof_id)
        return row

    async def proof_for(self, tenant_id: str, meldeschein_id: str) -> PaymentProof | None:
        async with self.db.tenant_scope(tenant_id) as session:
            return await session.scalar(
                select(PaymentProof)
                .where(
                    PaymentProof.tenant_id == tenant_id,
                    PaymentProof.meldeschein_id == meldeschein_id,
                )
                .order_by(PaymentProof.created_at.desc())
            )

    # -------------------------------------------------------------- export
    async def export(self, tenant_id: str, start: date, end: date) -> list[dict[str, Any]]:
        """Machine-readable rows for the authorities (decrypted, confirmed status included)."""
        async with self.db.tenant_scope(tenant_id) as session:
            rows = list(
                (
                    await session.execute(
                        select(Meldeschein)
                        .where(
                            Meldeschein.tenant_id == tenant_id,
                            Meldeschein.arrival >= start,
                            Meldeschein.arrival <= end,
                            Meldeschein.deleted_at.is_(None),
                        )
                        .order_by(Meldeschein.arrival, Meldeschein.id)
                    )
                ).scalars()
            )
            proofs = {
                p.meldeschein_id: p
                for p in (
                    await session.execute(
                        select(PaymentProof).where(
                            PaymentProof.tenant_id == tenant_id,
                            PaymentProof.meldeschein_id.in_([r.id for r in rows]),
                            PaymentProof.status == "confirmed",
                        )
                    )
                ).scalars()
            }
        out: list[dict[str, Any]] = []
        for row in rows:
            data = self.decrypt(row)
            proof = proofs.get(row.id)
            out.append(
                {
                    "meldeschein_id": row.id,
                    "reservation_id": row.reservation_id,
                    **{k: data.get(k) for k in FIELDS},
                    "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
                    "confirmation": (
                        {
                            "provider": proof.provider,
                            "assignment_number": self.keyring.decrypt_str(
                                proof.assignment_number_enc, tenant_id, "assignment_number"
                            )
                            if proof.assignment_number_enc
                            else None,
                        }
                        if proof
                        else None
                    ),
                    "delete_after": row.delete_after.isoformat(),
                }
            )
        return out

    # --------------------------------------------------------------- purge
    async def purge(self, *, today: date | None = None) -> int:
        """Hard-delete every Meldeschein past its deletion date; content-free log."""
        day = today or self.clock.now().date()
        deleted = 0
        async with self.db.scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(Meldeschein).where(Meldeschein.delete_after <= day)
                    )
                ).scalars()
            )
            per_tenant: dict[str, int] = {}
            for row in rows:
                proofs = list(
                    (
                        await session.execute(
                            select(PaymentProof).where(PaymentProof.meldeschein_id == row.id)
                        )
                    ).scalars()
                )
                for proof in proofs:
                    await session.delete(proof)
                await session.delete(row)
                per_tenant[row.tenant_id] = per_tenant.get(row.tenant_id, 0) + 1
                deleted += 1
            for tenant_id, count in per_tenant.items():
                session.add(
                    DeletionLog(
                        tenant_id=tenant_id,
                        table_name="meldeschein",
                        row_count=count,
                        reason="retention",
                    )
                )
            await session.commit()
        return deleted
