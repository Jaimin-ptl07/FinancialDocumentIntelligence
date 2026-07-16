"""Alembic environment. Reads the URL from Settings so config has one source.

`get_settings()` is called at import of this module, so every alembic command
requires the six mandatory FDI_ env vars to be present -- including ones that
never touch the database (`alembic history`). That is deliberate: a migration
runner that silently falls back to some other URL is how you migrate the wrong
database. The failure is a pydantic ValidationError naming each missing field.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from fdi.platform.config import get_settings
from fdi.platform.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `%` -> `%%` is load-bearing, not defensive coding. set_main_option hands the
# value to configparser, which reads `%` as interpolation syntax. A password
# containing any of @ : / # ? MUST be percent-encoded to be a legal URL, so a
# realistic generated password arrives here as e.g. `p%40ss` -- and MEASURED on
# this box, that kills every alembic command (even `alembic history`) at import
# of this module with:
#     ValueError: invalid interpolation syntax in
#     'postgresql+asyncpg://test:p%40ss@localhost:55432/test' at position 27
# The message names neither alembic nor the password. Worse, the app itself is
# unaffected -- create_async_engine() takes the URL raw -- so the API would run
# fine while migrations alone died on an error that reads like a config-file
# parser bug. configparser un-escapes on read, so get_main_option() below still
# returns the original URL.
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
