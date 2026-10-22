"""Autonomy within owner bounds: real actions via connectors (HTTP, GitHub, MCP), objective
discovery, strategy proposals, agent-created tools, and collaboration."""
import json
from pathlib import Path

import httpx
import pytest

from helpers import events, new_mission, tasks
from nexus.bootstrap import create_demo_project, ensure_fake_provider
from nexus.connectors.service import ActionPolicyError
from nexus.domain.missions import create_project
from nexus.tools.broker import ToolContext
from nexus.tools.builtin import expand_capabilities


class FakeApi(httpx.AsyncBaseTransport):
    """A destination service: records writes by idempotency key; can time out after applying."""

    def __init__(self, *, timeout_first_write=False):
        self.writes: list[dict] = []
        self.applied: dict[str, int] = {}
        self.timeout_first_write = timeout_first_write

    async def handle_async_request(self, request):
        path = request.url.path
        if request.method == "GET" and path == "/status":
            k = request.url.params.get("idempotency_key")
            return httpx.Response(200, json={"applied": k in self.applied})
        if request.method == "GET" and path.startswith("/notes"):
            return httpx.Response(200, json={"notes": [w["body"] for w in self.writes]})
        if request.method == "POST":
            key = request.headers.get("idempotency-key")
            self.applied[key] = self.applied.get(key, 0) + 1
            self.writes.append({"key": key, "body": json.loads(request.content), "auth": request.headers.get("authorization")})
            if self.timeout_first_write and len(self.writes) == 1:
                raise httpx.ReadTimeout("response lost", request=request)
            return httpx.Response(201, json={"id": len(self.writes)})
        return httpx.Response(404)


HTTP_CONFIG = {"base_url": "https://team.example.test", "actions": [
    {"name": "list_notes", "method": "GET", "path": "/notes", "description": "Read the team channel", "params": {}},
    {"name": "post_note", "method": "POST", "path": "/notes", "description": "Post a note to the team channel",
     "params": {"text": "string"}, "status_path": "/status"},
    {"name": "purge_channel", "method": "DELETE", "path": "/notes", "description": "Delete all notes", "params": {},
     "effect_class": "destructive"},
]}


def svc(ctx):
    return ctx.services.connectors


async def _http_connector(ctx, pid, api):
    svc(ctx).transport = api
    cid = svc(ctx).add(project_id=pid, kind="http", label="Team", config=HTTP_CONFIG, secret="tok-SECRET-123456")
    res = await svc(ctx).discover(cid)
    assert res["status"] == "ok", res
    return cid


def _action_id(ctx, cid, name):
    return ctx.db.one("SELECT id FROM connector_actions WHERE connector_id = ? AND name = ?", (cid, name))["id"]


async def test_http_connector_policy_approval_and_ambiguity(ctx):
    with ctx.db.tx() as cur:
        pid = create_project(cur, name="p", workspace_root=str(ctx.data_dir))
    api = FakeApi(timeout_first_write=True)
    cid = await _http_connector(ctx, pid, api)
    acts = {a["name"]: a for a in ctx.db.query("SELECT * FROM connector_actions WHERE connector_id = ?", (cid,))}
    assert (acts["list_notes"]["autonomy"], acts["post_note"]["autonomy"], acts["purge_channel"]["autonomy"]) == \
           ("auto", "ask", "deny"), "conservative defaults"
    with pytest.raises(ActionPolicyError):
        svc(ctx).set_policy(acts["purge_channel"]["id"], autonomy="auto")
    with pytest.raises(PermissionError):
        svc(ctx).set_policy(acts["post_note"]["id"], autonomy="auto", actor="agent:x")

    common = dict(project_id=pid, mission_id=None, task_id=None, attempt_id=None, requested_by="agent:a", connector_id=cid)
    r = await svc(ctx).request(action="list_notes", args={}, rationale="check channel", operation_id="op-read", **common)
    assert r["status"] == "succeeded" and r["approval"] == "auto:policy" and r["evidence_id"]
    bad = await svc(ctx).request(action="post_note", args={"txt": 1}, rationale="x", operation_id="op-bad", **common)
    assert bad["status"] == "denied" and "invalid arguments" in bad["decision_note"]
    p = await svc(ctx).request(action="post_note", args={"text": "hello"}, rationale="share summary",
                               operation_id="op-post", **common)
    assert p["status"] == "pending" and api.writes == [], "nothing is sent before approval"
    again = await svc(ctx).request(action="post_note", args={"text": "hello"}, rationale="share summary",
                                   operation_id="op-post", **common)
    assert again["id"] == p["id"], "duplicate request deduplicated"
    with pytest.raises(PermissionError):
        await svc(ctx).decide(p["id"], approve=True, actor="agent:a")
    d = await svc(ctx).decide(p["id"], approve=True, note="ok")
    assert d["status"] == "uncertain", "the write landed but the response was lost"
    assert len(api.writes) == 1 and api.writes[0]["auth"] == "Bearer tok-SECRET-123456"
    await svc(ctx).execute(p["id"])  # a second execution attempt is a no-op: no blind retry
    assert len(api.writes) == 1
    rec = await svc(ctx).reconcile(p["id"])
    assert rec["reconcile"] == "succeeded" and rec["status"] == "succeeded"
    stored = json.dumps([dict(r) for r in ctx.db.query("SELECT * FROM action_requests")])
    assert "tok-SECRET-123456" not in stored, "credentials never stored in records"


