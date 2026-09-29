"""Seed the fictional "Hotel Beispiel" for `concierge demo` (F29).

Implements: F29, C5

Creates the tenant from ``examples/hotel-beispiel/tenant.yaml``, imports the fixture
knowledge base (approved by "fixture"), stores demo WhatsApp secrets that only the
simulator and the mock Graph server accept, and creates demo users. Refused when
``GC_ENV=production``. Later milestones extend it with availability and reservations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select

from concierge.db.models import User
from concierge.kb.fixtures import FIXTURES_DIR, import_kb_file
from concierge.security.passwords import hash_password
from concierge.services import Services
from concierge.tenants.profile import TenantProfile

EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples"
# Published fixture values for the offline demo, not credentials: they only ever sign
# traffic between the simulator and the mock Graph server, and ``seed_demo`` refuses to
# run when GC_ENV=production.
DEMO_SECRETS: dict[str, str] = {
    "wa_app_secret": "demo-app-secret-not-for-production",  # nosec B105
    "wa_verify_token": "demo-verify-token",  # nosec B105
    "wa_access_token": "demo-access-token",  # nosec B105
    "reservation_api_secret": "demo-reservation-secret-not-for-production",  # nosec B105
}
DEMO_USERS: tuple[tuple[str, str, str], ...] = (
    ("owner@hotel-beispiel.invalid", "owner", "demo-owner-password"),
    ("staff@hotel-beispiel.invalid", "staff", "demo-staff-password"),
)


class DemoRefused(RuntimeError):
    pass


@dataclass
class SeedResult:
    tenant_id: str
    slug: str
    kb_rows: int
    users: list[str] = field(default_factory=list)
    created: bool = True


def example_profile(slug: str = "hotel-beispiel") -> TenantProfile:
    path = EXAMPLES_DIR / slug / "tenant.yaml"
    return TenantProfile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


async def seed_demo(services: Services, *, slug: str = "hotel-beispiel") -> SeedResult:
    if services.ctx.settings.is_production:
        raise DemoRefused("the demo is refused when GC_ENV=production")
    profile = example_profile(slug)
    existing = await services.tenants.get(profile.slug)
    created = existing is None
    tenant = existing or await services.tenants.create(profile, actor="demo")
    kb_rows = 0
    if created:
        kb_rows = await import_kb_file(
            services.kb, tenant.id, FIXTURES_DIR / "hotel_beispiel.yaml", approve=True, actor="demo"
        )
    for name, value in DEMO_SECRETS.items():
        if await services.secrets.get(tenant.id, name) is None:
            await services.secrets.set(tenant.id, name, value, actor="demo")
    services.secret_cache.invalidate()
    users: list[str] = []
    async with services.ctx.db.scope() as session:
        for email, role, password in DEMO_USERS:
            found = await session.scalar(
                select(User).where(User.tenant_id == tenant.id, User.email_lower == email)
            )
            if found is None:
                session.add(
                    User(
                        tenant_id=tenant.id,
                        email=email,
                        email_lower=email,
                        name=role.title() + " Beispiel",
                        role=role,
                        password_hash=hash_password(password),
                        locale="de",
                    )
                )
            users.append(email)
        await session.commit()
    return SeedResult(
        tenant_id=tenant.id, slug=tenant.slug, kb_rows=kb_rows, users=users, created=created
    )
