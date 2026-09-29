"""Seed the fictional "Hotel Beispiel" for `concierge demo` (F29).

Implements: F29, C5

Creates the tenant from ``examples/hotel-beispiel/tenant.yaml``, imports the fixture
knowledge base (approved by "fixture"), stores demo WhatsApp secrets that only the
simulator and the mock Graph server accept, and creates demo users. Refused when
``GC_ENV=production``. Later milestones extend it with availability and reservations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import yaml
from sqlalchemy import select

from concierge.availability.manual import CellUpdate
from concierge.clock import local_date
from concierge.db.models import User
from concierge.kb.fixtures import FIXTURES_DIR, import_kb_file
from concierge.reservations.service import ReservationInput
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
    ("operator@witalco.invalid", "operator", "demo-operator-password"),
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


# Fixture room types and prices for the fictional hotel (C5): not real hotel facts.
DEMO_ROOM_TYPES: tuple[tuple[str, dict[str, str], int, int, int], ...] = (
    (
        "ez",
        {
            "de": "Einzelzimmer",
            "en": "Single room",
            "ru": "Одноместный номер",
            "uk": "Одномісний номер",
            "tr": "Tek kişilik oda",
            "it": "Camera singola",
        },
        6,
        1,
        9500,
    ),
    (
        "dz",
        {
            "de": "Doppelzimmer",
            "en": "Double room",
            "ru": "Двухместный номер",
            "uk": "Двомісний номер",
            "tr": "Çift kişilik oda",
            "it": "Camera doppia",
        },
        10,
        2,
        12000,
    ),
)
DEMO_INVENTORY_DAYS = 120
DEMO_GUEST_PHONE = "+4915112345678"


async def seed_inventory(services: Services, tenant_id: str, profile: TenantProfile) -> int:
    """120 days of manual inventory with weekend prices, a min-stay rule and one closed arrival."""
    today = local_date(services.ctx.clock.now(), profile.timezone)
    cells: list[CellUpdate] = []
    for order, (code, names, units, max_occupancy, base_cents) in enumerate(DEMO_ROOM_TYPES):
        await services.inventory.upsert_room_type(
            tenant_id,
            code,
            names=names,
            units_total=units,
            max_occupancy=max_occupancy,
            base_price_cents=base_cents,
            currency=profile.availability.currency,
            sort_order=order,
            actor="demo",
        )
        for offset in range(DEMO_INVENTORY_DAYS):
            day = today + timedelta(days=offset)
            weekend = day.weekday() in {4, 5}
            cells.append(
                CellUpdate(
                    room_type=code,
                    date=day,
                    units_available=max(1, units - (units // 2 if weekend else offset % 3)),
                    min_stay=2 if day.weekday() == 4 else 1,
                    closed_arrival=offset == 20,
                    closed_departure=False,
                    price_cents=base_cents + (2000 if weekend else 0),
                )
            )
    return await services.inventory.apply_cells(
        tenant_id, cells, actor="demo", action="inventory.demo_seed"
    )


async def seed_reservations(services: Services, tenant_id: str, profile: TenantProfile) -> int:
    today = local_date(services.ctx.clock.now(), profile.timezone)
    now = services.ctx.clock.now()
    rows = [
        ReservationInput(
            res_id="RES-1001",
            guest_name="Sim Guest",
            phone=DEMO_GUEST_PHONE,
            email="sim.guest@example.invalid",
            lang="de",
            arrival=today + timedelta(days=3),
            departure=today + timedelta(days=5),
            room_type="dz",
            rate_plan="flex",
            source="fixture",
            adults=2,
            whatsapp_optin=True,
            optin_source="website_form",
            optin_at=now,
            optin_wording_version="v1-draft",
        ),
        ReservationInput(
            res_id="RES-1002",
            guest_name="Anna Muster",
            email="anna.muster@example.invalid",
            lang="en",
            arrival=today + timedelta(days=10),
            departure=today + timedelta(days=12),
            room_type="ez",
            source="fixture",
            adults=1,
        ),
    ]
    for data in rows:
        await services.reservations.upsert(tenant_id, data, actor="demo")
    return len(rows)


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
    templates_created = await services.templates.install_standard(tenant.id, actor="demo")
    if templates_created or created:
        # the mock Graph server approves at once; a real WABA answers PENDING first
        await services.templates.sync(tenant.id, profile.whatsapp.waba_id, actor="demo")
    await services.legal.install_drafts(tenant.id, profile, actor="demo")
    if created:
        await seed_inventory(services, tenant.id, profile)
        await seed_reservations(services, tenant.id, profile)
    users: list[str] = []
    async with services.ctx.db.scope() as session:
        for email, role, password in DEMO_USERS:
            tenant_ref = None if role == "operator" else tenant.id
            found = await session.scalar(
                select(User).where(
                    User.tenant_id.is_(None)
                    if tenant_ref is None
                    else User.tenant_id == tenant_ref,
                    User.email_lower == email,
                )
            )
            if found is None:
                session.add(
                    User(
                        tenant_id=tenant_ref,
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
