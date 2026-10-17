"""Shared Alembic env: each service's migrations/env.py imports its models and calls run()."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from gj_common.db import Base


def _do_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


def run(database_url: str) -> None:
    if context.is_offline_mode():
        context.configure(url=database_url, target_metadata=Base.metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        asyncio.run(_run_online(database_url))
