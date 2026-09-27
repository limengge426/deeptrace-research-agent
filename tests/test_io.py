"""LLM client HTTP behaviour and the CLI, without touching the network."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from deeptrace_agent import cli
from deeptrace_agent.llm import LLMError, OpenAICompatLLM

from .fakes import ScriptedLLM

EXAMPLE_CORPUS = Path(__file__).resolve().parents[1] / "examples" / "corpus"


def _llm_with(handler) -> OpenAICompatLLM:
    llm = OpenAICompatLLM("m", "key", "https://llm.test/v1", max_retries=2)
    llm._client = httpx.AsyncClient(base_url="https://llm.test/v1", transport=httpx.MockTransport(handler))
    return llm


def _ok(text: str, tokens: int = 42) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {"total_tokens": tokens}})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(OpenAICompatLLM, "_sleep", staticmethod(instant))


def test_llm_retries_rate_limits_then_succeeds():
    statuses = iter([429, 503])

    def handler(request):
        status = next(statuses, 200)
        return _ok("hi") if status == 200 else httpx.Response(status)

    result = asyncio.run(_llm_with(handler).complete("s", "u", purpose="t"))
    assert (result.text, result.tokens) == ("hi", 42)


def test_llm_waits_as_long_as_the_rate_limit_headers_ask(monkeypatch):
    waits = []

    async def record(seconds):
        waits.append(seconds)

    monkeypatch.setattr(OpenAICompatLLM, "_sleep", staticmethod(record))
    responses = iter([
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(429, headers={"x-ratelimit-reset-tokens": "1m2.5s"}),
        httpx.Response(429, headers={"x-ratelimit-reset-tokens": "450ms"}),
    ])
    llm = _llm_with(lambda r: next(responses, None) or _ok("done"))
    llm.max_retries = 5
    assert asyncio.run(llm.complete("s", "u", purpose="t")).text == "done"
    # Server hints (the second capped at 60 s), each plus up to 1 s of jitter.
    assert 7 <= waits[0] < 8 and 60 <= waits[1] < 61 and 0.45 <= waits[2] < 1.45


def test_llm_drops_response_format_when_server_rejects_it():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(400) if "response_format" in body else _ok("{}")

    asyncio.run(_llm_with(handler).complete("s", "u", purpose="t", json_mode=True))
    assert "response_format" in bodies[0] and "response_format" not in bodies[1]


def test_llm_fails_fast_when_the_account_is_out_of_credits():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, text='{"error": {"type": "insufficient_quota"}}')

    with pytest.raises(LLMError, match="no credits"):
        asyncio.run(_llm_with(handler).complete("s", "u", purpose="t"))
    assert len(calls) == 1


def test_llm_gives_up_on_client_errors():
    with pytest.raises(LLMError, match="401"):
        asyncio.run(_llm_with(lambda r: httpx.Response(401, text="bad key")).complete("s", "u", purpose="t"))


def test_cli_run_show_and_list_against_example_corpus(tmp_path, monkeypatch, capsys):
    llm = ScriptedLLM()
    llm.aclose = lambda: asyncio.sleep(0)
    monkeypatch.setattr(OpenAICompatLLM, "from_env", classmethod(lambda cls: llm))
    db, out = str(tmp_path / "runs.db"), str(tmp_path / "reports")

    code = cli.main(["--db", db, "run", "Are heat pumps worth it?", "--corpus", str(EXAMPLE_CORPUS), "--out", out])
    printed = capsys.readouterr().out
    assert code == 0, printed
    run_id = printed.split("run ", 1)[1].split(":", 1)[0]
    report = (tmp_path / "reports" / f"{run_id}.md").read_text()
    assert "## Sources" in report and ".md#" in report

    assert cli.main(["--db", db, "show", run_id]) == 0
    assert "run_finished" in capsys.readouterr().out
    assert cli.main(["--db", db, "list"]) == 0
    assert run_id in capsys.readouterr().out

    corpus = ["--corpus", str(EXAMPLE_CORPUS), "--out", out]
    assert cli.main(["--db", db, "ask", run_id, "What about the second point?", *corpus]) == 0
    printed = capsys.readouterr().out
    assert "understood as: Standalone: What about the second point?" in printed
    second = printed.split("\nrun ", 1)[1].split(":", 1)[0]
    assert "## Answer" in (tmp_path / "reports" / f"{second}.md").read_text()
    assert cli.main(["--db", db, "ask", run_id, "And the first?", *corpus]) == 0  # continues the latest turn
    capsys.readouterr()
    assert cli.main(["--db", db, "thread", run_id]) == 0
    turns = capsys.readouterr().out.splitlines()
    assert len(turns) == 3 and run_id in turns[0] and second in turns[1] and "answer" in turns[2]


def test_env_prefers_deeptrace_names_and_falls_back_to_researchloop(monkeypatch):
    from deeptrace_agent.env import getenv

    monkeypatch.delenv("DEEPTRACE_MODEL", raising=False)
    monkeypatch.setenv("RESEARCHLOOP_MODEL", "old-name")
    assert getenv("MODEL") == "old-name"
    monkeypatch.setenv("DEEPTRACE_MODEL", "new-name")
    assert getenv("MODEL") == "new-name"
    assert getenv("NOT_SET_ANYWHERE", "default") == "default"
