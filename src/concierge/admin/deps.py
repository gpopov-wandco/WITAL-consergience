"""Request-level guard for the admin pages (F19, F20): session, roles, CSRF, tenant.

Implements: F19, F20, F31, 6.3

The tenant comes from the session row, never from the client; operators switch
tenants through an audited action. Mutations need the CSRF token (hidden form field
or ``X-CSRF-Token`` header, which htmx sends from ``hx-headers``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from concierge.admin.auth import AuthService, Principal
from concierge.admin.i18n import labels
from concierge.db.models import Tenant
from concierge.security.sessions import SESSION_COOKIE
from concierge.tenants.profile import TenantProfile
from concierge.tenants.service import TenantService


@dataclass
class AdminContext:
    principal: Principal
    tenant: Tenant | None
    profile: TenantProfile | None
    lang: str
    csrf: str

    @property
    def tenant_id(self) -> str:
        return self.tenant_row.id

    @property
    def tenant_row(self) -> Tenant:
        """The tenant of a tenant-scoped page (the guard redirected otherwise)."""
        if self.tenant is None:
            raise Forbidden("no tenant")
        return self.tenant

    @property
    def tenant_profile(self) -> TenantProfile:
        if self.profile is None:
            raise Forbidden("no tenant")
        return self.profile

    def base(self, **extra: Any) -> dict[str, Any]:
        """Template variables every admin page needs."""
        return {
            "lang": self.lang,
            "t": labels(self.lang),
            "csrf": self.csrf,
            "user": self.principal.user,
            "tenant": self.tenant,
            "profile": self.profile,
            "is_operator": self.principal.is_operator,
            "is_owner": self.principal.is_owner,
            **extra,
        }


class Forbidden(Exception):
    pass


class AdminGuard:
    def __init__(
        self, auth: AuthService, tenants: TenantService, *, login_path: str = "/admin/login"
    ) -> None:
        self.auth = auth
        self.tenants = tenants
        self.login_path = login_path

    async def principal(self, request: Request) -> Principal | None:
        return await self.auth.resolve(request.cookies.get(SESSION_COOKIE))

    async def context(
        self,
        request: Request,
        *,
        tenant_required: bool = True,
        roles: tuple[str, ...] | None = None,
        mutation: bool = False,
    ) -> AdminContext | Response:
        principal = await self.principal(request)
        if principal is None:
            return RedirectResponse(f"{self.login_path}?next={request.url.path}", status_code=303)
        if roles and principal.user.role not in roles:
            raise Forbidden("role not allowed")
        if mutation:
            presented = request.headers.get("x-csrf-token")
            if presented is None:
                form = await request.form()
                value = form.get("csrf")
                presented = str(value) if value is not None else None
            if not self.auth.csrf_valid(principal, presented):
                raise Forbidden("csrf")
        tenant = principal.tenant
        if tenant_required and tenant is None:
            if principal.is_operator:
                return RedirectResponse("/operator/", status_code=303)
            raise Forbidden("no tenant")
        profile = TenantService.profile_of(tenant) if tenant is not None else None
        return AdminContext(
            principal=principal,
            tenant=tenant,
            profile=profile,
            lang=principal.lang,
            csrf=self.auth.csrf(principal),
        )
