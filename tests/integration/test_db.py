"""Database layer: migrations, SQLite pragmas, UTC datetimes, tenant isolation, append-only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import make_transient_to_detached

from concierge.db.migrate import head_state
from concierge.db.models import AuditLog, Conversation, Guest, Tenant, UsageEvent
from concierge.db.session import (
    ALLOW_APPEND_ONLY_DELETE,
    AppendOnlyError,
    Database,
    TenantIsolationError,
)

pytestmark = pytest.mark.feature("F31")


async def _tenant(db: Database, slug: str) -> Tenant:
    async with db.scope() as session:
        tenant = Tenant(slug=slug, name=slug.title(), public_key=f"pk_{slug.replace('-', '')}")
        session.add(tenant)
        await session.commit()
        return tenant


async def test_migration_head_matches_database(db: Database) -> None:
    current, head = await head_state(db.engine)
    assert current == head and head is not None


async def test_sqlite_pragmas_applied(db: Database) -> None:
    async with db.engine.connect() as conn:
        journal = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        fks = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
    assert str(journal).lower() == "wal"
    assert fks == 1


async def test_foreign_keys_enforced(db: Database) -> None:
    async with db.scope() as session:
        session.add(Guest(tenant_id="ten_missing", channel="whatsapp", channel_key="u1"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_datetimes_round_trip_as_aware_utc(db: Database) -> None:
    tenant = await _tenant(db, "hotel-a")
    when = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    async with db.tenant_scope(tenant.id) as session:
        guest = Guest(tenant_id=tenant.id, channel="whatsapp", channel_key="u1", last_seen_at=when)
        session.add(guest)
        await session.commit()
    async with db.tenant_scope(tenant.id) as session:
        row = (await session.execute(select(Guest))).scalar_one()
        assert row.last_seen_at == when and row.last_seen_at.tzinfo is not None


async def test_tenant_session_sees_only_its_rows(db: Database) -> None:
    a = await _tenant(db, "hotel-a")
    b = await _tenant(db, "hotel-b")
    async with db.scope() as session:
        session.add_all(
            [
                Guest(tenant_id=a.id, channel="whatsapp", channel_key="shared"),
                Guest(tenant_id=b.id, channel="whatsapp", channel_key="shared"),
            ]
        )
        await session.commit()
    async with db.tenant_scope(a.id) as session:
        rows = (await session.execute(select(Guest))).scalars().all()
        assert [g.tenant_id for g in rows] == [a.id]
        by_key = (
            (await session.execute(select(Guest).where(Guest.channel_key == "shared")))
            .scalars()
            .all()
        )
        assert len(by_key) == 1 and by_key[0].tenant_id == a.id
        # a bulk update only touches the tenant's own rows
        await session.execute(update(Guest).values(lang="en"))
        await session.commit()
    async with db.scope() as session:
        langs = {g.tenant_id: g.lang for g in (await session.execute(select(Guest))).scalars()}
        assert langs == {a.id: "en", b.id: "de"}


async def test_tenant_session_refuses_foreign_writes(db: Database) -> None:
    a = await _tenant(db, "hotel-a")
    b = await _tenant(db, "hotel-b")
    async with db.tenant_scope(a.id) as session:
        session.add(Guest(tenant_id=b.id, channel="whatsapp", channel_key="x"))
        with pytest.raises(TenantIsolationError):
            await session.commit()
    async with db.scope() as session:
        session.add(Guest(tenant_id=b.id, channel="whatsapp", channel_key="x"))
        await session.commit()
    async with db.scope() as session:
        foreign_id = (
            await session.execute(select(Guest.id).where(Guest.tenant_id == b.id))
        ).scalar_one()
    async with db.tenant_scope(a.id) as session:
        assert (await session.execute(select(Guest))).scalars().all() == []
        assert await session.get(Guest, foreign_id) is None
        # an attacker who knows the id and forges the object still cannot delete it
        forged = Guest(id=foreign_id, tenant_id=b.id, channel="whatsapp", channel_key="x")
        make_transient_to_detached(forged)
        session.add(forged)
        await session.delete(forged)
        with pytest.raises(TenantIsolationError):
            await session.commit()
    async with db.scope() as session:
        assert await session.get(Guest, foreign_id) is not None


async def test_append_only_rows_cannot_change(db: Database) -> None:
    a = await _tenant(db, "hotel-a")
    now = datetime(2026, 9, 29, tzinfo=UTC)
    async with db.tenant_scope(a.id) as session:
        session.add(UsageEvent(tenant_id=a.id, kind="llm", quantity=10, occurred_at=now, ref="r1"))
        session.add(AuditLog(tenant_id=a.id, actor="test", action="create"))
        await session.commit()
    async with db.tenant_scope(a.id) as session:
        event = (await session.execute(select(UsageEvent))).scalar_one()
        event.quantity = 999
        with pytest.raises(AppendOnlyError):
            await session.commit()
    async with db.tenant_scope(a.id) as session:
        with pytest.raises(AppendOnlyError):
            await session.execute(delete(UsageEvent))
        with pytest.raises(AppendOnlyError):
            await session.execute(update(AuditLog).values(action="x"))
    async with db.scope() as session:
        session.info[ALLOW_APPEND_ONLY_DELETE] = True
        await session.execute(delete(UsageEvent))
        await session.commit()
    async with db.scope() as session:
        assert (await session.execute(select(UsageEvent))).scalars().all() == []


async def test_unique_keys_include_tenant(db: Database) -> None:
    a = await _tenant(db, "hotel-a")
    b = await _tenant(db, "hotel-b")
    async with db.scope() as session:
        session.add(Guest(tenant_id=a.id, channel="whatsapp", channel_key="u"))
        session.add(Guest(tenant_id=b.id, channel="whatsapp", channel_key="u"))
        await session.commit()
        session.add(Guest(tenant_id=a.id, channel="whatsapp", channel_key="u"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_phone_number_unique_across_tenants(db: Database) -> None:
    async with db.scope() as session:
        session.add(Tenant(slug="a", name="A", public_key="pk_a", phone_number_id="123"))
        session.add(Tenant(slug="b", name="B", public_key="pk_b", phone_number_id="123"))
        with pytest.raises(IntegrityError):
            await session.commit()


@given(
    ops=st.lists(
        st.tuples(st.sampled_from(["a", "b"]), st.sampled_from(["guest", "conversation", "read"])),
        min_size=1,
        max_size=12,
    )
)
@h_settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
async def test_property_interleaved_operations_stay_isolated(
    db: Database, ops: list[tuple[str, str]]
) -> None:
    async with db.scope() as session:
        existing = {t.slug: t for t in (await session.execute(select(Tenant))).scalars()}
    tenants = {}
    for slug in ("a", "b"):
        tenants[slug] = existing.get(slug) or await _tenant(db, slug)
    counts = {"a": 0, "b": 0}
    async with db.scope() as session:
        for slug, tenant in tenants.items():
            counts[slug] = len(
                (await session.execute(select(Guest).where(Guest.tenant_id == tenant.id)))
                .scalars()
                .all()
            )
    for slug, op in ops:
        tenant = tenants[slug]
        async with db.tenant_scope(tenant.id) as session:
            if op == "guest":
                session.add(
                    Guest(tenant_id=tenant.id, channel="web", channel_key=f"k{counts[slug]}")
                )
                counts[slug] += 1
                await session.commit()
            elif op == "conversation":
                guest = (await session.execute(select(Guest))).scalars().first()
                if guest is not None:
                    session.add(Conversation(tenant_id=tenant.id, guest_id=guest.id, channel="web"))
                    await session.commit()
            rows = (await session.execute(select(Guest))).scalars().all()
            assert {g.tenant_id for g in rows} <= {tenant.id}
            assert len(rows) == counts[slug]
