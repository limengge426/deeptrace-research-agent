"""SQLite persistence: run checkpoints, an append-only event log and a tool-call cache."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import RunState

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


@dataclass
class Event:
    ts: float
    kind: str
    payload: dict[str, Any]


class RunStore:
    def __init__(self, path: str | Path = "researchloop.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- runs -------------------------------------------------------------

    def create_run(self, state: RunState) -> str:
        run_id = uuid.uuid4().hex[:10]
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, state.question, state.stage, now, now, json.dumps(state.to_dict())),
            )
        return run_id

    def checkpoint(self, run_id: str, state: RunState, *, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET status = ?, updated_at = ?, state = ? WHERE id = ?",
                (status, time.time(), json.dumps(state.to_dict()), run_id),
            )

    def load(self, run_id: str) -> RunState:
        row = self._conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run with id {run_id!r}")
        return RunState.from_dict(json.loads(row[0]))

    def runs(self, limit: int = 20) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT id, question, status, created_at, updated_at FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [RunRecord(*row) for row in rows]

    # -- events -----------------------------------------------------------

    def log(self, run_id: str, kind: str, **payload: Any) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO events (run_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
                (run_id, time.time(), kind, json.dumps(payload, ensure_ascii=False)),
            )

    def events(self, run_id: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT ts, kind, payload FROM events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [Event(ts, kind, json.loads(payload)) for ts, kind, payload in rows]

    # -- idempotent tool calls ---------------------------------------------

    @staticmethod
    def tool_key(run_id: str, tool: str, args: dict[str, Any]) -> str:
        blob = json.dumps({"run": run_id, "tool": tool, "args": args}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def cached(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT result FROM tool_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, key: str, run_id: str, result: Any) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO tool_cache VALUES (?, ?, ?)", (key, run_id, json.dumps(result))
            )
