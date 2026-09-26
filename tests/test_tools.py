"""Tool runner (write-ahead intents), page fetching, and idempotent report delivery."""

import asyncio
import json

import httpx
import pytest

from researchloop import ResearchRuntime, RunStore
from researchloop.budget import Budget, BudgetMeter
from researchloop.models import RunState
from researchloop.runtime import InvalidAction
from researchloop.tools import (
    FetchPageTool,
    ToolOutcomeUnknown,
    ToolRunner,
    WebhookTool,
    best_passages,
    html_to_text,
)

from .fakes import FakeSearch, ScriptedLLM


class CountingTool:
    def __init__(self, name="t", side_effects=False, idempotent=True, fail=False):
        self.name, self.side_effects, self.idempotent, self.fail = name, side_effects, idempotent, fail
        self.keys: list[str] = []

    async def __call__(self, args, *, idempotency_key):
        self.keys.append(idempotency_key)
        if self.fail:
            raise RuntimeError("provider error")
        return {"echo": args["x"]}


def _runner(tmp_path):
    store = RunStore(tmp_path / "t.db")
    run_id = store.create_run(RunState("q"))
    return store, run_id, ToolRunner(store, BudgetMeter(Budget()), run_id)


def _interrupt(store, run_id, tool, args):
    """Simulate a process that recorded the intent and died before recording the result."""
    store.begin_tool_call(store.tool_key(run_id, tool.name, args), run_id, tool.name, args)


def test_completed_calls_are_replayed_not_repeated(tmp_path):
    store, run_id, runner = _runner(tmp_path)
    tool = CountingTool()
    assert asyncio.run(runner.call(tool, {"x": 1})) == {"echo": 1}
    assert asyncio.run(runner.call(tool, {"x": 1})) == {"echo": 1}
    assert len(tool.keys) == 1
    assert runner.meter.metrics.tools["t"]["cache_hits"] == 1


@pytest.mark.parametrize("side_effects, idempotent", [(False, True), (True, True)])
def test_interrupted_safe_calls_are_retried_with_the_same_key(tmp_path, side_effects, idempotent):
    store, run_id, runner = _runner(tmp_path)
    tool = CountingTool(side_effects=side_effects, idempotent=idempotent)
    _interrupt(store, run_id, tool, {"x": 2})

    assert asyncio.run(runner.call(tool, {"x": 2})) == {"echo": 2}
    assert tool.keys == [store.tool_key(run_id, "t", {"x": 2})]
    assert store.tool_record(tool.keys[0])["attempts"] == 2
    assert [e.kind for e in store.events(run_id)] == ["tool_retry_after_crash"]


def test_interrupted_non_idempotent_side_effect_is_never_retried(tmp_path):
    store, run_id, runner = _runner(tmp_path)
    tool = CountingTool(side_effects=True, idempotent=False)
    _interrupt(store, run_id, tool, {"x": 3})
    with pytest.raises(ToolOutcomeUnknown):
        asyncio.run(runner.call(tool, {"x": 3}))
    assert tool.keys == []


def test_failed_reads_leave_no_intent_but_failed_side_effects_stay_pending(tmp_path):
    store, run_id, runner = _runner(tmp_path)
    for side_effects in (False, True):
        tool = CountingTool(name=f"t{side_effects}", side_effects=side_effects, fail=True)
        with pytest.raises(RuntimeError):
            asyncio.run(runner.call(tool, {"x": 4}))
    assert [c["tool"] for c in store.pending_tool_calls(run_id)] == ["tTrue"]


# -- fetching -------------------------------------------------------------------

PAGE = """<html><head><title>Heat pumps &amp; winter</title><script>var x = 1;</script></head>
<body><nav>Home | About</nav><h1>Heat pumps</h1><p>Some intro text about the site.</p>
<p>In cold climates, the coefficient of performance drops as outdoor temperature falls.</p>
<footer>Copyright</footer></body></html>"""


def test_html_to_text_drops_scripts_and_navigation():
    title, text = html_to_text(PAGE)
    assert title == "Heat pumps & winter"
    assert "var x" not in text and "Home | About" not in text and "Copyright" not in text
    assert "coefficient of performance drops" in text


def test_best_passages_prefers_relevant_chunks():
    text = "\n\n".join(["Unrelated paragraph about cooking pasta." * 5,
                        "Heat pump efficiency in cold climates falls as temperature drops." * 3,
                        "Another unrelated paragraph about gardening." * 5])
    best = best_passages(text, "heat pump efficiency cold", chunk_chars=250, top=1)
    assert "Heat pump efficiency" in best and "pasta" not in best


def _fetch_tool(routes):
    def handler(request):
        status, body, kind = routes.get(str(request.url), (404, "missing", "text/plain"))
        return httpx.Response(status, text=body, headers={"content-type": kind})

    return FetchPageTool(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_fetch_tool_extracts_text_and_rejects_non_web_urls():
    tool = _fetch_tool({"https://example.org/p": (200, PAGE, "text/html; charset=utf-8")})
    page = asyncio.run(tool({"url": "https://example.org/p"}, idempotency_key="k"))
    assert page["title"] == "Heat pumps & winter" and "coefficient" in page["text"]
    with pytest.raises(ValueError):
        asyncio.run(tool({"url": "file:///etc/passwd"}, idempotency_key="k"))


def test_web_hits_are_enriched_with_page_passages_and_failures_keep_the_snippet(tmp_path):
    ok = "https://example.org/what-is-a-heat-pump/0"
    routes = {ok: (200, PAGE.replace("In cold climates", "What is a heat pump? In cold climates"), "text/html")}
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "r.db"),
                              fetch=_fetch_tool(routes), fetch_pages=1)
    result = asyncio.run(runtime.start("Are heat pumps worth it?"))

    assert result.status == "done"
    enriched = [e for e in result.state.evidence if e.url == ok][0]
    assert "coefficient of performance drops" in enriched.content
    assert any(e.kind == "fetch_failed" for e in runtime.store.events(result.run_id))
    assert result.state.metrics["tools"]["fetch"]["calls"] >= 2