class FakeGitHub(httpx.AsyncBaseTransport):
    def __init__(self):
        self.issues = []

    async def handle_async_request(self, request):
        if request.method == "GET" and request.url.path == "/repos/acme/site":
            return httpx.Response(200, json={"full_name": "acme/site"})
        if request.method == "GET" and request.url.path == "/repos/acme/site/issues":
            return httpx.Response(200, json=self.issues)
        if request.method == "POST" and request.url.path == "/repos/acme/site/issues":
            body = json.loads(request.content)
            self.issues.append({"number": len(self.issues) + 1, "title": body["title"], "body": body["body"]})
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(404)


async def test_github_ambiguous_create_reconciled_by_marker(ctx):
    with ctx.db.tx() as cur:
        pid = create_project(cur, name="p", workspace_root=str(ctx.data_dir))
    gh = FakeGitHub()
    svc(ctx).transport = gh
    cid = svc(ctx).add(project_id=pid, kind="github", label="Site repo", config={"repo": "acme/site"}, secret="ghp_x" * 4)
    assert (await svc(ctx).discover(cid))["status"] == "ok"
    close = _action_id(ctx, cid, "close_issue")
    assert ctx.db.one("SELECT autonomy FROM connector_actions WHERE id = ?", (close,))["autonomy"] == "deny"
    svc(ctx).set_policy(_action_id(ctx, cid, "create_issue"), autonomy="auto")
    r = await svc(ctx).request(project_id=pid, mission_id=None, task_id=None, attempt_id=None, requested_by="agent:a",
                               connector_id=cid, action="create_issue", args={"title": "Broken link", "body": "Fix /about"},
                               rationale="found broken link", operation_id="op-gh")
    assert r["status"] == "uncertain" and len(gh.issues) == 1
    assert f"nexus-op:{r['idempotency_key']}" in gh.issues[0]["body"]
    rec = await svc(ctx).reconcile(r["id"])
    assert rec["status"] == "succeeded" and len(gh.issues) == 1


async def test_mcp_connector_classifies_effects_from_annotations(ctx):
    import mcp_types as types
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("calendar")
    booked = []

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=True))
    def list_slots(day: str) -> list[str]:
        """Free calendar slots for a day"""
        return [f"{day} 10:00", f"{day} 14:00"]

    @server.tool()
    def book_slot(slot: str) -> str:
        """Book a meeting slot"""
        booked.append(slot)
        return f"booked {slot}"

    @server.tool(annotations=types.ToolAnnotations(destructive_hint=True))
    def clear_day(day: str) -> str:
        """Cancel every meeting on a day"""
        return "cleared"

    with ctx.db.tx() as cur:
        pid = create_project(cur, name="p", workspace_root=str(ctx.data_dir))
    cid = svc(ctx).add(project_id=pid, kind="mcp", label="Calendar", config={"transport": "http", "url": "http://127.0.0.1:1/mcp"},
                       secret=None)
    svc(ctx).mcp_overrides[cid] = server
    res = await svc(ctx).discover(cid)
    assert res["status"] == "ok", res
    eff = {a["name"]: (a["effect_class"], a["autonomy"]) for a in
           ctx.db.query("SELECT * FROM connector_actions WHERE connector_id = ?", (cid,))}
    assert eff == {"list_slots": ("read_only", "auto"), "book_slot": ("external_write", "ask"),
                   "clear_day": ("destructive", "deny")}
    r = await svc(ctx).request(project_id=pid, mission_id=None, task_id=None, attempt_id=None, requested_by="agent:a",
                               connector_id=cid, action="list_slots", args={"day": "Mon"}, rationale="find a time",
                               operation_id="op-mcp")
    assert r["status"] == "succeeded" and "10:00" in json.dumps(r["result"])
    b = await svc(ctx).request(project_id=pid, mission_id=None, task_id=None, attempt_id=None, requested_by="agent:a",
                               connector_id=cid, action="book_slot", args={"slot": "Mon 10:00"}, rationale="book review",
                               operation_id="op-mcp-2")
    assert b["status"] == "pending" and booked == []
    await svc(ctx).decide(b["id"], approve=True)
    assert booked == ["Mon 10:00"]


