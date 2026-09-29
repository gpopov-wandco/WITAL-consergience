"""Admin authentication (F19, F31): passwords, sessions, lockout, bootstrap and invites.

Implements: F19, F31, F20, C3, C16

Argon2id password hashes; server-side sessions (random 256-bit token, only its hash
stored) with an absolute and an idle lifetime; five failed logins lock the account for
fifteen minutes; no default passwords (the first run prints a single-use bootstrap
link); invites are single-use fifteen-minute magic links. Every outcome is audited
without the password or the token.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from concierge import audit
from concierge.clock import Clock, ensure_utc
from concierge.db.models import LoginSession, SystemSetting, Tenant, User
from concierge.db.session import Database
from concierge.security.passwords import (
    WeakPasswordError,
    check_strength,
    hash_password,
    needs_rehash,
    verify_password,
)
from concierge.security.sessions import (
    LOCKOUT_MINUTES,
    LOCKOUT_THRESHOLD,
    MAGIC_LINK_TTL,
    SESSION_IDLE_TTL,
    SESSION_TTL,
    csrf_ok,
    csrf_token,
    hash_token,
    new_session_token,
)
from concierge.tools.setup import BOOTSTRAP_SETTING

ROLES = ("owner", "staff", "operator")


class AuthError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class Principal:
    user: User
    session: LoginSession
    tenant: Tenant | None

    @property
    def is_operator(self) -> bool:
        return self.user.role == "operator"

    @property
    def is_owner(self) -> bool:
        return self.user.role in {"owner", "operator"}

    @property
    def lang(self) -> str:
        return "en" if self.user.locale == "en" else "de"


class AuthService:
    def __init__(self, db: Database, clock: Clock, *, signing_key: bytes) -> None:
        self.db = db
        self.clock = clock
        self.signing_key = signing_key

    # ---------------------------------------------------------------- login
    async def login(
        self, email: str, password: str, *, ip_ref: str = "", user_agent: str = ""
    ) -> tuple[str, Principal]:
        """Returns (cookie token, principal); a generic error hides which part failed."""
        now = self.clock.now()
        lowered = email.strip().lower()
        async with self.db.scope() as session:
            users = list(
                (
                    await session.execute(
                        select(User).where(User.email_lower == lowered, User.is_active.is_(True))
                    )
                ).scalars()
            )
            user = next((u for u in users if u.password_hash), None)
            if user is None:
                # burn the same time as a real verification so timing reveals nothing
                verify_password(hash_password("no-such-user-password-0000"), password)
                audit.record(session, actor="anonymous", action="auth.login_failed", target=lowered)
                await session.commit()
                raise AuthError("invalid_credentials")
            if user.locked_until is not None and ensure_utc(user.locked_until) > now:
                audit.record(session, actor=user.id, action="auth.login_locked", target=lowered)
                await session.commit()
                raise AuthError("locked")
            assert user.password_hash is not None
            if not verify_password(user.password_hash, password):
                user.failed_logins += 1
                if user.failed_logins >= LOCKOUT_THRESHOLD:
                    user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                    user.failed_logins = 0
                    audit.record(session, actor=user.id, action="auth.lockout", target=lowered)
                else:
                    audit.record(session, actor=user.id, action="auth.login_failed", target=lowered)
                await session.commit()
                raise AuthError("invalid_credentials")
            if needs_rehash(user.password_hash):
                user.password_hash = hash_password(password)
            user.failed_logins = 0
            user.locked_until = None
            user.last_login_at = now
            token, token_hash = new_session_token()
            row = LoginSession(
                user_id=user.id,
                tenant_id=user.tenant_id,
                token_hash=token_hash,
                expires_at=now + SESSION_TTL,
                last_seen_at=now,
                ip_ref=ip_ref[:32],
                user_agent=user_agent[:200],
            )
            session.add(row)
            audit.record(session, actor=user.id, action="auth.login", tenant_id=user.tenant_id)
            await session.commit()
            await session.refresh(row)
            await session.refresh(user)
            tenant = await session.get(Tenant, row.tenant_id) if row.tenant_id else None
        return token, Principal(user=user, session=row, tenant=tenant)

    async def resolve(self, token: str | None) -> Principal | None:
        if not token:
            return None
        now = self.clock.now()
        token_hash = hash_token(token)
        async with self.db.scope() as session:
            row = await session.scalar(
                select(LoginSession).where(LoginSession.token_hash == token_hash)
            )
            if row is None:
                return None
            if (
                ensure_utc(row.expires_at) <= now
                or ensure_utc(row.last_seen_at) + SESSION_IDLE_TTL <= now
            ):
                await session.delete(row)
                await session.commit()
                return None
            user = await session.get(User, row.user_id)
            if user is None or not user.is_active:
                return None
            row.last_seen_at = now
            await session.commit()
            await session.refresh(row)
            tenant = await session.get(Tenant, row.tenant_id) if row.tenant_id else None
        return Principal(user=user, session=row, tenant=tenant)

    async def logout(self, token: str | None) -> None:
        if not token:
            return
        async with self.db.scope() as session:
            row = await session.scalar(
                select(LoginSession).where(LoginSession.token_hash == hash_token(token))
            )
            if row is not None:
                audit.record(
                    session, actor=row.user_id, action="auth.logout", tenant_id=row.tenant_id
                )
                await session.delete(row)
                await session.commit()

    async def switch_tenant(self, principal: Principal, tenant_id: str | None) -> Principal:
        """Operators work in one tenant at a time; every switch is audited."""
        if not principal.is_operator:
            raise AuthError("forbidden")
        async with self.db.scope() as session:
            row = await session.get(LoginSession, principal.session.id)
            if row is None:
                raise AuthError("no_session")
            tenant = await session.get(Tenant, tenant_id) if tenant_id else None
            if tenant_id and tenant is None:
                raise AuthError("unknown_tenant")
            row.tenant_id = tenant.id if tenant else None
            audit.record(
                session,
                actor=principal.user.id,
                action="operator.switch_tenant",
                target=tenant.slug if tenant else "-",
                tenant_id=tenant.id if tenant else None,
            )
            await session.commit()
            await session.refresh(row)
        return Principal(user=principal.user, session=row, tenant=tenant)

    # ----------------------------------------------------------------- csrf
    def csrf(self, principal: Principal) -> str:
        return csrf_token(principal.session.token_hash, self.signing_key)

    def csrf_valid(self, principal: Principal, presented: str | None) -> bool:
        return csrf_ok(principal.session.token_hash, self.signing_key, presented)

    # ------------------------------------------------------------ bootstrap
    async def bootstrap_user(self, token: str) -> User:
        """The user behind a valid, unexpired bootstrap link (not yet consumed)."""
        now = self.clock.now()
        async with self.db.scope() as session:
            setting = await session.get(SystemSetting, BOOTSTRAP_SETTING)
            if setting is None:
                raise AuthError("invalid_link")
            value = setting.value
            if (
                not secrets.compare_digest(str(value.get("token_hash", "")), hash_token(token))
                or datetime.fromisoformat(str(value.get("expires_at"))) <= now
            ):
                raise AuthError("invalid_link")
            user = await session.get(User, str(value.get("user_id", "")))
            if user is None or user.password_hash:
                raise AuthError("invalid_link")
            return user

    async def bootstrap(self, token: str, password: str) -> User:
        user = await self.bootstrap_user(token)
        try:
            check_strength(password)
        except WeakPasswordError as exc:
            raise AuthError("weak_password", str(exc)) from exc
        async with self.db.scope() as session:
            row = await session.get(User, user.id)
            assert row is not None
            row.password_hash = hash_password(password)
            setting = await session.get(SystemSetting, BOOTSTRAP_SETTING)
            if setting is not None:
                await session.delete(setting)
            audit.record(session, actor=row.id, action="auth.bootstrap", target=row.email_lower)
            await session.commit()
            await session.refresh(row)
            return row

    # -------------------------------------------------------------- invites
    async def invite(
        self,
        *,
        tenant_id: str | None,
        email: str,
        name: str,
        role: str,
        actor: str,
        locale: str = "de",
    ) -> tuple[User, str]:
        """Create or refresh an invited user; returns the user and the single-use token."""
        if role not in ROLES:
            raise AuthError("bad_role")
        if role == "operator" and tenant_id is not None:
            raise AuthError("bad_role", "operators are not bound to a tenant")
        lowered = email.strip().lower()
        if "@" not in lowered or len(lowered) > 254:
            raise AuthError("bad_email")
        now = self.clock.now()
        token = secrets.token_urlsafe(32)
        async with self.db.scope() as session:
            user = await session.scalar(
                select(User).where(User.tenant_id == tenant_id, User.email_lower == lowered)
            )
            if user is None:
                user = User(
                    tenant_id=tenant_id,
                    email=email.strip(),
                    email_lower=lowered,
                    name=name.strip()[:200],
                    role=role,
                    locale=locale if locale in {"de", "en"} else "de",
                )
                session.add(user)
            elif user.password_hash:
                raise AuthError("exists", "the user already has a password")
            user.invite_token_hash = hash_token(token)
            user.invite_expires_at = now + MAGIC_LINK_TTL
            user.role = role
            user.is_active = True
            audit.record(
                session,
                actor=actor,
                action="team.invite",
                target=lowered,
                tenant_id=tenant_id,
                details={"role": role},
            )
            await session.commit()
            await session.refresh(user)
            return user, token

    async def invited_user(self, token: str) -> User:
        now = self.clock.now()
        async with self.db.scope() as session:
            user = await session.scalar(
                select(User).where(User.invite_token_hash == hash_token(token))
            )
            if (
                user is None
                or user.invite_expires_at is None
                or ensure_utc(user.invite_expires_at) <= now
                or not user.is_active
            ):
                raise AuthError("invalid_link")
            return user

    async def accept_invite(self, token: str, password: str) -> User:
        user = await self.invited_user(token)
        try:
            check_strength(password)
        except WeakPasswordError as exc:
            raise AuthError("weak_password", str(exc)) from exc
        async with self.db.scope() as session:
            row = await session.get(User, user.id)
            assert row is not None
            row.password_hash = hash_password(password)
            row.invite_token_hash = None
            row.invite_expires_at = None
            row.failed_logins = 0
            row.locked_until = None
            audit.record(
                session,
                actor=row.id,
                action="team.invite_accepted",
                target=row.email_lower,
                tenant_id=row.tenant_id,
            )
            await session.commit()
            await session.refresh(row)
            return row

    async def set_active(self, user_id: str, active: bool, *, actor: str) -> User:
        async with self.db.scope() as session:
            row = await session.get(User, user_id)
            if row is None:
                raise AuthError("unknown_user")
            row.is_active = active
            if not active:
                for login in (
                    await session.execute(
                        select(LoginSession).where(LoginSession.user_id == user_id)
                    )
                ).scalars():
                    await session.delete(login)
            audit.record(
                session,
                actor=actor,
                action="team.set_active",
                target=row.email_lower,
                tenant_id=row.tenant_id,
                details={"active": active},
            )
            await session.commit()
            await session.refresh(row)
            return row

    async def users(self, tenant_id: str | None) -> list[User]:
        async with self.db.scope() as session:
            return list(
                (
                    await session.execute(
                        select(User).where(User.tenant_id == tenant_id).order_by(User.created_at)
                    )
                ).scalars()
            )
