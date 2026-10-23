"""LIVE-mode missions over the real wire protocols (simulated provider endpoints)."""
import json

import pytest

from helpers import new_mission, tasks
from nexus.appctx import build_context
from nexus.bootstrap import create_demo_project, ensure_fake_provider
from nexus.domain.budget import summary
from nexus.orchestration.supervisor import Supervisor
from nexus.persistence.db import dumps
from nexus.providers.capability import check_model
from nexus.providers.http_adapters import AnthropicAdapter, OllamaAdapter, OpenAIAdapter
from nexus.secrets import MemorySecretStore
from nexus.tools.sandbox import UnavailableSandbox
from sim_providers import ProviderSim

SETUPS = {
    "anthropic": (lambda t: AnthropicAdapter("sk-ant-sim", transport=t), "sim-claude", (3.0, 15.0)),
    "openai": (lambda t: OpenAIAdapter("sk-sim", transport=t), "sim-gpt", (2.5, 10.0)),
    "ollama": (lambda t: OllamaAdapter("http://127.0.0.1:11434", transport=t), "sim-llama", None),
    "deepseek": (lambda t: OpenAIAdapter("sk-ds", base_url="https://api.deepseek.com", transport=t), "deepseek-chat", (0.28, 0.42)),
}


@pytest.fixture
async def live(tmp_path):
    c = build_context(tmp_path / "d", secrets=MemorySecretStore(), sandbox=UnavailableSandbox("test"))
    c.heartbeat_s, c.lease_ttl_ms = 0.2, 5000
    ensure_fake_provider(c)  # present but must NOT be used by live missions
    sup = Supervisor(c)
    await sup.start()
    sup._loop_task.cancel()
    yield c, sup
    await sup.stop()
    c.close()


def configure(c, fmt):
    make, model, price = SETUPS[fmt]
    sim = ProviderSim(fmt)
    pid = c.registry.add(kind="openai" if fmt == "deepseek" else fmt, label=f"{fmt} (sim)",
                         base_url="https://api.deepseek.com" if fmt == "deepseek" else None,
                         api_key=None if fmt == "ollama" else "sk-sim-key-123456")
    c.registry.register_override(pid, make(sim))
    if price:
        c.registry.set_price(pid, model, *price, source="test price")
    with c.db.tx() as cur:
        cur.execute("INSERT OR REPLACE INTO settings VALUES ('default_model_policy', ?, '')",
                    (dumps({"provider_id": pid, "model": model, "max_output_tokens": 1024}),))
    return pid, model, sim


@pytest.mark.parametrize("fmt", ["anthropic", "openai", "ollama", "deepseek"])
async def test_live_mission_over_provider_protocol(live, fmt):
    c, sup = live
    pid, model, sim = configure(c, fmt)
    proj = create_demo_project(c, "research")
    mid = new_mission(c, proj, mode="live", cap=1.0)
    status = await sup.wait_idle(mid, timeout=90)
    ts = tasks(c, mid)
    assert status == "completed", ({k: (v["status"], v["status_reason"]) for k, v in ts.items()}, sim.violations[:3])
    assert sim.violations == [], sim.violations
    calls = c.db.query("SELECT * FROM model_calls WHERE mission_id = ?", (mid,))
    assert calls and {x["provider_kind"] for x in calls} == {"openai" if fmt == "deepseek" else fmt}, "never the fake"
    assert all(x["status"] == "succeeded" and x["usage_reported"] for x in calls)
    assert any(x["purpose"] == "consult" for x in calls) and any(x["purpose"] == "review" for x in calls)
    with c.db.tx() as cur:
        s = summary(cur, mid)
    if SETUPS[fmt][2]:
        assert 0 < s["actual_usd"] <= 1.0 and s["reserved_usd"] == 0 and s["unresolved_usd"] == 0, s
    else:
        assert s["actual_usd"] == 0, "local Ollama has no API charge by default"


