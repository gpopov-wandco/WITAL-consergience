"""Engines, sessions and the tenant-isolation guards.

Implements: F31, C15, C16, 6.8

- SQLite: WAL, ``foreign_keys=ON`` and ``busy_timeout`` are set in a ``connect``
  listener before any statement runs; URLs use ``Path.as_posix()``.
- A tenant session carries ``info["tenant_id"]``; a ``do_orm_execute`` listener adds
  ``with_loader_criteria`` for every :class:`TenantScoped` entity (aliases included)
  and refuses bulk statements against append-only tables; ``before_flush`` refuses
  new, changed or deleted rows of another tenant and any change to append-only rows.
- Unrestricted sessions (operators, jobs, CLI) carry no tenant id and are audited by
  their callers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from concierge.db.base import AppendOnly, TenantScoped

TENANT_KEY = "tenant_id"
ALLOW_APPEND_ONLY_DELETE = "allow_append_only_delete"


class TenantIsolationError(RuntimeError):
    """A session tried to touch another tenant's rows."""


class AppendOnlyError(RuntimeError):
    """An append-only row was updated or deleted."""


def make_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    if not url.startswith("sqlite"):
        return create_async_engine(url, echo=echo)
    # SQLite: the pool's reset-on-return ROLLBACK is a wasted round trip to the
    # aiosqlite thread after a committed transaction; roll back only when a
    # transaction is still open (a session that failed without rolling back).
    engine = create_async_engine(url, echo=echo, pool_reset_on_return=None)

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "reset")
    def _sqlite_reset(dbapi_connection: Any, _record: Any, _context: Any) -> None:
        raw = getattr(dbapi_connection, "_connection", None)
        if getattr(raw, "in_transaction", True):
            dbapi_connection.rollback()

    return engine


class Database:
    """Engine plus session factories."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine = make_engine(url, echo=echo)
        self._factory = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        """Unrestricted session for operators, jobs and the CLI."""
        return self._factory()

    def tenant_session(self, tenant_id: str) -> AsyncSession:
        """Session that can only see and change one tenant's rows."""
        if not tenant_id:
            raise TenantIsolationError("tenant_id is required")
        return self._factory(info={TENANT_KEY: tenant_id})

    @asynccontextmanager
    async def tenant_scope(self, tenant_id: str) -> AsyncIterator[AsyncSession]:
        session = self.tenant_session(tenant_id)
        try:
            yield session
        finally:
            await session.close()

    @asynccontextmanager
    async def scope(self) -> AsyncIterator[AsyncSession]:
        session = self.session()
        try:
            yield session
        finally:
            await session.close()

    async def dispose(self) -> None:
        await self.engine.dispose()


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_criteria(state: ORMExecuteState) -> None:
    session_info = state.session.info
    tenant_id = session_info.get(TENANT_KEY)
    if (state.is_update or state.is_delete) and not session_info.get(ALLOW_APPEND_ONLY_DELETE):
        for mapper in state.all_mappers:
            if issubclass(mapper.class_, AppendOnly):
                raise AppendOnlyError(f"{mapper.class_.__name__} rows are append-only")
    if tenant_id is None:
        return
    if state.is_column_load or state.is_relationship_load:
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    state.statement = state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _guard_flush(session: Session, _context: Any, _instances: Any) -> None:
    tenant_id = session.info.get(TENANT_KEY)
    allow_delete = bool(session.info.get(ALLOW_APPEND_ONLY_DELETE))
    for obj in session.new:
        if tenant_id is not None and isinstance(obj, TenantScoped) and obj.tenant_id != tenant_id:
            raise TenantIsolationError("cannot insert a row for another tenant")
    for obj in session.dirty:
        if not session.is_modified(obj, include_collections=False):
            continue
        if isinstance(obj, AppendOnly):
            raise AppendOnlyError(f"{type(obj).__name__} rows are append-only")
        if tenant_id is not None and isinstance(obj, TenantScoped) and obj.tenant_id != tenant_id:
            raise TenantIsolationError("cannot change a row of another tenant")
    for obj in session.deleted:
        if isinstance(obj, AppendOnly) and not allow_delete:
            raise AppendOnlyError(f"{type(obj).__name__} rows are append-only")
        if tenant_id is not None and isinstance(obj, TenantScoped) and obj.tenant_id != tenant_id:
            raise TenantIsolationError("cannot delete a row of another tenant")
