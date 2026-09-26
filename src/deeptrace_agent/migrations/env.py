"""Alembic environment. The store runs migrations programmatically on startup; developers can also use
``alembic -c alembic.ini revision --autogenerate`` from the repository root."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from deeptrace_agent.schema import metadata

config = context.config
connection = config.attributes.get("connection")


def run(conn) -> None:
    context.configure(connection=conn, target_metadata=metadata, render_as_batch=conn.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


if connection is not None:
    run(connection)
else:
    with create_engine(config.get_main_option("sqlalchemy.url")).connect() as conn:
        run(conn)