@pytest.mark.parametrize("fmt", ["anthropic", "openai", "ollama", "deepseek"])
async def test_capability_check(live, fmt):
    c, _ = live
    pid, model, sim = configure(c, fmt)

    def brain(req, packet):
        tool_msgs = [m for m in req.messages if m.role == "tool"]
        if req.tools and not tool_msgs:
            return {"__tool_calls__": [{"name": "get_secret_number", "arguments": {"label": "alpha"}}]}
        if tool_msgs:
            return {"secret": json.loads(tool_msgs[0].content)["secret"]}
        return {"answer": 4}

    sim.brain = brain
    res = await check_model(c.gateway, pid, model)
    assert res["passed"], res
    assert [r["check"] for r in res["results"]] == ["structured_output", "tool_call", "tool_result_round_trip", "usage_reported"]
    assert sim.violations == []


async def test_capability_check_reports_a_model_that_ignores_tools(live):
    c, _ = live
    pid, model, sim = configure(c, "openai")
    sim.brain = lambda req, packet: {"answer": 4}
    res = await check_model(c.gateway, pid, model)
    assert not res["passed"]
    tool = next(r for r in res["results"] if r["check"] == "tool_call")
    assert not tool["passed"] and "without calling the tool" in tool["detail"]


async def test_live_readiness_requires_prices(live):
    from nexus.domain.missions import create_project
    from nexus.providers.readiness import live_readiness

    c, _ = live
    with c.db.tx() as cur:
        proj = create_project(cur, name="r", workspace_root=str(c.data_dir))
    r = live_readiness(c.db, c.registry, proj)
    assert not r["ready"] and {p["kind"] for p in r["problems"]} == {"no_model"}
    pid = c.registry.add(kind="openai", label="DeepSeek", base_url="https://api.deepseek.com", api_key="sk-x-123456")
    with c.db.tx() as cur:
        cur.execute("UPDATE providers SET status = 'ok' WHERE id = ?", (pid,))
        cur.execute("INSERT OR REPLACE INTO settings VALUES ('default_model_policy', ?, '')",
                    (dumps({"provider_id": pid, "model": "deepseek-chat"}),))
    r = live_readiness(c.db, c.registry, proj)
    assert not r["ready"] and r["problems"][0]["kind"] == "no_price" and r["problems"][0]["model"] == "deepseek-chat"
    assert len(r["problems"]) == 1, "one model shared by all agents is reported once"
    c.registry.set_price(pid, "deepseek-chat", 0.28, 0.42, "test")
    r = live_readiness(c.db, c.registry, proj)
    assert r["ready"] and r["warnings"][0]["kind"] == "capability_unchecked"


async def test_reasoning_model_that_exhausts_output_limit_is_retried_larger(live):
    """Seen live with deepseek-v4-pro: hidden reasoning used the whole 2048-token allowance, the reply was empty,
    and the JSON-repair turn then sent an empty assistant message, which DeepSeek rejects (HTTP 400)."""
    c, sup = live
    pid, model, _ = configure(c, "deepseek")
    sim = ProviderSim("deepseek", reasoning_tokens=9000)  # any limit <= 9000 tokens comes back empty, finish=length
    c.registry.register_override(pid, SETUPS["deepseek"][0](sim))
    proj = create_demo_project(c, "research")
    mid = new_mission(c, proj, mode="live", cap=1.0)
    status = await sup.wait_idle(mid, timeout=90)
    ts = tasks(c, mid)
    assert status == "completed", ({k: (v["status"], v["status_reason"]) for k, v in ts.items()}, sim.violations[:3])
    assert sim.violations == []
    calls = c.db.query("SELECT operation_id, status FROM model_calls WHERE mission_id = ?", (mid,))
    assert any(":more" in x["operation_id"] for x in calls), "truncated calls must be retried with a larger limit"
    assert all(x["status"] == "succeeded" for x in calls)


async def test_truncation_retry_is_learned_so_later_missions_do_not_repeat_it(live):
    """Seen live: the planner exceeded 8192 output tokens on every mission, so each mission paid for one cut-off
    plan call (about $0.03) before the larger retry. After one retry, calls for that purpose start larger."""
    c, sup = live
    pid, model, _ = configure(c, "deepseek")
    sim = ProviderSim("deepseek", reasoning_tokens=9000)
    c.registry.register_override(pid, SETUPS["deepseek"][0](sim))
    proj = create_demo_project(c, "research")
    first = new_mission(c, proj, mode="live", cap=1.0)
    assert await sup.wait_idle(first, timeout=90) == "completed"
    q = "SELECT purpose, operation_id FROM model_calls WHERE mission_id = ?"
    retried = {r["purpose"] for r in c.db.query(q, (first,)) if ":more" in r["operation_id"]}
    assert "plan" in retried
    sent_before = sim.requests
    second = new_mission(c, proj, mode="live", cap=1.0)
    assert await sup.wait_idle(second, timeout=90) == "completed"
    again = [r for r in c.db.query(q, (second,)) if ":more" in r["operation_id"] and r["purpose"] in retried]
    assert again == [], again
    plan_calls = [r for r in c.db.query(q, (second,)) if r["purpose"] == "plan"]
    assert plan_calls and sim.requests > sent_before
    assert c.gateway.learned_output_limit(pid, model, "plan") == 16384