async def test_act_mission_waits_for_approval_then_completes(ctx, sup):
    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "research")
    api = FakeApi()
    cid = await _http_connector(ctx, pid, api)
    mid = new_mission(ctx, pid, objective="Brief the team on resident demand",
                      success=["Research resident demand using workspace sources",
                               "Notify the team channel with the findings"])
    await sup.wait_idle(mid, timeout=60)
    ts = tasks(ctx, mid)
    act = next(t for t in ts.values() if t["kind"] == "act")
    assert act["status"] == "blocked" and "awaiting owner approval" in act["status_reason"], act
    assert api.writes == [], "no real action without approval"
    req = ctx.db.one("SELECT * FROM action_requests WHERE task_id = ?", (act["id"],))
    assert req["status"] == "pending" and req["requested_by"].startswith("agent:")
    assert events(ctx, mid, "attention.action_approval")
    await svc(ctx).decide(req["id"], approve=True, note="approved by owner")
    assert await sup.wait_idle(mid, timeout=60) == "completed"
    assert len(api.writes) == 1 and api.writes[0]["key"] == req["idempotency_key"]
    act = tasks(ctx, mid)[act["key"]]
    assert act["status"] == "accepted"
    ev = ctx.db.one("SELECT * FROM evidence WHERE locator = ?", (f"action:{req['id']}",))
    assert ev["kind"] == "observation"
    reviews = ctx.db.query("SELECT r.check_kind, r.verdict, r.blocking FROM reviews r JOIN acceptances a ON"
                           " a.submission_id = r.submission_id WHERE a.task_id = ?", (act["id"],))
    assert ("actions_settled", "pass", 1) in {(r["check_kind"], r["verdict"], r["blocking"]) for r in reviews}


async def test_denied_action_is_not_success(ctx, sup):
    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "research")
    cid = await _http_connector(ctx, pid, FakeApi())
    mid = new_mission(ctx, pid, success=["Notify the team channel that work started"])
    await sup.wait_idle(mid, timeout=60)
    act = next(t for t in tasks(ctx, mid).values() if t["kind"] == "act")
    req = ctx.db.one("SELECT * FROM action_requests WHERE task_id = ?", (act["id"],))
    await svc(ctx).decide(req["id"], approve=False, note="not now")
    await sup.wait_idle(mid, timeout=60)
    subs = ctx.db.query("SELECT status FROM submissions WHERE task_id = ? ORDER BY created_at", (act["id"],))
    assert "accepted" not in [s["status"] for s in subs]
    fails = ctx.db.query("SELECT r.summary FROM (SELECT json_extract(findings_json, '$.summary') AS summary, verdict FROM"
                         " reviews WHERE task_id = ?) r WHERE r.verdict = 'fail'", (act["id"],))
    assert any("denied" in (f["summary"] or "") for f in fails)


