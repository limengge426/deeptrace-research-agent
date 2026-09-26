"""HTTP API tests (FastAPI TestClient, scripted LLM and search)."""

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from deeptrace_agent import Budget, ResearchRuntime, RunStore  # noqa: E402
from deeptrace_agent.server import create_app  # noqa: E402

from .fakes import FakeSearch, ScriptedLLM  # noqa: E402


def _client(tmp_path, **kw) -> TestClient:
    budget = kw.pop("budget", None)
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "api.db"), owner="api", budget=budget)
    return TestClient(create_app(runtime, poll_interval=0.02, **kw))


def _wait(client, run_id, statuses, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.02)
    raise AssertionError(f"run stuck in {body['status']}")


def _sse(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append(fields)
    return events


def test_submit_run_follow_events_and_fetch_report(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/health").json()["status"] == "ok"

        created = client.post("/runs", json={"question": "Are heat pumps worth it?"})
        assert created.status_code == 202
        run_id = created.json()["id"]

        run = _wait(client, run_id, {"done"})
        assert run["evidence"] > 0 and all(t["status"] == "done" for t in run["tasks"])

        report = client.get(f"/runs/{run_id}/report")
        assert report.status_code == 200 and "## Sources" in report.text

        events = _sse(client.get(f"/runs/{run_id}/events").text)
        kinds = [e["event"] for e in events]
        assert kinds[0] == "run_created" and "run_claimed" in kinds and kinds[-1] == "end"

        # Reconnect with Last-Event-ID: only later events are replayed.
        middle = events[len(events) // 2]["id"]
        replay = _sse(client.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": middle}).text)
        assert all(int(e["id"]) > int(middle) for e in replay if "id" in e)

        assert run_id in [r["id"] for r in client.get("/runs").json()]


def test_queued_run_has_no_report_and_unknown_run_is_404(tmp_path):
    with _client(tmp_path, embedded_worker=False) as client:
        run_id = client.post("/runs", json={"question": "Are heat pumps worth it?"}).json()["id"]
        assert client.get(f"/runs/{run_id}").json()["status"] == "plan"
        assert client.get(f"/runs/{run_id}/report").status_code == 409
        assert client.get("/runs/nope").status_code == 404
        assert client.post("/runs", json={"question": ""}).status_code == 422


def test_halted_run_can_be_resumed_through_the_api(tmp_path):
    with _client(tmp_path, budget=Budget(max_tool_calls=2)) as client:
        run_id = client.post("/runs", json={"question": "Are heat pumps worth it?"}).json()["id"]
        halted = _wait(client, run_id, {"halted"})
        assert "tool call budget" in halted["error"]

        assert client.post(f"/runs/{run_id}/resume").status_code == 202
        assert client.get(f"/runs/{run_id}").json()["status"] != "halted"
