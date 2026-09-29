"""Alembic without an ini file: configuration is built in Python.

Implements: F28, F33, C1

``upgrade`` runs the migration chain inside an existing async engine; ``head_state``
reports the script head and the database revision for ``doctor`` and ``/readyz``.
``revision`` writes Alembic's script template next to the versions directory for the
duration of the call only, so no ``.mako`` file ships in the repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_SCRIPT_TEMPLATE = '''"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
'''


def alembic_config(url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_DIR.as_posix())
    config.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    if url:
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def script_head() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _sync_upgrade(connection: Any, revision: str) -> None:
    config = alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_sync_upgrade, revision)


def _sync_current(connection: Any) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        return await connection.run_sync(_sync_current)


async def head_state(engine: AsyncEngine) -> tuple[str | None, str | None]:
    """Return (database revision, script head)."""
    return await current_revision(engine), script_head()


async def revision(engine: AsyncEngine, message: str, autogenerate: bool = True) -> None:
    """Create a new migration file (development only)."""
    template = MIGRATIONS_DIR / "script.py.mako"
    template.write_text(_SCRIPT_TEMPLATE, encoding="utf-8")
    try:

        def _sync_revision(connection: Any) -> None:
            config = alembic_config()
            config.attributes["connection"] = connection
            command.revision(config, message=message, autogenerate=autogenerate)

        async with engine.begin() as connection:
            await connection.run_sync(_sync_revision)
    finally:
        template.unlink(missing_ok=True)