async def test_objective_discovery_requires_goal_link_and_owner_adoption(ctx, sup):
    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "research")
    obj = ctx.services.objectives
    with pytest.raises(ValueError):
        obj.start_discovery(pid, execution_mode="offline_fake", spend_cap_usd=None)
    goals = obj.set_goals(pid, ["Help Larkspur decide on bike-share with sound evidence"])
    mid = obj.start_discovery(pid, execution_mode="offline_fake", spend_cap_usd=None)
    assert await sup.wait_idle(mid, timeout=60) == "completed"
    props = ctx.db.query("SELECT * FROM objective_proposals WHERE project_id = ?", (pid,))
    assert len(props) == 1 and props[0]["serves_goal"] == goals[0]["id"] and props[0]["status"] == "proposed"
    assert json.loads(props[0]["evidence_json"]), "proposal carries evidence"
    bad = obj.propose(project_id=pid, proposed_by="agent:x", title="Launch a crypto token", objective="Make money",
                      success_conditions=["Sell tokens"], serves_goal="G42", rationale="why not")
    assert bad["status"] == "rejected" and "standing goals" in bad["problems"][0]
    with pytest.raises(PermissionError):
        obj.adopt(props[0]["id"], actor="agent:x")
    new_mid = obj.adopt(props[0]["id"], actor="owner")
    m = ctx.db.one("SELECT * FROM missions WHERE id = ?", (new_mid,))
    assert m["status"] == "running" and m["title"].startswith("Close the evidence gap")
    # Auto-adoption only inside the owner's policy.
    with ctx.db.tx() as cur:
        cur.execute("INSERT INTO settings VALUES ('objectives.policy', ?, '')",
                    (json.dumps({"auto_adopt": True, "goal_ids": [goals[0]["id"]], "max_cost_usd": 0.5}),))
    ok = obj.propose(project_id=pid, proposed_by="agent:x", title="Count docks", objective="Inventory docking sites",
                     success_conditions=["List candidate sites"], serves_goal=goals[0]["id"], rationale="cheap check",
                     est_cost_usd=0.1)
    big = obj.propose(project_id=pid, proposed_by="agent:x", title="Run citywide trial", objective="Trial",
                      success_conditions=["Run it"], serves_goal=goals[0]["id"], rationale="r", est_cost_usd=5.0)
    assert ok["status"] == "adopted" and big["status"] == "proposed"


def _tctx(ctx, pid, role, caps):
    agent = ctx.db.one("SELECT a.id FROM agent_identities a JOIN agent_versions v ON v.id = a.current_version_id WHERE"
                       " a.project_id = ? AND v.role_default = ?", (pid, role))["id"]
    root = Path(ctx.db.one("SELECT workspace_root FROM projects WHERE id = ?", (pid,))["workspace_root"])
    return ToolContext(project_id=pid, workspace_root=root, agent_id=agent,
                       allowed_tools=expand_capabilities(ctx.broker, caps))


async def test_agents_create_tools_that_cannot_escalate(ctx, sup):
    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "research")
    eng = _tctx(ctx, pid, "engineer", ["create_tools", "read_source", "custom_tools"])
    spec = {"steps": [{"tool": "read_source", "args": {"path": "{{input.path}}"}, "save_as": "src"}],
            "output": {"title": "{{steps.src.result.title}}", "lineage": "{{steps.src.result.lineage_root}}"}}
    res = await ctx.broker.invoke(eng, "create_tool", {
        "name": "source_lineage", "description": "Return a source's title and lineage", "kind": "composite",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "spec": spec, "tests": [{"input": {"path": "sources/regional-news.md"},
                                 "expect": {"path": "lineage", "equals": "survey:larkspur-transit-2026"}}]},
        operation_id="op-ct")
    assert res["ok"] and res["result"]["status"] == "tested", res
    tid = res["result"]["tool_definition_id"]
    assert "tool__source_lineage" not in [t.name for t in ctx.broker.specs_for(eng)], "not usable before approval"
    with pytest.raises(PermissionError):
        ctx.services.toolsmith.approve(tid, actor="agent:x")
    ctx.services.toolsmith.approve(tid, actor="owner")
    assert "tool__source_lineage" in [t.name for t in ctx.broker.specs_for(eng)]
    out = await ctx.broker.invoke(eng, "tool__source_lineage", {"path": "sources/council-minutes.md"}, operation_id="op-use")
    assert out["ok"] and out["result"]["lineage"] == "file:sources/council-minutes.md"
    # A caller without the step's capability gets nothing extra from the composite.
    weak = _tctx(ctx, pid, "operator", ["custom_tools"])
    denied = await ctx.broker.invoke(weak, "tool__source_lineage", {"path": "sources/council-minutes.md"}, operation_id="op-w")
    assert not denied["ok"] and "lacks capability 'read_source'" in denied["error"]
    # Python tools never run unsandboxed.
    py = await ctx.broker.invoke(eng, "create_tool", {
        "name": "word_count", "description": "count words", "kind": "python",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "spec": {"code": "def run(input):\n    return len(input['text'].split())\n"},
        "tests": [{"input": {"text": "a b"}, "expect": {"equals": 2}}]}, operation_id="op-py")
    row = ctx.db.one("SELECT status, status_detail FROM tool_definitions WHERE name = 'word_count'")
    assert row["status"] == "candidate" and "sandbox unavailable" in row["status_detail"], (py, dict(row))
    # A composite that reaches an unknown tool is rejected outright.
    bad = await ctx.broker.invoke(eng, "create_tool", {"name": "sneaky", "description": "x", "kind": "composite",
                                                       "input_schema": {"type": "object"},
                                                       "spec": {"steps": [{"tool": "set_budget", "args": {}}]},
                                                       "tests": [{"input": {}}]}, operation_id="op-bad")
    assert bad["result"]["status"] == "rejected"


