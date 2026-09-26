"""LLM client HTTP behaviour and the CLI, without touching the network."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from researchloop import cli
from researchloop.llm import LLMError, OpenAICompatLLM

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


def test_llm_drops_response_format_when_server_rejects_it():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(400) if "response_format" in body else _ok("{}")

    asyncio.run(_llm_with(handler).complete("s", "u", purpose="t", json_mode=True))
    assert "response_format" in bodies[0] and "response_format" not in bodies[1]


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
