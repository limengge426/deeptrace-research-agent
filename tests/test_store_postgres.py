"""The store on Postgres: migrations, fencing, concurrent lease acquisition, upserts, workers.

Runs when DEEPTRACE_TEST_POSTGRES_URL is set (CI starts a Postgres service), e.g.
    DEEPTRACE_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/deeptrace pytest
"""

import asyncio
import os
import threading
import time

import pytest

URL = os.getenv("DEEPTRACE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="DEEPTRACE_TEST_POSTGRES_URL not set")


@pytest.fixture
def pg():
    from sqlalchemy import create_engine, text

    from deeptrace_agent.schema import metadata
    from deeptrace_agent.store import RunStore

    engine = create_engine(URL)
    with engine.begin() as c:
        metadata.drop_all(c)
        c.execute(text("DROP TABLE IF EXISTS alembic_version"))
    engine.dispose()
    store = RunStore(URL)
    yield store
    store.close()


def test_migrations_create_the_schema(pg):
    from sqlalchemy import inspect

    assert {"runs", "events", "leases", "controls", "tool_calls", "alembic_version"} <= set(
        inspect(pg.engine).get_table_names())


def test_fencing_rejects_a_stale_owner(pg):
    from deeptrace_agent.models import RunState
    from deeptrace_agent.store import LeaseLost

    run_id = pg.create_run(RunState("q"))
    stale = pg.acquire(run_id, "A", ttl=0.05)
    time.sleep(0.1)
    fresh = pg.acquire(run_id, "B", ttl=5)
    assert fresh.token == stale.token + 1
    with pytest.raises(LeaseLost):
        pg.checkpoint(run_id, RunState("q", stage="report"), status="report", lease=stale)
    pg.checkpoint(run_id, RunState("q", stage="execute"), status="execute", lease=fresh)
    assert pg.status(run_id) == "execute"


def test_exactly_one_of_many_concurrent_acquirers_wins(pg):
    from deeptrace_agent.models import RunState
    from deeptrace_agent.store import RunStore

    run_id = pg.create_run(RunState("q"))
    stores = [RunStore(URL, migrate=False) for _ in range(16)]
    barrier = threading.Barrier(len(stores))
    wins = []

    def race(i, store):
        barrier.wait()
        lease = store.acquire(run_id, f"worker-{i}", ttl=30)
        if lease:
            wins.append(lease)

    threads = [threading.Thread(target=race, args=(i, s)) for i, s in enumerate(stores)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in stores:
        s.close()
    assert len(wins) == 1 and wins[0].token == 1


def test_upserts_for_control_flags_and_tool_intents(pg):
    from deeptrace_agent.models import RunState

    run_id = pg.create_run(RunState("q"))
    pg.request_control(run_id, "pause")
    pg.request_control(run_id, "cancel")
    assert pg.take_control(run_id) == "cancel" and pg.take_control(run_id) is None

    key = pg.tool_key(run_id, "search", {"q": 1})
    pg.begin_tool_call(key, run_id, "search", {"q": 1})
    pg.begin_tool_call(key, run_id, "search", {"q": 1})
    assert pg.tool_record(key)["attempts"] == 2
    pg.finish_tool_call(key, {"hits": []})
    assert pg.tool_record(key)["status"] == "done" and pg.pending_tool_calls(run_id) == []


def test_two_workers_on_postgres_run_every_run_once(pg):
    from deeptrace_agent import ResearchRuntime
    from deeptrace_agent.store import RunStore
    from deeptrace_agent.worker import Worker

    from .fakes import FakeSearch, ScriptedLLM

    def runtime(owner):
        return ResearchRuntime(ScriptedLLM(), FakeSearch(delay=0.02), RunStore(URL, migrate=False), owner=owner)

    run_ids = [runtime("api").create(f"Question {i} about heat pumps") for i in range(6)]
    workers = [Worker(runtime(name), max_active=2, poll_interval=0.01) for name in ("w1", "w2")]

    async def main():
        await asyncio.gather(*(w.run_until_idle(timeout=30) for w in workers))

    asyncio.run(main())
    assert {pg.status(r) for r in run_ids} == {"done"}
    for r in run_ids:
        assert sum(e.kind == "run_claimed" for e in pg.events(r)) == 1
