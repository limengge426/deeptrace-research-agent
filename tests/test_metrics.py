"""Per-run metrics, cross-run aggregation, and the metrics endpoints."""

import asyncio

import pytest

from researchloop import ResearchRuntime, RunStore
from researchloop.metrics import run_metrics, summarize, to_prometheus

from . import fakes
from .fakes import FakeSearch, ScriptedLLM


def test_run_records_llm_tool_and_stage_metrics(tmp_path):
    search = FakeSearch()
    runtime = ResearchRuntime(ScriptedLLM(), search, RunStore(tmp_path / "r.db"))
    result = asyncio.run(runtime.start("Are heat pumps worth it?"))
    m = run_metrics(result.state)

    assert {"plan", "queries", "finding", "outline", "section", "synthesis", "judge"} <= set(m["llm"])
    assert m["llm"]["queries"]["calls"] == 3 and m["llm"]["plan"]["tokens"] == 100
    assert sum(v["tokens"] for v in m["llm"].values()) == result.state.usage["tokens"]
    assert m["tools"]["fake"]["calls"] == sum(search.calls.values())
    assert m["tools"]["fake"]["cache_hits"] == 0
    assert list(m["stages"]) == ["plan", "execute", "report", "verify"]
    assert m["stages"]["execute"]["steps"] == 3  # two waves plus the transition step
    assert m["derived"]["support_rate_final"] == 1.0


def test_metrics_survive_a_crash_and_count_cache_hits_on_resume(tmp_path):
    crash = {"armed": True}

    def finding(system, user):
        if "cold climates" in user and crash["armed"]:
            crash["armed"] = False
            raise KeyboardInterrupt
        return fakes.finding(system, user)

    runtime = ResearchRuntime(ScriptedLLM(finding=finding), FakeSearch(), RunStore(tmp_path / "r.db"))
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(runtime.start("Are heat pumps worth it?"))
    run_id = runtime.store.runs()[0].id
    before = run_metrics(runtime.store.load(run_id))

    result = asyncio.run(runtime.resume(run_id))
    after = run_metrics(result.state)
    assert after["llm"]["plan"]["calls"] == before["llm"]["plan"]["calls"] == 1  # carried over, not reset
    assert after["tools"]["fake"]["cache_hits"] == 2  # t3's two searches replayed from the cache
    assert after["derived"]["tool_cache_hit_rate"] > 0


def test_llm_errors_are_counted(tmp_path):
    def finding(system, user):
        if "efficient" in user:
            raise RuntimeError("boom")
        return fakes.finding(system, user)

    runtime = ResearchRuntime(ScriptedLLM(finding=finding), FakeSearch(), RunStore(tmp_path / "r.db"))
    result = asyncio.run(runtime.start("Are heat pumps worth it?"))
    assert run_metrics(result.state)["llm"]["finding"]["errors"] == 1


def test_summary_aggregates_runs_and_renders_prometheus(tmp_path):
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "r.db"))
    for q in ("Are heat pumps worth it?", "Are heat pumps efficient?"):
        asyncio.run(runtime.start(q))
    runtime.cancel(runtime.create("Cancelled before it started"))

    summary = summarize(runtime.store.states())
    assert summary["runs"] == 3
    assert summary["by_status"] == {"done": 2, "cancelled": 1}
    assert summary["done"]["tokens_p50"] > 0 and summary["done"]["support_rate_final_mean"] == 1.0
    assert summary["llm"]["plan"]["calls"] == 2

    text = to_prometheus(summary)
    assert 'researchloop_runs{status="done"} 2' in text
    assert 'researchloop_llm_tokens_total{purpose="plan"} 200' in text
    assert "# TYPE researchloop_tool_calls_total counter" in text


def test_metrics_endpoints(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from researchloop.server import create_app

    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "api.db"), owner="api")
    run_id = asyncio.run(runtime.start("Are heat pumps worth it?")).run_id
    with TestClient(create_app(runtime, embedded_worker=False)) as client:
        per_run = client.get(f"/runs/{run_id}/metrics").json()
        assert per_run["status"] == "done" and per_run["llm"]["section"]["calls"] >= 1
        assert client.get("/metrics").json()["by_status"] == {"done": 1}
        prom = client.get("/metrics", params={"format": "prometheus"})
        assert prom.headers["content-type"].startswith("text/plain")
        assert 'researchloop_runs{status="done"} 1' in prom.text
        assert client.get("/metrics", params={"format": "xml"}).status_code == 422