def test_openai_payload_drops_empty_assistant_turns():
    from nexus.providers.base import ChatMessage, CompletionRequest
    req = CompletionRequest(model="m", system="s", messages=[ChatMessage("user", "hi"), ChatMessage("assistant", ""),
                                                             ChatMessage("user", "again")], response_json=True)
    body = OpenAIAdapter.to_payload(req, token_field="max_tokens")
    assert [m["role"] for m in body["messages"]] == ["system", "user", "user"]


async def test_work_written_by_the_only_reviewer_is_reviewed_by_another_agent(live):
    """Seen live: the planner added a 'check' task, which went to the reviewer; verifying it then had no independent
    reviewer and blocked the mission. Another non-author, non-guardian agent must review instead."""
    c, sup = live
    proj = create_demo_project(c, "research")
    rows = c.db.query("SELECT a.id, v.role_default FROM agent_identities a JOIN agent_versions v ON"
                      " v.id = a.current_version_id WHERE a.project_id = ? OR a.project_id IS NULL", (proj,))
    reviewer = next(r["id"] for r in rows if r["role_default"] == "reviewer")
    from nexus.orchestration.verifier import Verifier
    pick = Verifier(c)._pick_reviewer({"project_id": proj, "agent_id": reviewer})
    assert pick is not None and pick["id"] != reviewer and pick["role_default"] not in ("reviewer", "guardian")
    assert pick["role_default"] == "planner"


async def test_explorer_that_never_stops_gets_a_final_no_tools_turn(live):
    """Seen live: a planner kept reading files on a large repository until 'tool loop exceeded 8 rounds', three times.
    Now the last turn disables tools (tool_choice none) and the agent answers from what it has learned."""
    c, sup = live
    pid, model, _ = configure(c, "deepseek")
    seen_none = []

    def explorer(req, packet):
        from nexus.demo.script import demo_script

        if req.metadata.get("purpose") == "plan" and getattr(req, "tool_choice", None) != "none":
            return {"__tool_calls__": [{"name": "list_files", "arguments": {}}]}  # would explore forever
        return demo_script(req, packet)

    class Sim(ProviderSim):
        def _parse_openai(self, b, request):
            req = super()._parse_openai(b, request)
            req.tool_choice = b.get("tool_choice")
            if req.tool_choice == "none":
                seen_none.append(True)
            return req

    sim = Sim("deepseek", brain=explorer)
    c.registry.register_override(pid, SETUPS["deepseek"][0](sim))
    proj = create_demo_project(c, "research")
    mid = new_mission(c, proj, mode="live", cap=1.0)
    status = await sup.wait_idle(mid, timeout=90)
    assert status == "completed", ({k: (v["status"], v["status_reason"]) for k, v in tasks(c, mid).items()}, sim.violations[:3])
    assert seen_none, "the final turn must forbid tool calls"
    plan_calls = c.db.query("SELECT operation_id FROM model_calls WHERE mission_id = ? AND purpose = 'plan'", (mid,))
    assert len(plan_calls) == 9  # 8 exploring rounds + 1 final answer


def test_plan_addressing_several_conditions_is_valid_and_malformed_plans_never_crash():
    from nexus.domain.verification import validate_plan

    plan = {"tasks": [{"key": "do-both", "title": "t", "kind": "write", "description": "d", "addresses": ["SC1", "SC2"],
                       "depends_on": [], "acceptance": [{"id": "A1", "text": "x", "check": {"kind": "reviewer"}}]}]}
    assert validate_plan(plan, success_condition_ids=["SC1", "SC2"], constraint_ids=[]) == []
    weird = {"tasks": [{"key": "x", "title": "t", "kind": "write", "description": "d", "addresses": {"a": 1},
                        "depends_on": "other", "constraint_ids": [["C1"]],
                        "acceptance": [{"id": "A1", "text": "x", "check": {"kind": "reviewer"}}]}]}
    errs = validate_plan(weird, success_condition_ids=["SC1"], constraint_ids=["C1"])
    assert any("depends_on must be a list" in e for e in errs) and any("constraint_ids" in e for e in errs)
    assert any("SC1 is not addressed" in e for e in errs)


