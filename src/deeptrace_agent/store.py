"""Persistence (SQLAlchemy): run checkpoints, leases, control flags, an append-only event log and a
write-ahead tool-call log.

Works on SQLite (a file path, the default) and on Postgres (``postgresql+psycopg://...``), so
workers on several hosts can share one queue. The schema is versioned with Alembic and migrated
on startup.

A run is only driven by the worker holding its lease, and every checkpoint carries the lease's
fencing token: if a worker stalls long enough for its lease to expire and another worker takes the
run over, the stale worker's next write is rejected instead of silently overwriting newer progress.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, event, insert, inspect, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from .models import NOT_CLAIMABLE, RunState
from .schema import controls, events, leases, metadata, runs, tool_calls

MIGRATIONS = Path(__file__).parent / "migrations"


@dataclass
class RunRecord:
    id: str
    question: str
    status: str
    created_at: float
    updated_at: float


class LeaseLost(RuntimeError):
    """This worker no longer holds the run's lease; another worker owns it now."""


@dataclass(frozen=True)
class Lease:
    run_id: str
    owner: str
    token: int


@dataclass
class Event:
    id: int
    ts: float
    kind: str
    payload: dict[str, Any]


def database_url(target: str | Path) -> str:
    """A SQLAlchemy URL for ``target``: URLs pass through, anything else is a SQLite file path."""
    target = str(target)
    if "://" in target:
        return target
    if target == ":memory:":
        return "sqlite://"
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{target}"


def make_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    kwargs: dict[str, Any] = {"connect_args": {"timeout": 30, "check_same_thread": False}}
    if url == "sqlite://":
        kwargs["poolclass"] = StaticPool  # one shared in-memory database
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:
        dbapi_conn.isolation_level = None  # let SQLAlchemy's "begin" event control transactions
        dbapi_conn.execute("PRAGMA journal_mode=WAL")

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        # Take SQLite's write lock up front, so read-then-write transactions (lease acquisition,
        # taking a control flag) are serialised across processes instead of failing on upgrade.
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


