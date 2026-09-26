"""Database schema (SQLAlchemy Core), shared by the store and the Alembic migrations."""

from __future__ import annotations

from sqlalchemy import Column, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text

metadata = MetaData()

runs = Table(
    "runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("question", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("state", Text, nullable=False),  # RunState as JSON
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(32), ForeignKey("runs.id"), nullable=False),
    Column("ts", Float, nullable=False),
    Column("kind", String(64), nullable=False),
    Column("payload", Text, nullable=False),
    Index("events_by_run", "run_id", "id"),
)

leases = Table(
    "leases", metadata,
    Column("run_id", String(32), ForeignKey("runs.id"), primary_key=True),
    Column("owner", String(255), nullable=False),
    Column("token", Integer, nullable=False),  # fencing token, bumped on every change of owner
    Column("expires_at", Float, nullable=False),
)

controls = Table(
    "controls", metadata,
    Column("run_id", String(32), ForeignKey("runs.id"), primary_key=True),
    Column("action", String(16), nullable=False),
    Column("requested_at", Float, nullable=False),
)

tool_calls = Table(
    "tool_calls", metadata,
    Column("key", String(64), primary_key=True),
    Column("run_id", String(32), nullable=False),
    Column("tool", String(64), nullable=False),
    Column("args", Text, nullable=False),
    Column("status", String(16), nullable=False),  # pending (intent recorded, outcome unknown) | done
    Column("result", Text),
    Column("attempts", Integer, nullable=False),
    Column("started_at", Float, nullable=False),
    Column("finished_at", Float),
    Index("tool_calls_by_run", "run_id", "status"),
)
