"""LLM client abstraction.

The runtime only depends on the small ``LLM`` protocol below, so any provider
with an OpenAI-compatible ``/chat/completions`` endpoint (OpenAI, DeepSeek,
Qwen/DashScope, vLLM, Ollama, ...) works through ``OpenAICompatLLM``, and tests
can swap in a scripted fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .env import getenv


class LLMError(RuntimeError):
    """The provider failed after all retries."""


class LLMFormatError(ValueError):
    """The model's reply could not be parsed into the expected JSON shape."""


@dataclass
class Completion:
    text: str
    tokens: int


class LLM(Protocol):
    async def complete(self, system: str, user: str, *, purpose: str, json_mode: bool = False) -> Completion:
        """Return the model's reply. ``purpose`` names the calling stage (plan, queries, ...)."""
        ...


def estimate_tokens(*texts: str) -> int:
    # Rough but provider-independent: ~4 characters per token for English,
    # and CJK text is closer to 1 token per character.
    total = 0
    for text in texts:
        cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
        total += cjk + (len(text) - cjk) // 4
    return max(total, 1)


class OpenAICompatLLM:
    RETRYABLE = {408, 409, 429, 500, 502, 503, 504}
    _sleep = staticmethod(asyncio.sleep)  # backoff; overridable in tests

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        *,
        temperature: float = 0.2,
        timeout: float = 90.0,
        max_retries: int = 6,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    @classmethod
    def from_env(cls) -> OpenAICompatLLM:
        api_key = getenv("API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise LLMError("set DEEPTRACE_API_KEY (or OPENAI_API_KEY)")
        return cls(
            model=getenv("MODEL", "gpt-4o-mini"),
            api_key=api_key,
            base_url=getenv("BASE_URL", "https://api.openai.com/v1"),
        )

    async def complete(self, system: str, user: str, *, purpose: str, json_mode: bool = False) -> Completion:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.post("/chat/completions", json=body)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise LLMError(f"{purpose}: network error: {exc}") from exc
            else:
                if resp.status_code == 400 and json_mode and "response_format" in body:
                    # Some OpenAI-compatible servers reject response_format; the
                    # prompts already demand JSON, so retry once without it.
                    body.pop("response_format")
                    continue
                if resp.status_code < 400:
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"] or ""
                    tokens = (data.get("usage") or {}).get("total_tokens") or estimate_tokens(system, user, text)
                    return Completion(text=text, tokens=int(tokens))
                if resp.status_code == 429 and "insufficient_quota" in resp.text:
                    # Out of credits: retrying cannot help, so fail fast instead of backing off.
                    raise LLMError(f"{purpose}: the provider account has no credits left (insufficient_quota)")
                if resp.status_code not in self.RETRYABLE or attempt == self.max_retries:
                    raise LLMError(f"{purpose}: HTTP {resp.status_code}: {resp.text[:300]}")
                hinted = retry_after(resp.headers)
                if hinted is not None:
                    # Rate limited: wait as long as the server asks (plus jitter), not our own guess.
                    await self._sleep(min(hinted, 60.0) + random.random())
                    continue
            await self._sleep(min(2**attempt, 20) + random.random())
        raise LLMError(f"{purpose}: retries exhausted")

    async def aclose(self) -> None:
        await self._client.aclose()


_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?$")


def retry_after(headers: httpx.Headers) -> float | None:
    """Seconds to wait before retrying, from Retry-After or OpenAI-style rate-limit reset headers."""
    if "retry-after-ms" in headers:
        try:
            return float(headers["retry-after-ms"]) / 1000
        except ValueError:
            pass
    if "retry-after" in headers:
        try:
            return float(headers["retry-after"])
        except ValueError:
            pass
    waits = []
    for name in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        match = _DURATION_RE.match(headers.get(name, "").strip())
        if match and any(match.groups()):
            h, m, sec, ms = (float(g) if g else 0.0 for g in match.groups())
            waits.append(h * 3600 + m * 60 + sec + ms / 1000)
    return max(waits) if waits else None


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a model reply.

    Tolerates markdown code fences and prose before/after the object, which
    models still emit occasionally even in JSON mode.
    """
    start = text.find("{")
    if start == -1:
        raise LLMFormatError(f"no JSON object in reply: {text[:200]!r}")
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise LLMFormatError(f"invalid JSON: {exc}") from exc
                if not isinstance(value, dict):
                    raise LLMFormatError("expected a JSON object")
                return value
    raise LLMFormatError("unterminated JSON object")
