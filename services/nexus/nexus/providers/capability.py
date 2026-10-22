"""Provider capability check: a few tiny real calls that verify what NEXUS needs from a model
before it runs a mission: structured JSON output, a full tool-call round trip, and usage reporting.
Owner-initiated only; each call is capped at a small number of output tokens."""

from __future__ import annotations

import json
import time
from typing import Any

from ..persistence.db import new_id
from .base import ChatMessage, CompletionRequest, ProviderError, ToolSpec, parse_json_output

SECRET = 7341


async def check_model(gateway, provider_id: str, model: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    usage_seen, cost = [], 0.0
    sys_json = "Reply with ONLY a JSON object, no prose."

    async def call(req: CompletionRequest, purpose: str):
        t0 = time.monotonic()
        out = await gateway.call(req, operation_id=new_id("capcheck"), provider_id=provider_id, purpose=purpose,
                                 allow_unpriced=True, timeout_s=60)
        usage_seen.append(out.result.usage is not None)
        return out, time.monotonic() - t0

    # 1. structured output
    try:
        out, dt = await call(CompletionRequest(model=model, system=sys_json, max_output_tokens=64, response_json=True,
                                               messages=[ChatMessage("user", 'What is 2+2? Answer as {"answer": <number>}.')]),
                             "capability:json")
        data = parse_json_output(out.result.text)
        ok = int(data.get("answer", -1)) == 4
        results.append({"check": "structured_output", "passed": ok, "detail": f"got {out.result.text[:80]!r}", "seconds": round(dt, 2)})
        cost += out.actual_usd or 0
    except (ProviderError, ValueError, TypeError) as exc:
        results.append({"check": "structured_output", "passed": False, "detail": str(exc)[:300]})
        if isinstance(exc, ProviderError) and exc.kind in ("auth", "not_configured", "bad_request"):
            return {"model": model, "results": results, "passed": False, "usage_reported": any(usage_seen)}

    # 2. tool call round trip
    tool = ToolSpec("get_secret_number", "Returns the secret number the user is asking about.",
                    {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]})
    msgs = [ChatMessage("user", "Use the get_secret_number tool with label 'alpha', then reply ONLY with "
                                '{"secret": <the number>}.')]
    try:
        out, dt = await call(CompletionRequest(model=model, system=sys_json, max_output_tokens=200, tools=[tool], messages=msgs),
                             "capability:tool_call")
        calls = out.result.tool_calls
        cost += out.actual_usd or 0
        if not calls:
            results.append({"check": "tool_call", "passed": False, "detail": f"model answered without calling the tool: "
                                                                             f"{out.result.text[:120]!r}"})
        else:
            tc = calls[0]
            results.append({"check": "tool_call", "passed": tc.name == "get_secret_number",
                            "detail": f"called {tc.name}({json.dumps(tc.arguments)[:80]})", "seconds": round(dt, 2)})
            msgs += [ChatMessage("assistant", out.result.text, tool_calls=calls, reasoning=out.result.reasoning)]
            msgs += [ChatMessage("tool", json.dumps({"secret": SECRET}), tool_call_id=c.id, tool_name=c.name) for c in calls]
            out2, dt2 = await call(CompletionRequest(model=model, system=sys_json, max_output_tokens=120, tools=[tool],
                                                     messages=msgs), "capability:tool_result")
            cost += out2.actual_usd or 0
            ok = str(SECRET) in out2.result.text
            results.append({"check": "tool_result_round_trip", "passed": ok,
                            "detail": f"final reply {out2.result.text[:100]!r}", "seconds": round(dt2, 2)})
    except ProviderError as exc:
        results.append({"check": "tool_call", "passed": False, "detail": str(exc)[:300]})

    results.append({"check": "usage_reported", "passed": bool(usage_seen) and all(usage_seen),
                    "detail": "provider reported token usage on every call" if usage_seen and all(usage_seen) else
                    "usage missing on some calls: spend for this model stays 'unresolved' (conservative)"})
    return {"model": model, "results": results, "passed": all(r["passed"] for r in results),
            "calls": len(usage_seen), "estimated_cost_usd": round(cost, 6),
            "note": "Costs use your configured price for this model; if none is set, cost is unknown."}
