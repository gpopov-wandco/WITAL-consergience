"""Alembic environment: runs inside a connection supplied by :mod:`concierge.db.migrate`."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from concierge.db import models  # noqa: F401  (registers every table on the metadata)
from concierge.db.base import Base

target_metadata = Base.metadata


def _render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Render project types with their import so generated files run as written."""
    if type_ == "type" and type(obj).__module__.startswith("concierge."):
        autogen_context.imports.add(f"import {type(obj).__module__}")
        return f"{type(obj).__module__}.{type(obj).__name__}()"
    return False


def _configure_and_run(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_with_engine(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_configure_and_run)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations() -> None:
    config = context.config
    if context.is_offline_mode():
        context.configure(
            url=config.get_main_option("sqlalchemy.url"),
            target_metadata=target_metadata,
            literal_binds=True,
            render_as_batch=True,
            render_item=_render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure_and_run(connection)
        return
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("no connection and no sqlalchemy.url configured")
    asyncio.run(_run_with_engine(url))


run_migrations()
