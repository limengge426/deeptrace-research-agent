"""Leases, fencing tokens and multi-worker execution."""

import asyncio
import threading
import time

import pytest

from researchloop import ResearchRuntime, RunStore
from researchloop.models import RunState
from researchloop.runtime import RunLocked
from researchloop.store import LeaseLost
from researchloop.worker import Worker

from . import fakes
from .fakes import FakeSearch, ScriptedLLM


@pytest.fixture
def db(tmp_path):
    return tmp_path / "runs.db"


def _runtime(db, owner, llm=None, search_delay=0.0, **kw):
    return ResearchRuntime(llm or ScriptedLLM(), FakeSearch(delay=search_delay), RunStore(db), owner=owner, **kw)


def test_lease_is_exclusive_until_it_expires(db):
    a, b = RunStore(db), RunStore(db)
    run_id = a.create_run(RunState("q"))

    lease_a = a.acquire(run_id, "A", ttl=0.2)
    assert lease_a.token == 1
    assert b.acquire(run_id, "B", ttl=0.2) is None
    assert b.lease_holder(run_id)[0] == "A"
    assert b.claimable() == []

    time.sleep(0.25)
    assert b.claimable() == [run_id]
    lease_b = b.acquire(run_id, "B", ttl=5)
    assert lease_b.token == 2  # takeover bumps the fencing token


def test_stale_owner_cannot_write_after_takeover(db):
    a, b = RunStore(db), RunStore(db)
    run_id = a.create_run(RunState("q"))
    lease_a = a.acquire(run_id, "A", ttl=0.05)
    time.sleep(0.1)
    lease_b = b.acquire(run_id, "B", ttl=5)

    stale = RunState("q", stage="report")
    with pytest.raises(LeaseLost):
        a.checkpoint(run_id, stale, status="report", lease=lease_a)
    assert not a.renew(lease_a, ttl=5)

    b.checkpoint(run_id, RunState("q", stage="execute"), status="execute", lease=lease_b)
    assert a.load(run_id).stage == "execute"


def test_stalled_worker_is_fenced_off_and_new_owner_finishes(db):
    """A worker freezes (think GC pause or a suspended VM) past its lease.

    Another worker takes over and finishes the run. When the frozen worker
    wakes up, its next checkpoint is rejected, so it cannot clobber the result.
    """
    frozen, resume_frozen = threading.Event(), threading.Event()

    def section(system, user):
        if not frozen.is_set():
            frozen.set()
            resume_frozen.wait(5)  # blocks the event loop: no heartbeat can run
        return fakes.section(system, user)

    stalled = _runtime(db, "stalled", ScriptedLLM(section=section), lease_ttl=0.3)
    outcome = {}

    def run_stalled():
        try:
            outcome["result"] = asyncio.run(stalled.start("Are heat pumps worth it?"))
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=run_stalled)
    thread.start()
    assert frozen.wait(5)
    time.sleep(0.4)  # let the stalled worker's lease expire

    rescuer = _runtime(db, "rescuer", lease_ttl=5)
    run_id = rescuer.store.runs()[0].id
    rescued = asyncio.run(rescuer.resume(run_id))
    assert rescued.status == "done"

    resume_frozen.set()
    thread.join(5)
    assert isinstance(outcome.get("error"), LeaseLost)

    final = rescuer.result(run_id)
    assert final.status == "done" and final.markdown == rescued.markdown
    kinds = [e.kind for e in rescuer.store.events(run_id)]
    assert "run_abandoned" in kinds
    assert kinds.count("run_finished") == 1


def test_resume_refuses_a_run_held_by_a_live_worker(db):
    store = RunStore(db)
    run_id = store.create_run(RunState("q"))
    store.acquire(run_id, "someone-else", ttl=30)
    with pytest.raises(RunLocked, match="someone-else"):
        asyncio.run(_runtime(db, "me").resume(run_id))


def test_two_workers_split_the_queue_and_each_run_executes_once(db):
    submitter = _runtime(db, "api")
    run_ids = [submitter.create(f"Question {i}: are heat pumps worth it?") for i in range(6)]
    workers = [
        Worker(_runtime(db, name, search_delay=0.02), max_active=2, poll_interval=0.01) for name in ("w1", "w2")
    ]

    async def main():
        await asyncio.gather(*(w.run_until_idle(timeout=20) for w in workers))

    asyncio.run(main())

    store = submitter.store
    assert {store.status(r) for r in run_ids} == {"done"}
    for r in run_ids:
        claims = [e for e in store.events(r) if e.kind == "run_claimed"]
        assert len(claims) == 1
    assert all(w.completed for w in workers)  # both workers did real work
    assert sorted(workers[0].completed + workers[1].completed) == sorted(run_ids)


def test_worker_takes_over_run_orphaned_by_a_dead_process(db):
    store = RunStore(db)
    run_id = _runtime(db, "api").create("Are heat pumps worth it?")
    store.acquire(run_id, "dead-worker", ttl=0.2)  # died without releasing

    worker = Worker(_runtime(db, "survivor"), poll_interval=0.05)
    asyncio.run(worker.run_until_idle(timeout=5))

    assert store.status(run_id) == "done"
    claim = [e for e in store.events(run_id) if e.kind == "run_claimed"][0]
    assert claim.payload["owner"] == "survivor" and claim.payload["token"] == 2


def test_worker_gives_up_after_max_attempts(db):
    def plan(system, user):
        raise RuntimeError("provider is down")

    runtime = _runtime(db, "w", ScriptedLLM(plan=plan), max_attempts=3)
    run_id = runtime.create("Are heat pumps worth it?")
    asyncio.run(Worker(runtime, poll_interval=0.01, retry_backoff=0).run_until_idle(timeout=5))

    assert runtime.store.status(run_id) == "failed"
    assert "gave up after 3 attempts" in runtime.store.load(run_id).error
    assert sum(e.kind == "worker_error" for e in runtime.store.events(run_id)) == 3
