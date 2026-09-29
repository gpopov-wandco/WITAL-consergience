"""Per-tenant secrets, encrypted at rest and write-only for users; key rotation.

Implements: F21, F31, C3

Secrets (WhatsApp tokens, PMS credentials, iCal URLs, reservation-API secrets) are
stored in ``tenant_secret`` under AES-256-GCM with AAD ``tenant_id|name|key_id``.
``rotate_all`` re-encrypts every secret and every encrypted personal-data column under
the active master key and recomputes blind indexes.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from concierge import audit
from concierge.db.base import TenantScoped
from concierge.db.models import CallbackTask, Guest, Reservation, TenantSecret, User
from concierge.db.session import Database
from concierge.security.crypto import Keyring

# (model, [(encrypted column, field name for AAD, blind-index column or None, kind)])
ENCRYPTED_COLUMNS: list[tuple[type[TenantScoped], list[tuple[str, str, str | None, str]]]] = [
    (
        Guest,
        [
            ("phone_enc", "phone", "phone_bidx", "phone"),
            ("name_enc", "name", "name_bidx", "name"),
            ("email_enc", "email", "email_bidx", "email"),
        ],
    ),
    (
        Reservation,
        [
            ("phone_enc", "phone", "phone_bidx", "phone"),
            ("name_enc", "name", "name_bidx", "name"),
            ("email_enc", "email", "email_bidx", "email"),
        ],
    ),
    (CallbackTask, [("phone_enc", "phone", "phone_bidx", "phone")]),
]

KNOWN_SECRET_NAMES: tuple[str, ...] = (
    "wa_access_token",
    "wa_app_secret",
    "wa_verify_token",
    "reservation_api_secret",
    "beds24_refresh_token",
    "apaleo_client_id",
    "apaleo_client_secret",
    "mews_client_token",
    "mews_access_token",
    "smoobu_api_key",
    "smoobu_api_secret",
    "ibelsa_key",
    "voice_sip_user",
    "voice_sip_password",
)


class SecretStore:
    def __init__(self, db: Database, keyring: Keyring) -> None:
        self.db = db
        self.keyring = keyring

    async def set(self, tenant_id: str, name: str, value: str, *, actor: str) -> None:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("secret names use letters, digits and underscores")
        if not value:
            raise ValueError("secret value is empty")
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(TenantSecret).where(
                    TenantSecret.tenant_id == tenant_id, TenantSecret.name == name
                )
            )
            blob = self.keyring.encrypt_str(value, tenant_id, name)
            if row is None:
                session.add(
                    TenantSecret(
                        tenant_id=tenant_id,
                        name=name,
                        ciphertext=blob,
                        key_id=self.keyring.active_key_id,
                    )
                )
            else:
                row.ciphertext = blob
                row.key_id = self.keyring.active_key_id
            audit.record(
                session, actor=actor, action="secret.set", target=name, tenant_id=tenant_id
            )
            await session.commit()

    async def get(self, tenant_id: str, name: str) -> str | None:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(TenantSecret).where(
                    TenantSecret.tenant_id == tenant_id, TenantSecret.name == name
                )
            )
            if row is None:
                return None
            return self.keyring.decrypt_str(row.ciphertext, tenant_id, name)

    async def get_many(self, tenant_id: str, names: Sequence[str]) -> dict[str, str]:
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(TenantSecret).where(
                        TenantSecret.tenant_id == tenant_id, TenantSecret.name.in_(list(names))
                    )
                )
            ).scalars()
            return {r.name: self.keyring.decrypt_str(r.ciphertext, tenant_id, r.name) for r in rows}

    async def names(self, tenant_id: str) -> list[str]:
        async with self.db.tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(TenantSecret.name).where(TenantSecret.tenant_id == tenant_id)
                )
            ).all()
            return sorted(name for (name,) in rows)

    async def delete(self, tenant_id: str, name: str, *, actor: str) -> bool:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(TenantSecret).where(
                    TenantSecret.tenant_id == tenant_id, TenantSecret.name == name
                )
            )
            if row is None:
                return False
            await session.delete(row)
            audit.record(
                session, actor=actor, action="secret.delete", target=name, tenant_id=tenant_id
            )
            await session.commit()
            return True

    async def rotate_all(self, *, actor: str) -> int:
        """Re-encrypt everything under the active key; returns the number of rows touched."""
        touched = 0
        async with self.db.scope() as session:
            for row in (await session.execute(select(TenantSecret))).scalars():
                if row.key_id != self.keyring.active_key_id:
                    row.ciphertext = self.keyring.rotate(row.ciphertext, row.tenant_id, row.name)
                    row.key_id = self.keyring.active_key_id
                    touched += 1
            for user in (await session.execute(select(User))).scalars():
                if (
                    user.totp_secret_enc
                    and self.keyring.key_id_of(user.totp_secret_enc) != self.keyring.active_key_id
                ):
                    user.totp_secret_enc = self.keyring.rotate(
                        user.totp_secret_enc, user.tenant_id or "operator", "totp"
                    )
                    touched += 1
                if (
                    user.whatsapp_alert_number_enc
                    and self.keyring.key_id_of(user.whatsapp_alert_number_enc)
                    != self.keyring.active_key_id
                ):
                    user.whatsapp_alert_number_enc = self.keyring.rotate(
                        user.whatsapp_alert_number_enc, user.tenant_id or "operator", "alert_number"
                    )
                    touched += 1
            for model, columns in ENCRYPTED_COLUMNS:
                for obj in (await session.execute(select(model))).scalars():
                    for enc_col, aad_name, bidx_col, kind in columns:
                        blob = getattr(obj, enc_col)
                        if not blob or self.keyring.key_id_of(blob) == self.keyring.active_key_id:
                            continue
                        plain = self.keyring.decrypt_str(blob, obj.tenant_id, aad_name)
                        setattr(
                            obj, enc_col, self.keyring.encrypt_str(plain, obj.tenant_id, aad_name)
                        )
                        if bidx_col:
                            setattr(
                                obj, bidx_col, self.keyring.blind_index(plain, obj.tenant_id, kind)
                            )
                        touched += 1
            audit.record(
                session,
                actor=actor,
                action="secrets.rotate",
                details={"rows": touched, "key_id": self.keyring.active_key_id},
            )
            await session.commit()
        return touched
