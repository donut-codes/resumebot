"""Alembic's environment: the app's one metadata, an async engine, and only the tables this app owns."""

import asyncio
import logging
import os
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from process_engine import models as proc_models  # noqa: F401 - registers the engine's tables on its own metadata
from process_engine.base import Base as EngineBase
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import Base
from app.db import OWNED_TABLES, create_schema, is_empty
from app.jobsearch import models  # noqa: F401 - registers the job-search tables
from app.models import agent, handout, work  # noqa: F401 - registers the agent, hand-out, task and trail tables
from app.sites import models as site_models  # noqa: F401 - registers the site and site-project tables

config = context.config
if config.config_file_name is not None and not logging.getLogger().handlers:  # inside the app, its logging stays
    fileConfig(config.config_file_name, disable_existing_loggers=False)

#: The tables this app owns: the process engine's shadow, mode and gate tables are on its metadata but are
#: not made here.
OWNED = OWNED_TABLES

#: The app's metadata and the process engine package's own (README, "Tables"); `include_object` keeps to `OWNED`.
target_metadata = [Base.metadata, EngineBase.metadata]


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in OWNED
    table = getattr(obj, "table", None)
    return table is None or table.name in OWNED


def database_url() -> str:
    url = config.attributes.get("url") or os.environ.get("JOBSEARCH_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("no database URL: set JOBSEARCH_DATABASE_URL")
    return url


def _migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
    if is_empty(connection):
        # A new file is made from the models and stamped at head: the migrations before it are Postgres-era and never
        # run on SQLite.
        create_schema(connection)
        context.get_context().stamp(ScriptDirectory.from_config(config), "head")
        connection.commit()
        return
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_migrate)
    await engine.dispose()


asyncio.run(run_async_migrations())