async def test_collaboration_consult_delegate_and_strategy(ctx, sup):
    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid)
    assert await sup.wait_idle(mid, timeout=60) == "completed"
    consult = ctx.db.one("SELECT * FROM model_calls WHERE purpose = 'consult'")
    researcher = ctx.db.one("SELECT a.id FROM agent_identities a JOIN agent_versions v ON v.id = a.current_version_id"
                            " WHERE a.project_id = ? AND v.role_default = 'researcher'", (pid,))["id"]
    assert consult and consult["agent_id"] == researcher, "the consulted agent answers under its own identity"
    qa = ctx.db.query("SELECT type, sender FROM messages WHERE mission_id = ? AND operation_id LIKE '%:consult:%'"
                      " ORDER BY created_at", (mid,))
    assert [m["type"] for m in qa][:1] == ["question"] and len(qa) == 2
    final = ctx.artifacts.read_text(ctx.db.one("SELECT current_artifact_id FROM tasks WHERE mission_id = ? AND key ="
                                               " 'integrate'", (mid,))["current_artifact_id"])
    assert "single survey" in final

    # Delegation and strategy proposals on a fresh mission (before execution).
    mid2 = new_mission(ctx, pid, start=False)
    plan = tasks(ctx, mid2)["plan"]
    tctx = _tctx(ctx, pid, "planner", ["delegate_task", "propose_strategy", "consult"])
    tctx.mission_id, tctx.task_id = mid2, plan["id"]
    d = await ctx.broker.invoke(tctx, "delegate_task", {"title": "Check dock costs", "kind": "research",
                                                        "description": "Find dock cost evidence"}, operation_id="op-d")
    assert d["ok"] and d["result"]["role"] == "researcher"
    sub_key = d["result"]["key"]
    sp = await ctx.broker.invoke(tctx, "propose_strategy", {"task_key": sub_key, "mode": "independent_investigation",
                                                            "rationale": "costs vary by vendor", "switch_if": "one vendor only"},
                                 operation_id="op-s")
    assert sp["result"]["status"] == "applied"
    assert json.loads(tasks(ctx, mid2)[sub_key]["strategy_json"])["mode"] == "independent_investigation"
    big = await ctx.broker.invoke(tctx, "propose_strategy", {"task_key": sub_key, "mode": "bounded_parallel_exploration",
                                                             "rationale": "r", "switch_if": "s", "extra_branches": 5},
                                  operation_id="op-s2")
    assert big["result"]["status"] == "awaiting owner decision"
    bogus = await ctx.broker.invoke(tctx, "propose_strategy", {"task_key": sub_key, "mode": "unlimited_swarm",
                                                               "rationale": "r", "switch_if": "s"}, operation_id="op-s3")
    assert bogus["result"]["status"] == "rejected"
    rv = await ctx.broker.invoke(tctx, "consult", {"role": "reviewer", "question": "will you pass this?"},
                                 operation_id="op-c")
    assert not rv["ok"] and "independent" in rv["error"]
    # Depth limit: a sub-subtask's own delegation is refused at depth 3.
    t2 = tasks(ctx, mid2)[sub_key]
    tctx.task_id = t2["id"]
    d2 = await ctx.broker.invoke(tctx, "delegate_task", {"title": "deeper", "kind": "research", "description": "x"},
                                 operation_id="op-d2")
    tctx.task_id = d2["result"]["task_id"]
    d3 = await ctx.broker.invoke(tctx, "delegate_task", {"title": "deepest", "kind": "research", "description": "x"},
                                 operation_id="op-d3")
    assert not d3["ok"] and "depth limit" in d3["error"]
