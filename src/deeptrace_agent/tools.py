"""Tool layer: one runner for every external call, with a write-ahead intent log.

Every tool declares whether it has side effects. The runner records an intent
("pending") before calling a tool and the result ("done") after it. On resume,
a pending record means the process died mid-call and the call may or may not
have happened:

* read-only tools are simply called again (a repeat is harmless);
* side-effecting tools that accept an idempotency key are called again with the
  same key, so the receiving service drops the duplicate;
* side-effecting tools without idempotency support are NOT retried: the run is
  held for a human to reconcile, because retrying could do the thing twice.
"""

from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser
from typing import Any, Protocol

import httpx

from .budget import BudgetMeter
from .search import SearchProvider, tokenize
from .store import RunStore


class ToolOutcomeUnknown(RuntimeError):
    """A side-effecting call may or may not have happened; a human must reconcile it."""

    def __init__(self, tool: str, key: str) -> None:
        super().__init__(f"{tool} call {key[:12]} was interrupted and cannot be safely retried")
        self.tool = tool
        self.key = key


class Tool(Protocol):
    name: str
    side_effects: bool  # False: read-only, safe to repeat
    idempotent: bool  # True: the receiver deduplicates repeated calls with the same idempotency key

    async def __call__(self, args: dict[str, Any], *, idempotency_key: str) -> Any: ...


class ToolRunner:
    def __init__(self, store: RunStore, meter: BudgetMeter, run_id: str) -> None:
        self.store = store
        self.meter = meter
        self.run_id = run_id

    async def call(self, tool: Tool, args: dict[str, Any]) -> Any:
        key = RunStore.tool_key(self.run_id, tool.name, args)
        record = self.store.tool_record(key)
        if record and record["status"] == "done":
            self.meter.metrics.record_tool(tool.name, cached=True)
            return record["result"]
        if record and record["status"] == "pending":
            if tool.side_effects and not tool.idempotent:
                self.store.log(self.run_id, "tool_outcome_unknown", tool=tool.name, key=key)
                raise ToolOutcomeUnknown(tool.name, key)
            self.store.log(self.run_id, "tool_retry_after_crash", tool=tool.name,
                           read_only=not tool.side_effects, key=key[:12])

        self.meter.charge_tool_call()
        self.store.begin_tool_call(key, self.run_id, tool.name, args)  # intent, durable before the call
        started = time.monotonic()
        try:
            result = await tool(args, idempotency_key=key)
        except Exception:
            self.meter.metrics.record_tool(tool.name, cached=False, seconds=time.monotonic() - started, error=True)
            if not tool.side_effects:
                self.store.discard_tool_call(key)  # a failed read leaves nothing to reconcile
            raise
        self.store.finish_tool_call(key, result)
        self.meter.metrics.record_tool(tool.name, cached=False, seconds=time.monotonic() - started)
        return result


# -- tools ---------------------------------------------------------------------


class SearchTool:
    side_effects = False
    idempotent = True

    def __init__(self, provider: SearchProvider) -> None:
        self.provider = provider
        self.name = provider.name

    async def __call__(self, args: dict[str, Any], *, idempotency_key: str) -> list[dict[str, str]]:
        hits = await self.provider.search(args["query"], args["k"])
        return [{"url": h.url, "title": h.title, "content": h.content} for h in hits]


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"}
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> tuple[str, str]:
    """(title, readable text) of an HTML page, dropping scripts, navigation and boilerplate blocks."""
    parser = _TextExtractor()
    parser.feed(markup)
    text = re.sub(r"[ \t\r\f\v]+", " ", html.unescape("".join(parser.parts)))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text)]
    return " ".join(parser.title.split()), "\n\n".join(p for p in paragraphs if len(p) > 1)


def best_passages(text: str, query: str, *, chunk_chars: int = 800, top: int = 2) -> str:
    """The ``top`` chunks of ``text`` sharing the most terms with ``query``, in document order."""
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        if buf and len(buf) + len(p) > chunk_chars:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    terms = set(tokenize(query))
    scored = sorted(range(len(chunks)), key=lambda i: -len(terms & set(tokenize(chunks[i]))))[:top]
    return "\n\n".join(chunks[i][:chunk_chars] for i in sorted(scored))


class FetchPageTool:
    """Download a web page and return its readable text."""

    name = "fetch"
    side_effects = False
    idempotent = True

    def __init__(self, client: httpx.AsyncClient | None = None, *, max_bytes: int = 2_000_000,
                 max_chars: int = 60_000) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=15.0, follow_redirects=True, headers={"User-Agent": "deeptrace-agent/0.3 (+research agent)"}
        )
        self.max_bytes = max_bytes
        self.max_chars = max_chars

    async def __call__(self, args: dict[str, Any], *, idempotency_key: str) -> dict[str, str]:
        url = args["url"]
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"not a web URL: {url}")
        resp = await self._client.get(url)
        resp.raise_for_status()
        kind = resp.headers.get("content-type", "")
        body = resp.content[: self.max_bytes].decode(resp.encoding or "utf-8", errors="replace")
        if "html" in kind or body.lstrip().startswith("<"):
            title, text = html_to_text(body)
        elif kind.startswith("text/"):
            title, text = "", body
        else:
            raise ValueError(f"unsupported content type {kind!r}")
        return {"url": str(resp.url), "title": title, "text": text[: self.max_chars]}

    async def aclose(self) -> None:
        await self._client.aclose()


class WebhookTool:
    """POST a finished report to a URL, with an Idempotency-Key header.

    The key is stable per run and payload, so a delivery retried after a crash
    carries the same key and a receiver that honors it processes it once.
    """

    name = "webhook"
    side_effects = True
    idempotent = True

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def __call__(self, args: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        resp = await self._client.post(args["url"], json=args["payload"],
                                       headers={"Idempotency-Key": idempotency_key})
        resp.raise_for_status()
        return {"status_code": resp.status_code}

    async def aclose(self) -> None:
        await self._client.aclose()