def test_old_tool_results_are_elided_but_recent_ones_stay_whole():
    """Seen live: an engineer reading a large repository reached 67k input tokens per call and spent $1.78 of a $2 cap
    before editing anything, because every tool result was re-sent in full on every call."""
    from nexus.orchestration.worker import TOOL_CONTEXT_CHARS, compact_tool_results
    from nexus.providers.base import ChatMessage, ToolCall

    msgs = [ChatMessage("user", "task")]
    for i in range(12):
        msgs.append(ChatMessage("assistant", "", tool_calls=[ToolCall(f"c{i}", "repo_read", {"path": f"f{i}.py"})]))
        msgs.append(ChatMessage("tool", f"file {i} " + "x" * 15000, tool_call_id=f"c{i}", tool_name="repo_read"))
        compact_tool_results(msgs)
    tools = [m for m in msgs if m.role == "tool"]
    from nexus.orchestration.worker import KEEP_RECENT_TOOL_RESULTS as K

    assert all(len(m.content) > 15000 for m in tools[-K:]), "the most recent results stay whole"
    assert all(len(m.content) < 700 for m in tools[:-K]), "older results are elided once over budget"
    before = 12 * 15007
    assert sum(len(m.content) for m in tools) < before / 3
    assert tools[0].content.startswith("file 0 ") and "call the tool again" in tools[0].content
    assert [m.tool_call_id for m in tools] == [f"c{i}" for i in range(12)], "pairing with tool calls is preserved"


async def test_cached_input_is_billed_at_the_cached_rate_and_balance_is_compared(live):
    """DeepSeek reports how many prompt tokens were cache hits (billed lower); NEXUS used to bill them all at the
    full input rate. The account balance endpoint lets the owner compare NEXUS's computed spend with real deductions."""
    from nexus.providers.balance import comparison, snapshot

    c, sup = live
    pid, model, _ = configure(c, "deepseek")
    sim = ProviderSim("deepseek")
    sim.cache_hit_ratio = 0.75
    c.registry.register_override(pid, SETUPS["deepseek"][0](sim))
    c.registry.set_price(pid, model, 0.28, 0.42, source="test", cached_input_per_mtok=0.028)
    with c.db.tx() as cur:
        cur.execute("UPDATE providers SET status = 'ok' WHERE id = ?", (pid,))
    first = await snapshot(c, pid)
    assert first["total_balance"] == 10.0 and first["currency"] == "USD"
    proj = create_demo_project(c, "research")
    mid = new_mission(c, proj, mode="live", cap=1.0)
    assert await sup.wait_idle(mid, timeout=90) == "completed"
    calls = c.db.query("SELECT input_tokens, cached_input_tokens, output_tokens, actual_usd FROM model_calls"
                       " WHERE mission_id = ?", (mid,))
    for x in calls:
        assert x["cached_input_tokens"] == int(x["input_tokens"] * 0.75)
        expect = ((x["input_tokens"] - x["cached_input_tokens"]) * 0.28 + x["cached_input_tokens"] * 0.028
                  + x["output_tokens"] * 0.42) / 1_000_000
        assert abs(x["actual_usd"] - expect) < 1e-12
    spent = sum(x["actual_usd"] for x in calls)
    sim.balance = 10.0 - round(spent, 2) - 0.01  # what the provider deducted
    await snapshot(c, pid)
    sim.balance += 5.0  # a top-up is not counted as spend
    await snapshot(c, pid)
    cmp = comparison(c.db, pid)
    assert cmp["snapshots"] == 3 and cmp["topups"] == 5.0 and cmp["calls"] == len(calls)
    assert abs(cmp["deducted"] - (round(spent, 2) + 0.01)) < 1e-9
    assert abs(cmp["nexus_computed_usd"] - round(spent, 4)) < 1e-4
