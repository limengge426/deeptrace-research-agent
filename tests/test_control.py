"""Human control: cancel, pause/resume, and plan approval."""

import asyncio
import json

import pytest

from researchloop import ResearchRuntime, RunStore, cli
from researchloop.llm import OpenAICompatLLM
from researchloop.runtime import InvalidAction
from researchloop.worker import Worker

from .fakes import FakeSearch, ScriptedLLM


@pytest.fixture
def db(tmp_path):
    return tmp_path / "runs.db"


def _rt(db, owner="w", search=None, llm=None):
    return ResearchRuntime(llm or ScriptedLLM(), search or FakeSearch(), RunStore(db), owner=owner)


async def _when(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def test_cancel_idle_run_applies_immediately_and_workers_skip_it(db):
    api = _rt(db, "api")
    run_id = api.create("Are heat pumps worth it?")
    assert api.cancel(run_id) == "cancelled"
    assert api.store.claimable() == []
    result = asyncio.run(_rt(db).resume(run_id))
    assert result.status == "cancelled" and result.state.plan.tasks == []
    with pytest.raises(InvalidAction):
        api.cancel(run_id)


@pytest.mark.parametrize("action, expected", [("pause", "paused"), ("cancel", "cancelled")])
def test_running_worker_stops_at_the_next_step_boundary(db, action, expected):
    search = FakeSearch(delay=0.05)
    worker, api = _rt(db, "worker", search), _rt(db, "api")

    async def scenario():
        run_id = api.create("Are heat pumps worth it?")
        task = asyncio.create_task(worker.resume(run_id))
        await _when(lambda: search.calls)  # mid-execute
        assert getattr(api, action)(run_id) == "execute"  # worker holds the lease: request is queued
        return run_id, await task

    run_id, result = asyncio.run(scenario())
    assert result.status == expected
    assert result.state.stage == ("execute" if action == "pause" else "cancelled")
    assert result.markdown is None
    kinds = [e.kind for e in api.store.events(run_id)]
    assert kinds.index("control_requested") < kinds.index("control_applied")


def test_paused_run_resumes_where_it_stopped_without_repeating_searches(db):
    search = FakeSearch(delay=0.05)
    worker, api = _rt(db, "worker", search), _rt(db, "api")

    async def scenario():
        run_id = api.create("Are heat pumps worth it?")
        task = asyncio.create_task(worker.resume(run_id))
        await _when(lambda: search.calls)
        api.pause(run_id)
        paused = await task
        assert paused.status == "paused" and api.store.claimable() == []
        done_before = [t.id for t in paused.state.plan.tasks if t.status == "done"]

        assert api.unpause(run_id) == "execute"
        await Worker(worker, poll_interval=0.01).run_until_idle(timeout=10)
        return run_id, done_before

    run_id, done_before = asyncio.run(scenario())
    assert done_before  # the first wave finished before the pause took effect
    assert api.store.status(run_id) == "done"
    assert max(search.calls.values()) == 1


def test_plan_approval_holds_the_run_and_accepts_an_edited_plan(db):
    runtime = _rt(db)
    held = asyncio.run(runtime.start("Are heat pumps worth it?", approve_plan=True))
    assert held.status == "awaiting_approval"
    assert [t.id for t in held.state.plan.tasks] == ["t1", "t2", "t3"]
    assert held.state.findings == {} and runtime.store.claimable() == []

    with pytest.raises(InvalidAction, match="cycle"):
        runtime.approve_plan(held.run_id, [
            {"id": "a", "question": "What is a heat pump?", "depends_on": ["b"]},
            {"id": "b", "question": "How efficient are they?", "depends_on": ["a"]},
        ])
    with pytest.raises(InvalidAction, match="invalid plan"):
        runtime.approve_plan(held.run_id, [{"question": "missing id"}])

    edited = [
        {"id": "a", "question": "What is a heat pump?"},
        {"id": "b", "question": "Are heat pumps worth it in cold climates?", "depends_on": ["a"]},
    ]
    runtime.approve_plan(held.run_id, edited)
    result = asyncio.run(runtime.resume(held.run_id))

    assert result.status == "done"
    assert sorted(result.state.findings) == ["a", "b"]
    approved = [e for e in runtime.store.events(held.run_id) if e.kind == "plan_approved"][0]
    assert approved.payload["edited"] is True
    with pytest.raises(InvalidAction):
        runtime.approve_plan(held.run_id)


def test_api_plan_approval_and_cancel_flow(tmp_path):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    from researchloop.server import create_app

    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "api.db"), owner="api")
    with TestClient(create_app(runtime, poll_interval=0.02)) as client:
        run_id = client.post("/runs", json={"question": "Are heat pumps worth it?", "approve_plan": True}).json()["id"]
        for _ in range(250):
            body = client.get(f"/runs/{run_id}").json()
            if body["status"] == "awaiting_approval":
                break
            asyncio.run(asyncio.sleep(0.02))
        assert body["status"] == "awaiting_approval" and len(body["tasks"]) == 3

        assert client.post(f"/runs/{run_id}/plan/approve", json={"tasks": [
            {"id": "x", "question": "x", "depends_on": []}]}).status_code == 422  # question too short
        assert client.post(f"/runs/{run_id}/plan/approve").status_code == 202
        for _ in range(250):
            if client.get(f"/runs/{run_id}").json()["status"] == "done":
                break
            asyncio.run(asyncio.sleep(0.02))
        assert client.get(f"/runs/{run_id}").json()["status"] == "done"
        assert client.post(f"/runs/{run_id}/cancel").status_code == 409

        other = client.post("/runs", json={"question": "Another heat pump question"}).json()["id"]
        assert client.post(f"/runs/{other}/cancel").json()["status"] in ("cancelled", "plan", "execute")
        assert client.post("/runs/nope/pause").status_code == 404


def test_cli_approve_plan_round_trip(tmp_path, monkeypatch, capsys):
    llm = ScriptedLLM()
    llm.aclose = lambda: asyncio.sleep(0)
    monkeypatch.setattr(OpenAICompatLLM, "from_env", classmethod(lambda cls: llm))
    db = str(tmp_path / "runs.db")
    corpus = str(tmp_path.parent / "corpus")
    from pathlib import Path

    Path(corpus).mkdir(exist_ok=True)
    (Path(corpus) / "pumps.md").write_text("# Heat pumps\n\nA heat pump moves heat. It works in cold climates too.")
    common = ["--db", db, "--corpus", corpus, "--out", str(tmp_path / "out")]

    assert cli.main(["run", "Are heat pumps worth it?", "--approve-plan", *common]) == 0
    out = capsys.readouterr().out
    run_id = out.split("run ", 1)[1].split(":", 1)[0]
    assert "proposed plan:" in out and "t3:" in out

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"id": "only", "question": "What is a heat pump?"}]))
    assert cli.main(["approve", run_id, "--db", db, "--plan", str(plan)]) == 0
    assert cli.main(["resume", run_id, *common]) == 0
    assert "report:" in capsys.readouterr().out