class RunStore:
    def __init__(self, target: str | Path = "deeptrace.db", *, migrate: bool = True) -> None:
        self.url = database_url(target)
        self.engine = make_engine(self.url)
        self.dialect = self.engine.dialect.name
        if migrate:
            self.migrate()

    def close(self) -> None:
        self.engine.dispose()

    def migrate(self) -> None:
        """Bring the schema to the latest Alembic revision.

        Databases created before migrations existed already have the initial tables but no
        ``alembic_version``: they are stamped as revision 0001 and upgraded from there.
        """
        from alembic import command
        from alembic.config import Config

        with self.engine.begin() as conn:
            config = Config()
            config.set_main_option("script_location", str(MIGRATIONS))
            config.attributes["connection"] = conn
            tables = set(inspect(conn).get_table_names())
            if "alembic_version" not in tables and "runs" in tables:
                command.stamp(config, "0001")
            command.upgrade(config, "head")

    def _insert(self, table):
        """Dialect-specific INSERT supporting ON CONFLICT (SQLite and Postgres)."""
        if self.dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        return dialect_insert(table)

    # -- runs -------------------------------------------------------------

    def create_run(self, state: RunState) -> str:
        run_id = uuid.uuid4().hex[:10]
        now = time.time()
        with self.engine.begin() as c:
            c.execute(insert(runs).values(id=run_id, question=state.question, status=state.stage,
                                          created_at=now, updated_at=now, state=json.dumps(state.to_dict())))
        return run_id

    def checkpoint(self, run_id: str, state: RunState, *, status: str, lease: Lease | None = None) -> None:
        stmt = update(runs).where(runs.c.id == run_id).values(
            status=status, updated_at=time.time(), state=json.dumps(state.to_dict()))
        if lease is not None:
            # Fencing: the write only lands if this worker still holds the lease with this token.
            stmt = stmt.where(select(leases.c.run_id).where(
                leases.c.run_id == run_id, leases.c.owner == lease.owner, leases.c.token == lease.token,
            ).exists())
        with self.engine.begin() as c:
            if c.execute(stmt).rowcount == 0 and lease is not None:
                raise LeaseLost(f"lease on run {run_id} (token {lease.token}) was taken over")

    def requeue(self, run_id: str) -> None:
        """Make a halted or paused run claimable again by clearing its error and hold."""
        state = self.load(run_id)
        state.error = None
        state.hold = None
        self.checkpoint(run_id, state, status=state.status)

    def load(self, run_id: str) -> RunState:
        with self.engine.connect() as c:
            row = c.execute(select(runs.c.state).where(runs.c.id == run_id)).first()
        if row is None:
            raise KeyError(f"no run with id {run_id!r}")
        return RunState.from_dict(json.loads(row[0]))

    def states(self, limit: int = 1000) -> list[RunState]:
        """Most recent run states, newest first (for metrics aggregation)."""
        with self.engine.connect() as c:
            rows = c.execute(select(runs.c.state).order_by(runs.c.created_at.desc()).limit(limit)).all()
        return [RunState.from_dict(json.loads(row[0])) for row in rows]

    def runs(self, limit: int = 20) -> list[RunRecord]:
        with self.engine.connect() as c:
            rows = c.execute(select(runs.c.id, runs.c.question, runs.c.status, runs.c.created_at,
                                    runs.c.updated_at).order_by(runs.c.created_at.desc()).limit(limit)).all()
        return [RunRecord(*row) for row in rows]

    def status(self, run_id: str) -> str:
        with self.engine.connect() as c:
            row = c.execute(select(runs.c.status).where(runs.c.id == run_id)).first()
        if row is None:
            raise KeyError(f"no run with id {run_id!r}")
        return row[0]

    # -- human control ------------------------------------------------------

    def request_control(self, run_id: str, action: str) -> None:
        """Ask whoever drives the run to "cancel" or "pause" it at the next step boundary."""
        stmt = self._insert(controls).values(run_id=run_id, action=action, requested_at=time.time())
        stmt = stmt.on_conflict_do_update(index_elements=["run_id"],
                                          set_={"action": stmt.excluded.action,
                                                "requested_at": stmt.excluded.requested_at})
        with self.engine.begin() as c:
            c.execute(stmt)

    def take_control(self, run_id: str) -> str | None:
        """Atomically read and clear a pending control request."""
        with self.engine.begin() as c:
            row = c.execute(select(controls.c.action).where(controls.c.run_id == run_id).with_for_update()).first()
            if row:
                c.execute(delete(controls).where(controls.c.run_id == run_id))
        return row[0] if row else None

    # -- leases -----------------------------------------------------------

    def acquire(self, run_id: str, owner: str, ttl: float) -> Lease | None:
        """Take the run's lease if it is free, expired, or already ours.

        Taking over from another owner bumps the fencing token, which invalidates any checkpoint
        the previous owner might still try to write. On Postgres the lease row is locked with
        ``FOR UPDATE SKIP LOCKED``: a worker that finds another worker mid-acquisition backs off
        instead of waiting.
        """
        now = time.time()
        try:
            with self.engine.begin() as c:
                row = c.execute(
                    select(leases.c.owner, leases.c.token, leases.c.expires_at)
                    .where(leases.c.run_id == run_id)
                    .with_for_update(skip_locked=True)
                ).first()
                if row is None:
                    if self.dialect == "postgresql" and c.execute(
                        select(leases.c.run_id).where(leases.c.run_id == run_id)
                    ).first():
                        return None  # the row exists but another worker has it locked right now
                    token = 1
                    c.execute(insert(leases).values(run_id=run_id, owner=owner, token=token, expires_at=now + ttl))
                elif row.owner == owner and row.expires_at > now:
                    token = row.token
                    c.execute(update(leases).where(leases.c.run_id == run_id).values(expires_at=now + ttl))
                elif row.expires_at <= now:
                    token = row.token + 1
                    c.execute(update(leases).where(leases.c.run_id == run_id)
                              .values(owner=owner, token=token, expires_at=now + ttl))
                else:
                    return None
        except IntegrityError:
            return None  # another worker inserted the lease first
        return Lease(run_id, owner, token)

    def lease_holder(self, run_id: str) -> tuple[str, float] | None:
        """(owner, seconds until expiry) of a live lease, or None if the run is free."""
        with self.engine.connect() as c:
            row = c.execute(select(leases.c.owner, leases.c.expires_at).where(leases.c.run_id == run_id)).first()
        if row is None or row.expires_at <= time.time():
            return None
        return row.owner, row.expires_at - time.time()

    def renew(self, lease: Lease, ttl: float) -> bool:
        with self.engine.begin() as c:
            result = c.execute(update(leases).where(
                leases.c.run_id == lease.run_id, leases.c.owner == lease.owner, leases.c.token == lease.token,
            ).values(expires_at=time.time() + ttl))
        return result.rowcount == 1

    def release(self, lease: Lease) -> None:
        with self.engine.begin() as c:
            c.execute(update(leases).where(
                leases.c.run_id == lease.run_id, leases.c.owner == lease.owner, leases.c.token == lease.token,
            ).values(expires_at=0))

    def claimable(self, limit: int = 10) -> list[str]:
        """Unfinished, unpaused runs that nobody currently holds a live lease on."""
        stmt = (select(runs.c.id).select_from(runs.outerjoin(leases, leases.c.run_id == runs.c.id))
                .where(runs.c.status.not_in(NOT_CLAIMABLE),
                       (leases.c.run_id.is_(None)) | (leases.c.expires_at <= time.time()))
                .order_by(runs.c.created_at).limit(limit))
        with self.engine.connect() as c:
            return [row[0] for row in c.execute(stmt)]

    def pending(self) -> list[str]:
        """Runs that are neither finished nor paused, whether or not someone holds them."""
        with self.engine.connect() as c:
            rows = c.execute(select(runs.c.id).where(runs.c.status.not_in(NOT_CLAIMABLE)).order_by(runs.c.created_at))
            return [row[0] for row in rows]

    # -- events -----------------------------------------------------------

    def log(self, run_id: str, kind: str, **payload: Any) -> None:
        with self.engine.begin() as c:
            c.execute(insert(events).values(run_id=run_id, ts=time.time(), kind=kind,
                                            payload=json.dumps(payload, ensure_ascii=False)))

    def events(self, run_id: str, *, after: int = 0) -> list[Event]:
        with self.engine.connect() as c:
            rows = c.execute(select(events.c.id, events.c.ts, events.c.kind, events.c.payload)
                             .where(events.c.run_id == run_id, events.c.id > after).order_by(events.c.id)).all()
        return [Event(eid, ts, kind, json.loads(payload)) for eid, ts, kind, payload in rows]

    # -- tool calls: write-ahead intent log + idempotency cache ------------

    @staticmethod
    def tool_key(run_id: str, tool: str, args: dict[str, Any]) -> str:
        blob = json.dumps({"run": run_id, "tool": tool, "args": args}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def tool_record(self, key: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = c.execute(select(tool_calls.c.tool, tool_calls.c.status, tool_calls.c.result,
                                   tool_calls.c.attempts).where(tool_calls.c.key == key)).first()
        if row is None:
            return None
        return {"tool": row.tool, "status": row.status, "result": json.loads(row.result) if row.result else None,
                "attempts": row.attempts}

    def begin_tool_call(self, key: str, run_id: str, tool: str, args: dict[str, Any]) -> None:
        stmt = self._insert(tool_calls).values(key=key, run_id=run_id, tool=tool, args=json.dumps(args),
                                               status="pending", attempts=1, started_at=time.time())
        stmt = stmt.on_conflict_do_update(index_elements=["key"],
                                          set_={"attempts": tool_calls.c.attempts + 1,
                                                "started_at": stmt.excluded.started_at})
        with self.engine.begin() as c:
            c.execute(stmt)

    def finish_tool_call(self, key: str, result: Any) -> None:
        with self.engine.begin() as c:
            c.execute(update(tool_calls).where(tool_calls.c.key == key)
                      .values(status="done", result=json.dumps(result), finished_at=time.time()))

    def discard_tool_call(self, key: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(tool_calls).where(tool_calls.c.key == key, tool_calls.c.status == "pending"))

    def pending_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            rows = c.execute(select(tool_calls.c.key, tool_calls.c.tool, tool_calls.c.args, tool_calls.c.attempts,
                                    tool_calls.c.started_at)
                             .where(tool_calls.c.run_id == run_id, tool_calls.c.status == "pending")).all()
        return [{"key": k, "tool": t, "args": json.loads(a), "attempts": n, "started_at": ts} for k, t, a, n, ts in rows]


__all__ = ["Event", "Lease", "LeaseLost", "RunRecord", "RunStore", "database_url", "metadata"]
