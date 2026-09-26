"""A scripted OpenAI-compatible /chat/completions server for end-to-end checks.

It answers each prompt with the same deterministic handlers the unit tests use,
so the full deployment (HTTP LLM client, API, separate worker processes, Docker)
can be exercised without a real model or API key.

    python evals/mock_llm_server.py --port 9100
    DEEPTRACE_BASE_URL=http://127.0.0.1:9100/v1 DEEPTRACE_API_KEY=x deeptrace serve
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402

from tests import fakes  # noqa: E402

PURPOSES = [
    ("You are the planner", "plan"),
    ("You write search queries", "queries"),
    ("You summarize evidence", "finding"),
    ("You write the final report", "report"),
    ("You plan the structure of a research report", "outline"),
    ("You write one section of a research report", "section"),
    ("You write the concluding section", "synthesis"),
    ("You condense research evidence", "digest"),
    ("You review research reports", "critic"),
    ("You check whether research claims", "judge"),
]

app = FastAPI()
calls = {"count": 0}
DELAY = {"seconds": 0.0}


@app.post("/v1/chat/completions")
async def chat(request: Request) -> dict:
    body = await request.json()
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    purpose = next(p for marker, p in PURPOSES if marker in system)
    calls["count"] += 1
    await asyncio.sleep(DELAY["seconds"])
    content = json.dumps(fakes.DEFAULT_HANDLERS[purpose](system, user))
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"total_tokens": 100},
    }


@app.get("/calls")
async def call_count() -> dict:
    return calls


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait before each reply")
    args = parser.parse_args()
    DELAY["seconds"] = args.delay
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