# -- delivery -------------------------------------------------------------------


class Receiver:
    """A webhook endpoint that honors Idempotency-Key, like Stripe-style APIs."""

    def __init__(self):
        self.raw: list[str] = []
        self.effective: dict[str, dict] = {}

    def handler(self, request):
        key = request.headers["Idempotency-Key"]
        self.raw.append(key)
        self.effective.setdefault(key, json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def tool(self):
        return WebhookTool(httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


def test_finished_report_is_delivered_once_with_an_idempotency_key(tmp_path):
    receiver = Receiver()
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "r.db"), webhook=receiver.tool())
    result = asyncio.run(runtime.start("Are heat pumps worth it?", deliver_to="https://hooks.example/r"))

    assert result.status == "done" and result.state.delivery == {"status": "delivered"}
    assert len(receiver.raw) == 1
    payload = next(iter(receiver.effective.values()))
    assert payload["run_id"] == result.run_id and "## Sources" in payload["markdown"] and payload["sources"]


def test_delivery_interrupted_after_sending_is_deduplicated_by_the_receiver(tmp_path):
    receiver = Receiver()
    crash = {"armed": True}

    class CrashAfterSend(WebhookTool):
        async def __call__(self, args, *, idempotency_key):
            out = await super().__call__(args, idempotency_key=idempotency_key)
            if crash["armed"]:
                crash["armed"] = False
                raise KeyboardInterrupt  # process dies before the result is recorded
            return out

    tool = CrashAfterSend(httpx.AsyncClient(transport=httpx.MockTransport(receiver.handler)))
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "r.db"), webhook=tool)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(runtime.start("Are heat pumps worth it?", deliver_to="https://hooks.example/r"))
    run_id = runtime.store.runs()[0].id
    result = asyncio.run(runtime.resume(run_id))

    assert result.status == "done" and result.state.delivery == {"status": "delivered"}
    assert len(receiver.raw) == 2  # sent twice...
    assert len(receiver.effective) == 1  # ...processed once
    assert receiver.raw[0] == receiver.raw[1]


def test_failing_delivery_is_recorded_without_losing_the_report(tmp_path):
    tool = WebhookTool(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "r.db"), webhook=tool,
                              delivery_retries=2)
    result = asyncio.run(runtime.start("Are heat pumps worth it?", deliver_to="https://hooks.example/r"))
    assert result.status == "done" and result.markdown
    assert result.state.delivery["status"] == "failed" and "503" in result.state.delivery["error"]


def test_non_idempotent_side_effect_holds_the_run_until_a_human_reconciles(tmp_path):
    sends = []
    crash = {"armed": True}

    class LegacyWebhook:
        name, side_effects, idempotent = "legacy", True, False

        async def __call__(self, args, *, idempotency_key):
            sends.append(args["url"])
            if crash["armed"]:
                crash["armed"] = False
                raise KeyboardInterrupt
            return {"ok": True}

    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "r.db"), webhook=LegacyWebhook())
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(runtime.start("Are heat pumps worth it?", deliver_to="https://legacy.example/r"))
    run_id = runtime.store.runs()[0].id

    held = asyncio.run(runtime.resume(run_id))
    assert held.status == "needs_reconciliation" and len(sends) == 1  # not retried
    assert runtime.store.claimable() == []
    [pending] = runtime.store.pending_tool_calls(run_id)

    with pytest.raises(InvalidAction):
        runtime.resolve_tool_call(run_id, "not-a-key", "done")
    assert runtime.resolve_tool_call(run_id, pending["key"], "done") == "deliver"
    result = asyncio.run(runtime.resume(run_id))
    assert result.status == "done" and len(sends) == 1  # confirmed as sent: never sent again


def test_api_accepts_a_webhook_and_exposes_reconciliation(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from researchloop.server import create_app

    receiver = Receiver()
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "a.db"), owner="api",
                              webhook=receiver.tool())
    with TestClient(create_app(runtime, poll_interval=0.02)) as client:
        assert client.post("/runs", json={"question": "Heat pumps?", "webhook_url": "ftp://x"}).status_code == 422
        run_id = client.post("/runs", json={"question": "Are heat pumps worth it?",
                                            "webhook_url": "https://hooks.example/r"}).json()["id"]
        for _ in range(250):
            body = client.get(f"/runs/{run_id}").json()
            if body["status"] == "done":
                break
            asyncio.run(asyncio.sleep(0.02))
        assert body["delivery"] == {"status": "delivered"} and len(receiver.effective) == 1
        assert client.get(f"/runs/{run_id}/tool-calls/pending").json() == []
        bad = client.post(f"/runs/{run_id}/tool-calls/abc/resolve", json={"outcome": "maybe"})
        assert bad.status_code == 422
