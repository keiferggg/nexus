"""Deterministic fake provider for offline development, demos and fault tests.

It is a *simulation*: missions that use it run in ``offline_fake`` mode and the UI labels them
as such. It never stands in for a real provider in a live mission. It establishes nothing about
real provider compatibility.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import CompletionRequest, CompletionResult, ModelInfo, ProviderError, ToolCall, Usage

PACKET_RE = re.compile(r"<context_packet>\s*(\{.*\})\s*</context_packet>", re.S)


def extract_packet(req: CompletionRequest) -> dict[str, Any]:
    for m in req.messages:
        if m.role == "user":
            mm = PACKET_RE.search(m.content)
            if mm:
                return json.loads(mm.group(1))
    return {}


def _tok(s: str) -> int:
    return max(1, math.ceil(len(s) / 4))


@dataclass
class Fault:
    """Inject a provider failure for matching calls. ``times`` failures, then pass through."""

    kind: str  # auth | rate_limit | timeout | server | malformed_output | missing_usage | hang
    purpose: str | None = None
    role: str | None = None
    times: int = 1
    hits: int = 0

    def matches(self, req: CompletionRequest) -> bool:
        md = req.metadata
        return (self.purpose is None or md.get("purpose") == self.purpose) and (
            self.role is None or md.get("role") == self.role)


ScriptFn = Callable[[CompletionRequest, dict[str, Any]], CompletionResult | str | dict]


@dataclass
class FakeProvider:
    script: ScriptFn
    faults: list[Fault] = field(default_factory=list)
    kind: str = "fake"
    sends_data_off_machine: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    latency_s: float = 0.0

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo("fake-deterministic-1", "Deterministic fake (offline)")]

    async def complete(self, req: CompletionRequest, *, timeout_s: float) -> CompletionResult:
        packet = extract_packet(req)
        self.calls.append({"purpose": req.metadata.get("purpose"), "role": req.metadata.get("role"),
                           "messages": len(req.messages)})
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        missing_usage = False
        for f in self.faults:
            if f.matches(req) and f.hits < f.times:
                f.hits += 1
                if f.kind == "hang":
                    await asyncio.sleep(timeout_s + 5)
                if f.kind == "timeout":
                    raise ProviderError("timeout", f"simulated timeout after {timeout_s}s")
                if f.kind == "auth":
                    raise ProviderError("auth", "HTTP 401: credentials rejected", status=401)
                if f.kind == "rate_limit":
                    raise ProviderError("rate_limit", "HTTP 429: simulated", status=429, retry_after=0.01)
                if f.kind == "server":
                    raise ProviderError("server", "HTTP 529: simulated overload", status=529)
                if f.kind == "malformed_output":
                    return CompletionResult("this is not json {", [], Usage(_tok(str(packet)), 5), "end_turn", req.model)
                if f.kind == "missing_usage":
                    missing_usage = True
        out = self.script(req, packet)
        if isinstance(out, CompletionResult):
            result = out
        elif isinstance(out, dict) and "__tool_calls__" in out:
            result = CompletionResult("", [ToolCall(c.get("id", f"call_{i}"), c["name"], c.get("arguments", {}))
                                           for i, c in enumerate(out["__tool_calls__"])], None, "tool_use", req.model)
        else:
            text = out if isinstance(out, str) else json.dumps(out)
            result = CompletionResult(text, [], None, "end_turn", req.model)
        if result.usage is None and not missing_usage:
            in_chars = len(req.system) + sum(len(m.content) for m in req.messages)
            result.usage = Usage(_tok("x" * in_chars), _tok(result.text) + 10 * len(result.tool_calls))
        if missing_usage:
            result.usage = None
        return result
