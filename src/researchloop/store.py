"""SQLite persistence: run checkpoints, leases, an append-only event log and a tool-call cache.

Several worker processes may share one database. A run is only driven by the
worker holding its lease, and every checkpoint carries the lease's fencing
token: if a worker stalls long enough for its lease to expire and another
worker takes the run over, the stale worker's next write is rejected instead of
silently overwriting newer progress.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import NOT_CLAIMABLE, RunState

_NOT_CLAIMABLE_SQL = ", ".join(f"'{s}'" for s in NOT_CLAIMABLE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS leases (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    owner TEXT NOT NULL,
    token INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS controls (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    action TEXT NOT NULL,
    requested_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_cache (
    key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


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


class RunStore:
    def __init__(self, path: str | Path = "researchloop.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode: single statements commit on their own, and multi-step
        # operations open an explicit BEGIN IMMEDIATE transaction.
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- runs -------------------------------------------------------------

    def create_run(self, state: RunState) -> str:
        run_id = uuid.uuid4().hex[:10]
        now = time.time()
        self._conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, state.question, state.stage, now, now, json.dumps(state.to_dict())),
        )
        return run_id

    def checkpoint(self, run_id: str, state: RunState, *, status: str, lease: Lease | None = None) -> None:
        params = (status, time.time(), json.dumps(state.to_dict()), run_id)
        if lease is None:
            self._conn.execute("UPDATE runs SET status = ?, updated_at = ?, state = ? WHERE id = ?", params)
            return
        cur = self._conn.execute(
            "UPDATE runs SET status = ?, updated_at = ?, state = ? WHERE id = ? AND EXISTS "
            "(SELECT 1 FROM leases WHERE run_id = ? AND owner = ? AND token = ?)",
            (*params, run_id, lease.owner, lease.token),
        )
        if cur.rowcount == 0:
            raise LeaseLost(f"lease on run {run_id} (token {lease.token}) was taken over")

    def requeue(self, run_id: str) -> None:
        """Make a halted or paused run claimable again by clearing its error and hold."""
        state = self.load(run_id)
        state.error = None
        state.hold = None
        self.checkpoint(run_id, state, status=state.status)

    # -- human control ------------------------------------------------------

    def request_control(self, run_id: str, action: str) -> None:
        """Ask whoever drives the run to "cancel" or "pause" it at the next step boundary."""
        self._conn.execute(
            "INSERT OR REPLACE INTO controls VALUES (?, ?, ?)", (run_id, action, time.time())
        )

    def take_control(self, run_id: str) -> str | None:
        """Atomically read and clear a pending control request."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute("SELECT action FROM controls WHERE run_id = ?", (run_id,)).fetchone()
            if row:
                self._conn.execute("DELETE FROM controls WHERE run_id = ?", (run_id,))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return row[0] if row else None

    def load(self, run_id: str) -> RunState:
        row = self._conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run with id {run_id!r}")
        return RunState.from_dict(json.loads(row[0]))

    def states(self, limit: int = 1000) -> list[RunState]:
        """Most recent run states, newest first (for metrics aggregation)."""
        rows = self._conn.execute("SELECT state FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [RunState.from_dict(json.loads(row[0])) for row in rows]

    def runs(self, limit: int = 20) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT id, question, status, created_at, updated_at FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [RunRecord(*row) for row in rows]

    def status(self, run_id: str) -> str:
        row = self._conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run with id {run_id!r}")
        return row[0]

    # -- leases -----------------------------------------------------------

    def acquire(self, run_id: str, owner: str, ttl: float) -> Lease | None:
        """Take the run's lease if it is free, expired, or already ours.

        Taking over from another owner bumps the fencing token, which
        invalidates any checkpoint the previous owner might still try to write.
        """
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT owner, token, expires_at FROM leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                token = 1
                self._conn.execute("INSERT INTO leases VALUES (?, ?, ?, ?)", (run_id, owner, token, now + ttl))
            elif row[0] == owner and row[2] > now:
                token = row[1]
                self._conn.execute("UPDATE leases SET expires_at = ? WHERE run_id = ?", (now + ttl, run_id))
            elif row[2] <= now:
                token = row[1] + 1
                self._conn.execute(
                    "UPDATE leases SET owner = ?, token = ?, expires_at = ? WHERE run_id = ?",
                    (owner, token, now + ttl, run_id),
                )
            else:
                self._conn.execute("ROLLBACK")
                return None
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return Lease(run_id, owner, token)

    def lease_holder(self, run_id: str) -> tuple[str, float] | None:
        """(owner, seconds until expiry) of a live lease, or None if the run is free."""
        row = self._conn.execute("SELECT owner, expires_at FROM leases WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or row[1] <= time.time():
            return None
        return row[0], row[1] - time.time()

    def renew(self, lease: Lease, ttl: float) -> bool:
        cur = self._conn.execute(
            "UPDATE leases SET expires_at = ? WHERE run_id = ? AND owner = ? AND token = ?",
            (time.time() + ttl, lease.run_id, lease.owner, lease.token),
        )
        return cur.rowcount == 1

    def release(self, lease: Lease) -> None:
        self._conn.execute(
            "UPDATE leases SET expires_at = 0 WHERE run_id = ? AND owner = ? AND token = ?",
            (lease.run_id, lease.owner, lease.token),
        )

    def claimable(self, limit: int = 10) -> list[str]:
        """Unfinished, unpaused runs that nobody currently holds a live lease on."""
        rows = self._conn.execute(
            "SELECT r.id FROM runs r LEFT JOIN leases l ON l.run_id = r.id "
            f"WHERE r.status NOT IN ({_NOT_CLAIMABLE_SQL}) AND (l.run_id IS NULL OR l.expires_at <= ?) "
            "ORDER BY r.created_at LIMIT ?",
            (time.time(), limit),
        ).fetchall()
        return [row[0] for row in rows]

    def pending(self) -> list[str]:
        """Runs that are neither finished nor paused, whether or not someone holds them."""
        rows = self._conn.execute(
            f"SELECT id FROM runs WHERE status NOT IN ({_NOT_CLAIMABLE_SQL}) ORDER BY created_at"
        ).fetchall()
        return [row[0] for row in rows]

    # -- events -----------------------------------------------------------

    def log(self, run_id: str, kind: str, **payload: Any) -> None:
        self._conn.execute(
            "INSERT INTO events (run_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
            (run_id, time.time(), kind, json.dumps(payload, ensure_ascii=False)),
        )

    def events(self, run_id: str, *, after: int = 0) -> list[Event]:
        rows = self._conn.execute(
            "SELECT id, ts, kind, payload FROM events WHERE run_id = ? AND id > ? ORDER BY id", (run_id, after)
        ).fetchall()
        return [Event(eid, ts, kind, json.loads(payload)) for eid, ts, kind, payload in rows]

    # -- idempotent tool calls ---------------------------------------------

    @staticmethod
    def tool_key(run_id: str, tool: str, args: dict[str, Any]) -> str:
        blob = json.dumps({"run": run_id, "tool": tool, "args": args}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def cached(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT result FROM tool_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, key: str, run_id: str, result: Any) -> None:
        self._conn.execute("INSERT OR REPLACE INTO tool_cache VALUES (?, ?, ?)", (key, run_id, json.dumps(result)))
