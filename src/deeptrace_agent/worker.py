"""Worker: claims unowned runs from the shared database and drives them.

Any number of workers (processes, containers, machines sharing the SQLite file
on one host) can poll the same database. Leases make sure each run is driven by
exactly one worker at a time, and a run whose worker died is picked up by
another one once the dead worker's lease expires.
"""

from __future__ import annotations

import asyncio
import time

from .runtime import ResearchRuntime, RunLocked
from .store import LeaseLost


class Worker:
    def __init__(
        self,
        runtime: ResearchRuntime,
        *,
        max_active: int = 2,
        poll_interval: float = 1.0,
        retry_backoff: float = 5.0,
    ) -> None:
        self.runtime = runtime
        self.store = runtime.store
        self.max_active = max_active
        self.poll_interval = poll_interval
        self.retry_backoff = retry_backoff
        self.active: dict[str, asyncio.Task] = {}
        self._retry_after: dict[str, float] = {}
        self.completed: list[str] = []

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Poll until ``stop`` is set."""
        stop = stop or asyncio.Event()
        try:
            while not stop.is_set():
                self.poll()
                try:
                    await asyncio.wait_for(stop.wait(), self.poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            for task in self.active.values():
                task.cancel()
            await asyncio.gather(*self.active.values(), return_exceptions=True)

    async def run_until_idle(self, timeout: float = 30.0) -> None:
        """Process runs until no unfinished run is left (used by tests and one-shot jobs).

        Runs held by another worker count as unfinished, so this also waits for
        orphaned leases to expire and be taken over.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if not self.active and not self._unfinished():
                return
            await asyncio.sleep(self.poll_interval)
        raise TimeoutError("worker did not become idle")

    def poll(self) -> None:
        free = self.max_active - len(self.active)
        for run_id in self._claimable()[: max(free, 0)]:
            self.active[run_id] = asyncio.create_task(self._drive(run_id))

    def _unfinished(self) -> list[str]:
        return self.store.pending()

    def _claimable(self) -> list[str]:
        now = time.monotonic()
        return [
            r
            for r in self.store.claimable(limit=self.max_active * 4)
            if r not in self.active and self._retry_after.get(r, 0) <= now
        ]

    async def _drive(self, run_id: str) -> None:
        try:
            result = await self.runtime.resume(run_id)
            if result.status in ("done", "failed"):
                self.completed.append(run_id)
        except (RunLocked, LeaseLost):
            pass  # another worker got there first, or took the run over
        except Exception as exc:  # noqa: BLE001 - keep the worker alive; the run records the error
            self.store.log(run_id, "worker_error", owner=self.runtime.owner, error=f"{type(exc).__name__}: {exc}")
            self._retry_after[run_id] = time.monotonic() + self.retry_backoff
        finally:
            self.active.pop(run_id, None)
