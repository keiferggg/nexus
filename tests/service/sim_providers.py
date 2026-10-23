"""Wire-protocol simulators for Anthropic Messages, OpenAI Chat Completions and Ollama /api/chat.

They parse the exact JSON NEXUS sends, enforce the structural rules the real APIs enforce (and
answer HTTP 400 when violated), run a deterministic 'brain' on the reconstructed conversation,
and reply in each provider's response format with usage. This exercises NEXUS's live path
end to end (formatting, multi-turn tool use, usage-based cost, budgets); it does not test model
quality.
"""
import json
import math
import re

import httpx

from nexus.demo.script import demo_script
from nexus.providers.base import ChatMessage, CompletionRequest, ToolCall, ToolSpec


def _tok(s: str) -> int:
    return max(1, math.ceil(len(s) / 4))


def _meta(system: str, messages: list[ChatMessage]) -> dict:
    m = re.search(r"ROLE:\s*(\w+)", system or "")
    role = m.group(1) if m else None
    last_user = next((x.content for x in reversed(messages) if x.role == "user"), "")
    if "not the required JSON" in last_user:
        purpose = "execute:repair"
    elif role == "reviewer":
        purpose = "review"
    elif role == "guardian":
        purpose = "guard"
    elif "colleague's question" in (system or ""):
        purpose = "consult"
    elif "textual conflicts" in (system or ""):
        purpose = "resolve_conflict"
    elif role == "planner":
        purpose = "plan"
    else:
        purpose = "execute"
    return {"role": role, "purpose": purpose}


