"""Direct HTTP adapters for Anthropic Messages, OpenAI Chat Completions and Ollama /api/chat.

Keys are passed in-process from the credential store and only placed in request headers.
They are never logged, never placed in URLs, and never returned in errors.
These adapters are exercised against recorded-shape fixtures via httpx.MockTransport in tests;
live compatibility requires a separately reported smoke test with real credentials.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from .base import (
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    ModelInfo,
    ProviderError,
    ToolCall,
    Usage,
    classify_http,
)


class _HttpAdapter:
    kind = "base"
    sends_data_off_machine = True

    def __init__(self, base_url: str, api_key: str | None, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._transport = transport

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
                                 transport=self._transport, follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}

    async def _request(self, method: str, path: str, *, json_body: Any = None, timeout_s: float = 30) -> Any:
        try:
            async with self._client(timeout_s) as client:
                resp = await client.request(method, f"{self.base_url}{path}", json=json_body, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"no response within {timeout_s}s ({type(exc).__name__})") from None
        except httpx.ConnectError as exc:
            raise ProviderError("network", f"connection failed: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            raise ProviderError("network", f"transport error: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise classify_http(resp.status_code, _safe_text(resp), {k.lower(): v for k, v in resp.headers.items()})
        try:
            return resp.json()
        except ValueError:
            raise ProviderError("malformed_output", "provider returned non-JSON body") from None

    def __repr__(self) -> str:  # never include the key
        return f"<{type(self).__name__} {self.base_url}>"


def _safe_text(resp: httpx.Response) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""


class AnthropicAdapter(_HttpAdapter):
    kind = "anthropic"
    API_VERSION = "2023-06-01"

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com", **kw):
        super().__init__(base_url, api_key, **kw)

    def _headers(self) -> dict[str, str]:
        return {"content-type": "application/json", "x-api-key": self._api_key or "",
                "anthropic-version": self.API_VERSION}

    async def list_models(self) -> list[ModelInfo]:
        out, after = [], None
        for _ in range(20):
            q = "?limit=100" + (f"&after_id={after}" if after else "")
            data = await self._request("GET", f"/v1/models{q}")
            out += [ModelInfo(m["id"], m.get("display_name")) for m in data.get("data", [])]
            if not data.get("has_more"):
                break
            after = data.get("last_id")
        return out

    @staticmethod
    def to_payload(req: CompletionRequest) -> dict[str, Any]:
        msgs: list[dict[str, Any]] = []
        for m in req.messages:
            if m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
                    msgs[-1]["content"].append(block)
                else:
                    msgs.append({"role": "user", "content": [block]})
            elif m.role == "assistant":
                content: list[dict[str, Any]] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                content += [{"type": "tool_use", "id": t.id, "name": t.name, "input": t.arguments} for t in m.tool_calls]
                if not content:  # the Messages API rejects empty assistant turns
                    content = [{"type": "text", "text": "(no output)"}]
                msgs.append({"role": "assistant", "content": content})
            else:
                block = {"type": "text", "text": m.content or "(empty)"}
                if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
                    msgs[-1]["content"].append(block)  # roles must alternate
                else:
                    msgs.append({"role": "user", "content": [block]})
        body: dict[str, Any] = {"model": req.model, "max_tokens": req.max_output_tokens, "system": req.system,
                                "messages": msgs}
        if req.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                             for t in req.tools]
            if req.tool_choice == "none":
                body["tool_choice"] = {"type": "none"}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        return body

    @staticmethod
    def parse(data: dict[str, Any]) -> CompletionResult:
        if not isinstance(data.get("content"), list):
            raise ProviderError("malformed_output", "anthropic response missing content list")
        text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        calls = [ToolCall(b.get("id") or uuid.uuid4().hex, b.get("name", ""), b.get("input") or {})
                 for b in data["content"] if b.get("type") == "tool_use"]
        u = data.get("usage")
        usage = None
        if isinstance(u, dict) and "input_tokens" in u and "output_tokens" in u:
            usage = Usage(int(u["input_tokens"]) + int(u.get("cache_creation_input_tokens") or 0)
                          + int(u.get("cache_read_input_tokens") or 0), int(u["output_tokens"]),
                          int(u.get("cache_read_input_tokens") or 0))
        return CompletionResult(text, calls, usage, data.get("stop_reason"), data.get("model", ""), data.get("id"))

    async def complete(self, req: CompletionRequest, *, timeout_s: float) -> CompletionResult:
        data = await self._request("POST", "/v1/messages", json_body=self.to_payload(req), timeout_s=timeout_s)
        return self.parse(data)


class OpenAIAdapter(_HttpAdapter):
    kind = "openai"

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com", **kw):
        super().__init__(base_url, api_key, **kw)

    def _headers(self) -> dict[str, str]:
        return {"content-type": "application/json", "authorization": f"Bearer {self._api_key or ''}"}

    async def list_models(self) -> list[ModelInfo]:
        data = await self._request("GET", "/v1/models")
        return [ModelInfo(m["id"]) for m in data.get("data", [])]

    @staticmethod
    def to_payload(req: CompletionRequest, *, token_field: str = "max_completion_tokens") -> dict[str, Any]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": req.system}]
        for m in req.messages:
            if m.role == "tool":
                msgs.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            elif m.role == "assistant":
                if not m.content and not m.tool_calls:
                    continue  # DeepSeek and others reject an assistant turn with neither content nor tool_calls
                d: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    d["tool_calls"] = [{"id": t.id, "type": "function",
                                        "function": {"name": t.name, "arguments": json.dumps(t.arguments)}}
                                       for t in m.tool_calls]
                    if m.reasoning:  # reasoning models (e.g. DeepSeek) require it back within a tool-use turn
                        d["reasoning_content"] = m.reasoning
                msgs.append(d)
            else:
                msgs.append({"role": "user", "content": m.content})
        body: dict[str, Any] = {"model": req.model, "messages": msgs, token_field: req.max_output_tokens}
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.input_schema}} for t in req.tools]
            if req.tool_choice == "none":
                body["tool_choice"] = "none"
        if req.response_json and not req.tools:
            body["response_format"] = {"type": "json_object"}
            # OpenAI rejects json_object mode unless the word "JSON" appears in the messages.
            if "json" not in json.dumps(msgs).lower():
                msgs[0]["content"] = msgs[0]["content"] + "\n\nRespond with a single JSON object."
        if req.temperature is not None:
            body["temperature"] = req.temperature
        return body

    @staticmethod
    def parse(data: dict[str, Any]) -> CompletionResult:
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("malformed_output", "openai response missing choices[0].message") from None
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                raise ProviderError("malformed_output", f"tool call arguments are not JSON for {fn.get('name')}") from None
            calls.append(ToolCall(tc.get("id") or uuid.uuid4().hex, fn.get("name", ""), args))
        u = data.get("usage")
        usage = None
        if isinstance(u, dict) and "prompt_tokens" in u and "completion_tokens" in u:
            cached = u.get("prompt_cache_hit_tokens")
            if cached is None:
                cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
            usage = Usage(int(u["prompt_tokens"]), int(u["completion_tokens"]), int(cached or 0))
        return CompletionResult(msg.get("content") or "", calls, usage, choice.get("finish_reason"),
                                data.get("model", ""), data.get("id"), reasoning=msg.get("reasoning_content") or None)

    @property
    def token_field(self) -> str:
        """api.openai.com uses max_completion_tokens; OpenAI-compatible servers (DeepSeek, vLLM, LM Studio...)
        use the classic max_tokens."""
        host = (httpx.URL(self.base_url).host or "").lower()
        return "max_completion_tokens" if host.endswith("openai.com") else "max_tokens"

    async def complete(self, req: CompletionRequest, *, timeout_s: float) -> CompletionResult:
        data = await self._request("POST", "/v1/chat/completions",
                                   json_body=self.to_payload(req, token_field=self.token_field), timeout_s=timeout_s)
        return self.parse(data)


class OllamaAdapter(_HttpAdapter):
    kind = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", **kw):
        super().__init__(base_url, None, **kw)
        host = httpx.URL(self.base_url).host or ""
        self.sends_data_off_machine = host not in ("127.0.0.1", "localhost", "::1")

    async def list_models(self) -> list[ModelInfo]:
        data = await self._request("GET", "/api/tags")
        return [ModelInfo(m.get("name") or m.get("model")) for m in data.get("models", [])]

    @staticmethod
    def to_payload(req: CompletionRequest) -> dict[str, Any]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": req.system}]
        for m in req.messages:
            if m.role == "tool":
                msgs.append({"role": "tool", "content": m.content, "tool_name": m.tool_name})
            elif m.role == "assistant":
                if not m.content and not m.tool_calls:
                    continue
                d: dict[str, Any] = {"role": "assistant", "content": m.content}
                if m.tool_calls:
                    d["tool_calls"] = [{"function": {"name": t.name, "arguments": t.arguments}} for t in m.tool_calls]
                msgs.append(d)
            else:
                msgs.append({"role": "user", "content": m.content})
        body: dict[str, Any] = {"model": req.model, "messages": msgs, "stream": False,
                                "options": {"num_predict": req.max_output_tokens}}
        if req.tools and req.tool_choice != "none":
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.input_schema}} for t in req.tools]
        elif req.response_json:
            body["format"] = "json"
        if req.temperature is not None:
            body["options"]["temperature"] = req.temperature
        return body

    @staticmethod
    def parse(data: dict[str, Any]) -> CompletionResult:
        msg = data.get("message")
        if not isinstance(msg, dict):
            raise ProviderError("malformed_output", "ollama response missing message")
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    raise ProviderError("malformed_output", "ollama tool arguments not JSON") from None
            calls.append(ToolCall(uuid.uuid4().hex, fn.get("name", ""), args))
        usage = None
        if "prompt_eval_count" in data and "eval_count" in data:
            usage = Usage(int(data["prompt_eval_count"]), int(data["eval_count"]))
        return CompletionResult(msg.get("content") or "", calls, usage, data.get("done_reason"), data.get("model", ""))

    async def complete(self, req: CompletionRequest, *, timeout_s: float) -> CompletionResult:
        data = await self._request("POST", "/api/chat", json_body=self.to_payload(req), timeout_s=timeout_s)
        return self.parse(data)


def history_to_messages(history: list[dict[str, Any]]) -> list[ChatMessage]:
    out = []
    for h in history:
        out.append(ChatMessage(role=h["role"], content=h.get("content", ""),
                               tool_calls=[ToolCall(t["id"], t["name"], t.get("arguments", {})) for t in h.get("tool_calls", [])],
                               tool_call_id=h.get("tool_call_id"), tool_name=h.get("tool_name")))
    return out
