"""Provider-neutral completion types and error taxonomy.

Error kinds and how the gateway treats billing for them:
  not_configured  - no provider/key/price configured; nothing sent           -> reservation released
  auth            - 401/403; request rejected                                -> released, BLOCKED
  rate_limit      - 429; request rejected                                    -> released, retryable
  bad_request     - 400/404/422; request rejected                           -> released, BLOCKED
  network         - connection failed before a response; may not have sent  -> unresolved (conservative)
  timeout         - no response in time; provider may still bill            -> unresolved
  server          - 5xx / overloaded                                         -> unresolved, retryable
  malformed_output- response received but violates the requested structure   -> billed (usage reconciled)
  canceled        - local cancellation; provider may still bill             -> unresolved
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

RETRYABLE = {"rate_limit", "server", "network", "timeout"}
REJECTED_UNBILLED = {"not_configured", "auth", "rate_limit", "bad_request"}


class ProviderError(Exception):
    def __init__(self, kind: str, detail: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: str  # user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    reasoning: str | None = None  # provider reasoning text that must be echoed back (e.g. DeepSeek tool turns)


@dataclass
class CompletionRequest:
    model: str
    system: str
    messages: list[ChatMessage]
    max_output_tokens: int = 1024
    tools: list[ToolSpec] = field(default_factory=list)
    response_json: bool = False
    temperature: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # purpose/role; never sent to the provider


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class CompletionResult:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage | None
    stop_reason: str | None
    model: str
    raw_id: str | None = None
    reasoning: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [{"id": t.id, "name": t.name, "arguments": t.arguments} for t in self.tool_calls],
            "usage": None if self.usage is None else {"input": self.usage.input_tokens, "output": self.usage.output_tokens},
            "stop_reason": self.stop_reason,
            "model": self.model,
            "raw_id": self.raw_id,
            "reasoning": self.reasoning,
        }

    @classmethod
    def from_record(cls, d: dict[str, Any]) -> "CompletionResult":
        u = d.get("usage")
        return cls(
            text=d.get("text", ""),
            tool_calls=[ToolCall(t["id"], t["name"], t.get("arguments", {})) for t in d.get("tool_calls", [])],
            usage=None if u is None else Usage(u["input"], u["output"]),
            stop_reason=d.get("stop_reason"),
            model=d.get("model", ""),
            raw_id=d.get("raw_id"),
            reasoning=d.get("reasoning"),
        )


@dataclass
class ModelInfo:
    id: str
    display_name: str | None = None


class ProviderAdapter(Protocol):
    kind: str
    sends_data_off_machine: bool

    async def list_models(self) -> list[ModelInfo]: ...

    async def complete(self, req: CompletionRequest, *, timeout_s: float) -> CompletionResult: ...


def classify_http(status: int, body: str, headers: dict[str, str] | None = None) -> ProviderError:
    snippet = body[:300]
    retry_after = None
    if headers:
        ra = headers.get("retry-after")
        try:
            retry_after = float(ra) if ra else None
        except ValueError:
            retry_after = None
    if status in (401, 403):
        return ProviderError("auth", f"HTTP {status}: credentials rejected", status=status)
    if status == 429:
        return ProviderError("rate_limit", f"HTTP 429: {snippet}", status=status, retry_after=retry_after)
    if status in (400, 404, 413, 422):
        return ProviderError("bad_request", f"HTTP {status}: {snippet}", status=status)
    if status >= 500:
        return ProviderError("server", f"HTTP {status}: {snippet}", status=status, retry_after=retry_after)
    return ProviderError("bad_request", f"HTTP {status}: {snippet}", status=status)


def parse_json_output(text: str) -> Any:
    """Extract a JSON object from model text; raise ProviderError('malformed_output') if absent."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(t[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ProviderError("malformed_output", f"expected a JSON object, got: {text[:200]!r}")


def estimate_input_tokens_upper(req: CompletionRequest) -> int:
    """Conservative upper estimate (about 1 token per 3 characters plus overhead)."""
    chars = len(req.system) + sum(len(m.content) + sum(len(json.dumps(t.arguments)) for t in m.tool_calls)
                                  for m in req.messages)
    chars += sum(len(t.name) + len(t.description) + len(json.dumps(t.input_schema)) for t in req.tools)
    return chars // 3 + 16 * (len(req.messages) + 1) + 64