class ProviderSim(httpx.AsyncBaseTransport):
    def __init__(self, fmt: str, brain=demo_script, reasoning_tokens: int = 0):
        self.fmt = fmt
        self.reasoning_tokens = reasoning_tokens  # hidden reasoning that counts against max_tokens (DeepSeek-style)
        self.cache_hit_ratio = 0.0  # DeepSeek: share of prompt tokens reported as prompt_cache_hit_tokens
        self.balance = 10.0  # DeepSeek GET /user/balance (USD)
        self.brain = brain
        self.violations: list[str] = []
        self.requests = 0

    def _reject(self, why: str):
        self.violations.append(why)
        if self.fmt == "anthropic":
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": why}})
        return httpx.Response(400, json={"error": {"message": why, "type": "invalid_request_error"}})

    async def handle_async_request(self, request):
        path = request.url.path
        if request.method == "GET" and path == "/user/balance" and self.fmt == "deepseek":
            return httpx.Response(200, json={"is_available": True, "balance_infos": [
                {"currency": "USD", "total_balance": f"{self.balance:.2f}", "granted_balance": "0.00",
                 "topped_up_balance": f"{self.balance:.2f}"}]})
        if request.method == "GET":
            if self.fmt == "anthropic":
                return httpx.Response(200, json={"data": [{"id": "sim-claude", "display_name": "Sim"}], "has_more": False})
            if self.fmt == "openai":
                return httpx.Response(200, json={"data": [{"id": "sim-gpt"}]})
            if self.fmt == "deepseek":
                return httpx.Response(200, json={"object": "list", "data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]})
            return httpx.Response(200, json={"models": [{"name": "sim-llama"}]})
        self.requests += 1
        body = json.loads(request.content)
        try:
            req = getattr(self, f"_parse_{'openai' if self.fmt == 'deepseek' else self.fmt}")(body, request)
        except AssertionError as exc:
            return self._reject(str(exc))
        limit = body.get("max_tokens") or body.get("max_completion_tokens") or 0
        if self.reasoning_tokens and limit and limit <= self.reasoning_tokens:  # reasoning used the whole allowance
            return self._reply_openai("", [], _tok(json.dumps(body)), limit, body, finish="length")
        out = self.brain(req, _packet(req))
        calls = []
        if isinstance(out, dict) and "__tool_calls__" in out:
            calls = [ToolCall(f"call_{self.requests}_{i}", c["name"], c.get("arguments", {})) for i, c in enumerate(out["__tool_calls__"])]
            text = ""
        else:
            text = out if isinstance(out, str) else json.dumps(out)
        in_tok = _tok(json.dumps(body))
        out_tok = _tok(text) + 20 * len(calls)
        return getattr(self, f"_reply_{'openai' if self.fmt == 'deepseek' else self.fmt}")(text, calls, in_tok, out_tok, body)

    # ---- anthropic ----
    def _parse_anthropic(self, b, request):
        assert request.headers.get("x-api-key"), "missing x-api-key"
        assert request.headers.get("anthropic-version"), "missing anthropic-version"
        assert isinstance(b.get("max_tokens"), int) and b["max_tokens"] > 0, "max_tokens required"
        msgs, names, prev, pending = [], {}, None, set()
        for m in b["messages"]:
            assert m["role"] in ("user", "assistant"), f"bad role {m['role']}"
            assert m["role"] != prev, "roles must alternate"
            prev = m["role"]
            assert isinstance(m["content"], list) and m["content"], "content must be a non-empty list"
            if m["role"] == "user":
                results = [c for c in m["content"] if c["type"] == "tool_result"]
                assert {r["tool_use_id"] for r in results} >= pending, "tool_use without matching tool_result"
                pending = set()
                for c in m["content"]:
                    if c["type"] == "tool_result":
                        assert c["tool_use_id"] in names, "tool_result for unknown tool_use_id"
                        msgs.append(ChatMessage("tool", c["content"], tool_call_id=c["tool_use_id"], tool_name=names[c["tool_use_id"]]))
                    elif c["type"] == "text":
                        assert c["text"].strip(), "text block must be non-empty"
                        msgs.append(ChatMessage("user", c["text"]))
            else:
                text = "".join(c.get("text", "") for c in m["content"] if c["type"] == "text")
                tcs = [ToolCall(c["id"], c["name"], c["input"]) for c in m["content"] if c["type"] == "tool_use"]
                for t in tcs:
                    names[t.id] = t.name
                pending = {t.id for t in tcs}
                msgs.append(ChatMessage("assistant", text, tool_calls=tcs))
        assert b["messages"][0]["role"] == "user", "first message must be user"
        tools = [ToolSpec(t["name"], t.get("description", ""), t["input_schema"]) for t in b.get("tools", [])]
        for t in tools:
            assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t.name), f"invalid tool name {t.name}"
        return CompletionRequest(model=b["model"], system=b.get("system", ""), messages=msgs, tools=tools,
                                 max_output_tokens=b["max_tokens"], metadata=_meta(b.get("system", ""), msgs))

    def _reply_anthropic(self, text, calls, i, o, b):
        content = ([{"type": "text", "text": text}] if text else []) + \
                  [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in calls]
        return httpx.Response(200, json={"id": f"msg_{self.requests}", "type": "message", "role": "assistant", "model": b["model"],
                                         "content": content, "stop_reason": "tool_use" if calls else "end_turn",
                                         "usage": {"input_tokens": i, "output_tokens": o}})

    # ---- openai ----
    def _parse_openai(self, b, request):
        assert request.headers.get("authorization", "").startswith("Bearer "), "missing bearer"
        assert b["messages"][0]["role"] == "system", "expected system first"
        if self.fmt == "deepseek":
            assert "max_tokens" in b and "max_completion_tokens" not in b, "DeepSeek uses max_tokens"
            for m in b["messages"]:
                if m["role"] == "assistant" and m.get("tool_calls"):
                    assert m.get("reasoning_content"), "reasoning_content must be passed back in a tool-use turn"
                if m["role"] == "assistant":
                    assert m.get("content") or m.get("tool_calls"), "Invalid assistant message: content or tool_calls must be set"
        msgs, names, open_ids = [], {}, set()
        for m in b["messages"][1:]:
            if m["role"] == "tool":
                assert m["tool_call_id"] in open_ids, "tool message without preceding tool_calls"
                open_ids.discard(m["tool_call_id"])
                msgs.append(ChatMessage("tool", m["content"], tool_call_id=m["tool_call_id"], tool_name=names[m["tool_call_id"]]))
                continue
            assert not open_ids, "tool_calls must be followed by tool messages"
            if m["role"] == "assistant":
                tcs = [ToolCall(t["id"], t["function"]["name"], json.loads(t["function"]["arguments"]))
                       for t in m.get("tool_calls") or []]
                for t in tcs:
                    names[t.id] = t.name
                open_ids = {t.id for t in tcs}
                msgs.append(ChatMessage("assistant", m.get("content") or "", tool_calls=tcs))
            else:
                assert m["role"] == "user", f"bad role {m['role']}"
                msgs.append(ChatMessage("user", m["content"]))
        if b.get("response_format", {}).get("type") == "json_object":
            assert "json" in json.dumps(b["messages"]).lower(), "json_object mode requires the word JSON in messages"
        tools = [ToolSpec(t["function"]["name"], t["function"].get("description", ""), t["function"]["parameters"])
                 for t in b.get("tools", [])]
        return CompletionRequest(model=b["model"], system=b["messages"][0]["content"], messages=msgs, tools=tools,
                                 max_output_tokens=b.get("max_completion_tokens", 1024),
                                 metadata=_meta(b["messages"][0]["content"], msgs))

    def _reply_openai(self, text, calls, i, o, b, finish=None):
        msg = {"role": "assistant", "content": text or None}
        if self.fmt == "deepseek":
            msg["reasoning_content"] = f"thinking about request {self.requests}"
        if calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                 for c in calls]
        return httpx.Response(200, json={"id": f"chatcmpl-{self.requests}", "model": b["model"], "choices": [
            {"index": 0, "message": msg, "finish_reason": finish or ("tool_calls" if calls else "stop")}],
            "usage": {"prompt_tokens": i, "completion_tokens": o, "total_tokens": i + o,
                      **({"prompt_cache_hit_tokens": int(i * self.cache_hit_ratio),
                          "prompt_cache_miss_tokens": i - int(i * self.cache_hit_ratio)} if self.fmt == "deepseek" else {})}})

    # ---- ollama ----
    def _parse_ollama(self, b, request):
        assert b.get("stream") is False, "stream must be false"
        msgs = []
        for m in b["messages"][1:]:
            if m["role"] == "tool":
                assert m.get("tool_name"), "tool message needs tool_name"
                msgs.append(ChatMessage("tool", m["content"], tool_name=m["tool_name"]))
            elif m["role"] == "assistant":
                msgs.append(ChatMessage("assistant", m.get("content", ""), tool_calls=[
                    ToolCall(f"o{i}", t["function"]["name"], t["function"]["arguments"]) for i, t in enumerate(m.get("tool_calls") or [])]))
            else:
                msgs.append(ChatMessage("user", m["content"]))
        tools = [ToolSpec(t["function"]["name"], t["function"].get("description", ""), t["function"]["parameters"])
                 for t in b.get("tools", [])]
        return CompletionRequest(model=b["model"], system=b["messages"][0]["content"], messages=msgs, tools=tools,
                                 metadata=_meta(b["messages"][0]["content"], msgs))

    def _reply_ollama(self, text, calls, i, o, b):
        return httpx.Response(200, json={"model": b["model"], "done": True, "done_reason": "stop", "message": {
            "role": "assistant", "content": text, "tool_calls": [{"function": {"name": c.name, "arguments": c.arguments}} for c in calls]},
            "prompt_eval_count": i, "eval_count": o})


def _packet(req: CompletionRequest) -> dict:
    from nexus.providers.fake import extract_packet

    return extract_packet(req)
