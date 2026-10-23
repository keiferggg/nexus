"""HTTP API (authenticated loopback only) and resumable SSE event stream."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..appctx import AppContext
from ..bootstrap import create_demo_project, ensure_fake_provider
from ..domain import missions as M
from ..domain import rooms
from ..domain.budget import summary as budget_summary
from ..domain.execution import StaleSubmissionError
from ..domain.tasks import TransitionError, transitions_spec, waive_dependency
from ..domain.understanding import ObjectionInvalid, add_objection, change_evidence, resolve_proposal
from ..learning import opportunities as O
from ..learning.skills import SkillRegistry
from ..orchestration.adapter import FRAMEWORK_VERSIONS
from ..orchestration.supervisor import Supervisor
from ..persistence.db import ConflictError, dumps, loads, new_id, now_iso
from ..persistence.events import emit, read_events
from ..providers.base import ProviderError
from . import views


class ProviderIn(BaseModel):
    kind: str
    label: str
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)


class KeyIn(BaseModel):
    api_key: str = Field(repr=False)


class PriceIn(BaseModel):
    model: str
    input_per_mtok_usd: float
    output_per_mtok_usd: float
    cached_input_per_mtok_usd: float | None = None
    source: str = "owner-configured"


class ProjectIn(BaseModel):
    name: str
    workspace_root: str
    fetch_allowlist: list[str] = []


class MissionIn(BaseModel):
    title: str
    objective: str
    success_conditions: list[str]
    constraints: list[str] = []
    execution_mode: str = "offline_fake"
    spend_cap_usd: float | None = None
    max_active_workers: int = 4
    max_branches: int = 2
    start: bool = True
    data_exposure_acknowledged: bool = False
    unattended_web_hosts: list[str] = []
    unattended_emails: list[str] = []


class ReviseIn(BaseModel):
    objective: str | None = None
    success_conditions: list[str] | None = None
    constraints: list[str] | None = None
    reason: str


class MessageIn(BaseModel):
    type: str = "question"
    body: str
    task_id: str | None = None
    recipients: list[str] = []
    expects_response: str | None = None
    operation_id: str | None = None


class ObjectionIn(BaseModel):
    target_ref: str
    summary: str
    mechanism: str
    evidence_or_test: str
    resolution_condition: str
    blocking: bool = True


class ResolveIn(BaseModel):
    resolution: str
    status: str = "resolved"


class OwnerReviewIn(BaseModel):
    criterion: str
    verdict: str
    note: str = ""


class EvidenceChangeIn(BaseModel):
    status: str = "withdrawn"
    reason: str


class AgentUpdateIn(BaseModel):
    role: str | None = None
    model_policy: dict[str, Any] | None = None
    tools: list[str] | None = None
    memory_scope: str | None = None


class SettingIn(BaseModel):
    value: Any


SETTABLE = {"default_model_policy", "learning.auto_promote", "initiative.policy", "retention.days",
            "data_exposure.acknowledged", "concurrency.defaults", "objectives.policy", "tools.policy",
            "chat.limits", "chat.data_ack"}


class ConnectorIn(BaseModel):
    kind: str
    label: str
    config: dict[str, Any]
    secret: str | None = Field(default=None, repr=False)


class ActionPolicyIn(BaseModel):
    autonomy: str | None = None
    max_auto_per_day: int | None = None
    enabled: bool | None = None


class NoteIn(BaseModel):
    note: str = ""


class ResolveActionIn(BaseModel):
    outcome: str
    note: str


class GoalsIn(BaseModel):
    goals: list[str]


class DiscoverIn(BaseModel):
    execution_mode: str = "offline_fake"
    spend_cap_usd: float | None = None
    focus: str = ""


class InternetIn(BaseModel):
    mode: str = "read_write"
    write_autonomy: str = "ask"
    blocked_domains: list[str] = []
    allowed_private_hosts: list[str] = []
    max_requests_per_minute: int = 120
    max_auto_writes_per_day: int = 200
    contact: str = ""


class SandboxIn(BaseModel):
    image: str = ""
    memory_mb: int | None = None
    cpus: float | None = None
    tmpfs_mb: int | None = None
    timeout_s: int | None = None
    test_command: list[str] | None = None


class AutopilotLimitsIn(BaseModel):
    run_cap_usd: float | None = None
    daily_cap_usd: float | None = None


class ChatIn(BaseModel):
    text: str = ""
    title: str = "New chat"


class AutonomyIn(BaseModel):
    mode: str = "supervised"
    web_hosts: list[str] = []
    email_domains: list[str] = []
    max_writes_per_mission: int = 25
    max_writes_per_hour: int = 30
    monitor: bool = True
    suspend_after_violations: int = 3


class SearchIn(BaseModel):
    kind: str = "duckduckgo"
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)


class AdoptIn(BaseModel):
    execution_mode: str = "offline_fake"
    spend_cap_usd: float | None = None
    note: str = ""


def create_app(ctx: AppContext, *, token: str, allowed_origins: list[str] | None = None) -> FastAPI:
    sup = Supervisor(ctx)
    state: dict[str, Any] = {"startup_report": None, "started": False}
    origins = set(allowed_origins or [])
    from ..chat.narrator import Narrator
    from ..chat.service import ChatService

    chat = ChatService(ctx, sup)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_fake_provider(ctx)
        state["startup_report"] = await sup.start()
        narrator = Narrator(ctx, chat)
        narrator.start()
        if os.environ.get("NEXUS_AUTOPILOT", "1") != "0":
            chat.autopilot.start()
        from ..providers.balance import BalanceWatcher

        balances = BalanceWatcher(ctx)
        if os.environ.get("NEXUS_BALANCE_WATCH", "1") != "0":
            balances.start()
        state["started"] = True
        yield
        await balances.stop()
        await chat.autopilot.stop()
        await narrator.stop()
        await sup.stop()

    app = FastAPI(title="NEXUS local service", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {token}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return await call_next(request)
        except KeyError as exc:
            return JSONResponse({"error": f"not found: {exc}"}, status_code=404)
        except (ConflictError, TransitionError, M.MissionStateError, StaleSubmissionError, ObjectionInvalid) as exc:
            return JSONResponse({"error": str(exc), "kind": type(exc).__name__}, status_code=409)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except (ValueError, ProviderError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    db = ctx.db

    # ---- health & diagnostics --------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        ok, why = await ctx.sandbox.available()
        sec_ok, sec_backend = ctx.secrets.available()
        return {"status": "ready" if state["started"] else "starting", "version": __version__,
                "framework": FRAMEWORK_VERSIONS, "schema": [r["version"] for r in db.query("SELECT version FROM"
                                                                                          " schema_migrations")],
                "sandbox": {"available": ok, "detail": why}, "secrets": {"available": sec_ok, "backend": sec_backend},
                "supervisor_error": sup.last_error, "data_dir": str(ctx.data_dir)}

    @app.get("/api/diagnostics")
    async def diagnostics():
        counts = {t: db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in
                  ("projects", "missions", "tasks", "artifacts", "events", "model_calls", "tool_operations", "evidence")}
        return {"health": await health(), "counts": counts, "startup_reconciliation": state["startup_report"],
                "artifact_integrity": ctx.artifacts.verify_all(),
                "recent_errors": [dict(r) for r in db.query(
                    "SELECT type, payload_json, created_at FROM events WHERE type IN ('tool.failed','attempt.abandoned',"
                    " 'attention.task_blocked','model_call.finished') ORDER BY seq DESC LIMIT 30")],
                "activity_log": str(getattr(sup, "activity", None).folder) if getattr(sup, "activity", None) else None,
                "note": "Redacted: contains no credentials, prompts or artifact contents."}

    @app.get("/api/meta/transitions")
    async def meta_transitions():
        return transitions_spec()

    # ---- settings --------------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings():
        return {r["key"]: loads(r["value_json"]) for r in db.query("SELECT * FROM settings WHERE key NOT LIKE 'mission:%'")}

    @app.put("/api/settings/{key}")
    async def put_setting(key: str, body: SettingIn):
        if key not in SETTABLE:
            raise ValueError(f"unknown setting {key}")
        with db.tx() as cur:
            cur.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?,?,?)",
                        (key, dumps(body.value), now_iso()))
            emit(cur, "settings.changed", "owner", {"key": key})
        return {"ok": True}

    # ---- providers -------------------------------------------------------------------------
    @app.get("/api/providers")
    async def list_providers():
        out = []
        for r in db.query("SELECT * FROM providers ORDER BY created_at"):
            v = ctx.registry.public_view(r)
            v["prices"] = [dict(p) for p in db.query("SELECT model, input_per_mtok_usd, output_per_mtok_usd,"
                                                     " cached_input_per_mtok_usd, source, version"
                                                     " FROM model_prices WHERE provider_id = ? ORDER BY model, version",
                                                     (r["id"],))]
            out.append(v)
        return out

    @app.post("/api/providers")
    async def add_provider(body: ProviderIn):
        if body.kind == "fake":
            raise ValueError("the offline fake provider is built in")
        pid = ctx.registry.add(kind=body.kind, label=body.label, base_url=body.base_url, api_key=body.api_key)
        return ctx.registry.public_view(ctx.registry.get_row(pid))

    @app.post("/api/providers/{pid}/validate")
    async def validate_provider(pid: str):
        return await ctx.registry.validate(pid)

    @app.put("/api/providers/{pid}/key")
    async def replace_key(pid: str, body: KeyIn):
        ctx.registry.replace_key(pid, body.api_key)
        return {"ok": True}

    @app.delete("/api/providers/{pid}")
    async def delete_provider(pid: str):
        ctx.registry.remove(pid)
        return {"ok": True}

    @app.post("/api/providers/{pid}/check")
    async def check_provider(pid: str, body: SettingIn):
        """Owner-initiated capability check (3 tiny calls; billed by the provider)."""
        from ..providers.capability import check_model

        model = str(body.value or "")
        if not model:
            raise ValueError("choose a model to check")
        res = await check_model(ctx.gateway, pid, model)
        with db.tx() as cur:
            emit(cur, "provider.capability_checked", "owner", {"provider_id": pid, "model": model, "passed": res["passed"],
                                                               "results": res["results"]})
        return res

    @app.post("/api/providers/{pid}/prices")
    async def set_price(pid: str, body: PriceIn):
        v = ctx.registry.set_price(pid, body.model, body.input_per_mtok_usd, body.output_per_mtok_usd, body.source,
                                  body.cached_input_per_mtok_usd)
        return {"version": v}

    @app.get("/api/providers/{pid}/balance")
    async def provider_balance(pid: str):
        from ..providers.balance import comparison, supports_balance

        return {"supported": supports_balance(ctx.registry.get_row(pid)), **comparison(db, pid)}

    @app.post("/api/providers/{pid}/balance/refresh")
    async def provider_balance_refresh(pid: str):
        from ..providers.balance import comparison, snapshot

        await snapshot(ctx, pid)
        return {"supported": True, **comparison(db, pid)}

    # ---- projects & agents -----------------------------------------------------------------
    @app.get("/api/projects")
    async def list_projects():
        return [dict(r) for r in db.query("SELECT p.*, (SELECT COUNT(*) FROM missions m WHERE m.project_id = p.id) AS"
                                          " mission_count FROM projects p ORDER BY created_at")]

    @app.post("/api/projects")
    async def create_project(body: ProjectIn):
        root = Path(body.workspace_root)
        if not root.is_dir():
            raise ValueError(f"workspace folder does not exist: {root}")
        with db.tx() as cur:
            pid = M.create_project(cur, name=body.name, workspace_root=str(root.resolve()))
            cur.execute("UPDATE projects SET settings_json = ? WHERE id = ?",
                        (dumps({"fetch_allowlist": body.fetch_allowlist}), pid))
        return dict(db.one("SELECT * FROM projects WHERE id = ?", (pid,)))

    @app.post("/api/projects/demo/{kind}")
    async def demo_project(kind: str):
        if kind not in ("research", "code"):
            raise ValueError("demo kind must be research or code")
        pid = create_demo_project(ctx, kind)
        return dict(db.one("SELECT * FROM projects WHERE id = ?", (pid,)))

    @app.get("/api/projects/{pid}")
    async def get_project(pid: str):
        p = db.one("SELECT * FROM projects WHERE id = ?", (pid,))
        if p is None:
            raise KeyError(pid)
        return {**dict(p), "settings": loads(p["settings_json"], {})}

    @app.get("/api/projects/{pid}/agents")
    async def list_agents(pid: str):
        out = []
        for a in db.query("SELECT a.*, v.version, v.role_default, v.model_policy_json, v.tool_permissions_json,"
                          " v.memory_scope FROM agent_identities a JOIN agent_versions v ON v.id = a.current_version_id"
                          " WHERE a.project_id = ? OR a.project_id IS NULL ORDER BY a.created_at", (pid,)):
            perf = db.one(
                "SELECT COUNT(DISTINCT t.id) AS tasks, SUM(t.status = 'accepted') AS accepted,"
                " (SELECT COUNT(*) FROM submissions s JOIN tasks t2 ON t2.id = s.task_id WHERE t2.agent_id = ? AND"
                " s.status = 'rejected') AS rejected_submissions FROM tasks t WHERE t.agent_id = ?", (a["id"], a["id"]))
            cost = db.one("SELECT COALESCE(SUM(actual_usd),0) AS usd, COUNT(*) AS calls FROM model_calls WHERE agent_id = ?",
                          (a["id"],))
            active = db.query("SELECT key, status FROM tasks WHERE agent_id = ? AND status IN ('leased','running',"
                              "'submitted','verifying')", (a["id"],))
            versions = [dict(v) for v in db.query("SELECT version, role_default, created_at FROM agent_versions WHERE"
                                                  " agent_id = ? ORDER BY version", (a["id"],))]
            out.append({"id": a["id"], "name": a["name"], "charter": a["charter"], "version": a["version"],
                        "role": a["role_default"], "model_policy": loads(a["model_policy_json"], {}),
                        "tools": loads(a["tool_permissions_json"], []), "memory_scope": a["memory_scope"],
                        "performance": {**dict(perf), "model_calls": cost["calls"], "spend_usd": cost["usd"]},
                        "current_work": [dict(x) for x in active], "versions": versions})
        return out

    @app.put("/api/agents/{aid}")
    async def update_agent(aid: str, body: AgentUpdateIn):
        cur_v = db.one("SELECT v.* FROM agent_identities a JOIN agent_versions v ON v.id = a.current_version_id WHERE"
                       " a.id = ?", (aid,))
        if cur_v is None:
            raise KeyError(aid)
        with db.tx() as cur:
            vid = M.new_agent_version(cur, aid, role=body.role or cur_v["role_default"],
                                      model_policy=body.model_policy if body.model_policy is not None
                                      else loads(cur_v["model_policy_json"], {}),
                                      tools=body.tools if body.tools is not None else loads(cur_v["tool_permissions_json"], []),
                                      memory_scope=body.memory_scope or cur_v["memory_scope"])
            emit(cur, "agent.version_created", "owner", {"agent_id": aid, "agent_version_id": vid})
        return {"agent_version_id": vid}

    # ---- missions --------------------------------------------------------------------------
    @app.get("/api/projects/{pid}/missions")
    async def list_missions(pid: str):
        out = []
        for m in db.query("SELECT * FROM missions WHERE project_id = ? ORDER BY created_at DESC", (pid,)):
            c = db.one("SELECT COUNT(*) AS n, SUM(status='accepted') AS acc FROM tasks WHERE mission_id = ?", (m["id"],))
            out.append({**dict(m), "task_count": c["n"], "accepted_count": c["acc"] or 0})
        return out

    @app.get("/api/projects/{pid}/live-readiness")
    async def readiness(pid: str):
        from ..providers.readiness import live_readiness

        return live_readiness(db, ctx.registry, pid)

    @app.post("/api/projects/{pid}/missions")
    async def create_mission(pid: str, body: MissionIn):
        if body.execution_mode == "live" and not body.data_exposure_acknowledged:
            raise ValueError("live execution sends mission data to the configured providers; acknowledge the data"
                             " exposure summary first")
        if body.execution_mode == "live":
            from ..providers.readiness import live_readiness

            r = live_readiness(db, ctx.registry, pid)
            if not r["ready"]:
                raise ValueError("not ready for live execution: " + "; ".join(
                    f"{p['kind']}: {p.get('model') or p.get('agent')} - {p['fix']}" for p in r["problems"][:4]))
        with db.tx() as cur:
            mid = M.create_mission(cur, project_id=pid, title=body.title, objective=body.objective,
                                   success_conditions=body.success_conditions, constraints=body.constraints,
                                   execution_mode=body.execution_mode, spend_cap_usd=body.spend_cap_usd,
                                   max_active_workers=body.max_active_workers, max_branches=body.max_branches)
            if body.unattended_web_hosts or body.unattended_emails:
                cur.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?,?,?)",
                            (f"mission:{mid}:scope", dumps({"web_hosts": [h.strip().lower() for h in body.unattended_web_hosts
                                                                          if h.strip()],
                                                            "emails": [e.strip().lower() for e in body.unattended_emails
                                                                       if e.strip()]}), now_iso()))
            if body.start:
                M.start_mission(cur, mid)
        ctx.notifier._on_commit()
        return views.mission_view(db, mid)

    @app.get("/api/projects/{pid}/data-exposure")
    async def data_exposure(pid: str):
        """What leaves the machine on a live run, from configured agents and providers."""
        rows = []
        dflt = db.one("SELECT value_json FROM settings WHERE key = 'default_model_policy'")
        dflt = loads(dflt["value_json"], {}) if dflt else {}
        for a in await list_agents(pid):
            pol = {**dflt, **{k: v for k, v in a["model_policy"].items() if v}}
            prov = db.one("SELECT * FROM providers WHERE id = ?", (pol.get("provider_id"),)) if pol.get("provider_id") else None
            rows.append({"agent": a["name"], "role": a["role"], "provider": prov["label"] if prov else None,
                         "model": pol.get("model"),
                         "sends_off_machine": ctx.registry.public_view(prov)["sends_data_off_machine"] if prov else None})
        return {"agents": rows, "what_is_sent": [
            "mission objective, success conditions and constraints",
            "the task description and acceptance criteria",
            "contents of workspace sources and files the agent reads through tools",
            "accepted outputs of upstream tasks and the evidence index",
            "relevant memory items the agent is permitted to read"],
            "never_sent": ["API keys or other credentials", "files outside the project workspace",
                           "other projects' private memory"]}

    @app.get("/api/missions/{mid}")
    async def get_mission(mid: str):
        return views.mission_view(db, mid)

    @app.post("/api/missions/{mid}/start")
    async def start(mid: str):
        with db.tx() as cur:
            M.start_mission(cur, mid)
        ctx.notifier._on_commit()
        return views.mission_view(db, mid)

    @app.post("/api/missions/{mid}/pause")
    async def pause(mid: str):
        with db.tx() as cur:
            M.pause_mission(cur, mid)
        return views.mission_view(db, mid)

    @app.post("/api/missions/{mid}/cancel")
    async def cancel(mid: str):
        await sup.cancel_mission(mid)
        return views.mission_view(db, mid)

    @app.post("/api/missions/{mid}/revise")
    async def revise(mid: str, body: ReviseIn):
        with db.tx() as cur:
            res = M.revise_mission(cur, mid, objective=body.objective, success_conditions=body.success_conditions,
                                   constraints=body.constraints, reason=body.reason, actor="owner")
            m = M.get_mission(cur, mid)
            if m["status"] in ("completed", "blocked"):
                M.set_mission_status(cur, mid, "running", actor="owner", reason="requirements revised",
                                     allowed_from={"completed", "blocked"})
        ctx.notifier._on_commit()
        return res

    @app.put("/api/missions/{mid}/budget")
    async def set_budget(mid: str, body: SettingIn):
        cap = float(body.value)
        if cap <= 0:
            raise ValueError("spend cap must be positive")
        with db.tx() as cur:
            m = M.get_mission(cur, mid)
            cur.execute("UPDATE missions SET spend_cap_usd = ?, version = version + 1 WHERE id = ?", (cap, mid))
            emit(cur, "budget.cap_changed", "owner", {"from": m["spend_cap_usd"], "to": cap}, project_id=m["project_id"],
                 mission_id=mid)
        return budget_summary_for(mid)

    @app.post("/api/missions/{mid}/continue")
    async def continue_with_more_budget(mid: str, body: SettingIn):
        """Raise a mission's cap and resume the steps that stopped on it. Accepted work (plan, finished steps) is kept;
        only the stopped steps start a new attempt. Seen live: the only option was to cancel, which threw away an
        accepted plan and improvement choice."""
        n = sup.continue_with_budget(mid, float(body.value))
        ctx.notifier._on_commit()
        return {"resumed_tasks": n, "budget": budget_summary_for(mid)}

    def budget_summary_for(mid: str):
        with db.tx() as cur:
            return budget_summary(cur, mid)

    @app.get("/api/missions/{mid}/understanding")
    async def understanding(mid: str, since: int = 0):
        return views.understanding(db, mid, since)

    @app.get("/api/missions/{mid}/messages")
    async def messages(mid: str, thread: str | None = None):
        q = "SELECT * FROM messages WHERE mission_id = ?" + (" AND thread_id = ?" if thread else "") + " ORDER BY created_at"
        rows = db.query(q, (mid, thread) if thread else (mid,))
        return [{**dict(r), "recipients": loads(r["recipients_json"], []), "refs": loads(r["refs_json"], {}),
                 "deliveries": [dict(d) for d in db.query("SELECT recipient, status FROM message_deliveries WHERE"
                                                          " message_id = ?", (r["id"],))]} for r in rows]

    @app.post("/api/missions/{mid}/messages")
    async def post_message(mid: str, body: MessageIn):
        m = db.one("SELECT * FROM missions WHERE id = ?", (mid,))
        if m is None:
            raise KeyError(mid)
        thread = rooms.thread_for_task(body.task_id) if body.task_id else rooms.thread_for_mission(mid)
        recipients = body.recipients or ([f"agent:{db.one('SELECT agent_id FROM tasks WHERE id = ?', (body.task_id,))['agent_id']}"]
                                         if body.task_id else ["role:planner"])
        with db.tx() as cur:
            msg_id, created = rooms.post(cur, operation_id=body.operation_id or new_id("op"), project_id=m["project_id"],
                                         mission_id=mid, thread_id=thread, sender="owner", recipients=recipients,
                                         type=body.type, body=body.body, task_id=body.task_id,
                                         expects_response=body.expects_response)
        return {"message_id": msg_id, "created": created}

    @app.post("/api/missions/{mid}/objections")
    async def raise_objection(mid: str, body: ObjectionIn):
        with db.tx() as cur:
            oid = add_objection(cur, mission_id=mid, target_ref=body.target_ref, summary=body.summary,
                                mechanism=body.mechanism, evidence_or_test=body.evidence_or_test,
                                resolution_condition=body.resolution_condition, decision_owner="owner",
                                blocking=body.blocking, created_by="owner")
        return {"objection_id": oid}

    @app.post("/api/proposals/{pid}/resolve")
    async def resolve(pid: str, body: ResolveIn):
        with db.tx() as cur:
            resolve_proposal(cur, pid, resolution=body.resolution, actor="owner", status=body.status)
        return {"ok": True}

    @app.get("/api/attention")
    async def attention(mission_id: str | None = None):
        return views.attention(db, mission_id)

    # ---- tasks, submissions, artifacts, evidence ------------------------------------------
    @app.get("/api/tasks/{tid}")
    async def task(tid: str):
        return views.task_detail(db, tid)

    @app.post("/api/tasks/{tid}/retry")
    async def retry(tid: str):
        sup.retry_task(tid)
        ctx.notifier._on_commit()
        return views.task_detail(db, tid)

    @app.post("/api/tasks/{tid}/waive/{dep}")
    async def waive(tid: str, dep: str):
        with db.tx() as cur:
            waive_dependency(cur, tid, dep, actor="owner")
        return views.task_detail(db, tid)

    @app.post("/api/submissions/{sid}/owner-review")
    async def owner_review(sid: str, body: OwnerReviewIn):
        r = sup.owner_review(sid, criterion=body.criterion, verdict=body.verdict, note=body.note)
        ctx.notifier._on_commit()
        return r

    @app.get("/api/missions/{mid}/artifacts")
    async def mission_artifacts(mid: str, kind: str | None = None):
        q = "SELECT a.*, t.key AS task_key FROM artifacts a LEFT JOIN tasks t ON t.id = a.task_id WHERE a.mission_id = ?"
        params: tuple = (mid,)
        if kind:
            q += " AND a.kind = ?"
            params += (kind,)
        rows = db.query(q + " ORDER BY a.created_at", params)
        out = []
        for r in rows:
            acc = db.one("SELECT is_current, noncurrent_reason FROM acceptances WHERE artifact_id = ?", (r["id"],))
            sub = db.one("SELECT status FROM submissions WHERE artifact_id = ? ORDER BY created_at DESC LIMIT 1", (r["id"],))
            out.append({**{k: r[k] for k in r.keys() if not k.endswith("_json")}, "lineage": loads(r["lineage_json"], {}),
                        "input_versions": loads(r["input_versions_json"], {}),
                        "acceptance": dict(acc) if acc else None, "submission_status": sub["status"] if sub else None})
        return out

    @app.get("/api/artifacts/{aid}")
    async def artifact(aid: str, max_bytes: int = 400_000):
        meta = ctx.artifacts.meta(aid)
        if meta is None:
            raise KeyError(aid)
        data = ctx.artifacts.read(aid)
        text = data[:max_bytes].decode("utf-8", errors="replace") if meta["media_type"].startswith(("text/", "application/json")) else None
        return {"meta": meta, "content": text, "truncated": len(data) > max_bytes, "integrity": "sha256 verified"}

    @app.post("/api/evidence/{eid}/change")
    async def evidence_change(eid: str, body: EvidenceChangeIn):
        if body.status not in ("withdrawn", "changed"):
            raise ValueError("status must be withdrawn or changed")
        with db.tx() as cur:
            res = change_evidence(cur, eid, new_status=body.status, reason=body.reason, actor="owner")
            ev = cur.execute("SELECT mission_id FROM evidence WHERE id = ?", (eid,)).fetchone()
            m = M.get_mission(cur, ev["mission_id"]) if ev["mission_id"] else None
            if m and res["tasks"] and m["status"] in ("completed", "blocked"):
                M.set_mission_status(cur, m["id"], "running", actor="nexus:invalidation",
                                     reason="evidence changed; re-verifying affected work",
                                     allowed_from={"completed", "blocked"})
        ctx.notifier._on_commit()
        return res

    # ---- events, timeline, replay ----------------------------------------------------------
    @app.get("/api/events")
    async def events(after: int = 0, mission_id: str | None = None, limit: int = 500):
        return read_events(db, after=after, mission_id=mission_id, limit=min(limit, 2000))

    @app.get("/api/events/stream")
    async def stream(request: Request, after: int = 0, mission_id: str | None = None):
        last = request.headers.get("last-event-id")
        cursor = int(last) if last and last.isdigit() else after

        async def gen():
            nonlocal cursor
            yield f"retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                batch = read_events(db, after=cursor, mission_id=mission_id, limit=200)
                for ev in batch:
                    cursor = ev["seq"]
                    yield f"id: {ev['seq']}\nevent: nexus\ndata: {json.dumps(ev, default=str)}\n\n"
                if not batch:
                    await ctx.notifier.wait(15)
                    yield ": keep-alive\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    @app.get("/api/missions/{mid}/timeline")
    async def timeline(mid: str, after: int = 0):
        evs = read_events(db, after=after, mission_id=mid, limit=2000)
        ckpts = [dict(c) for c in db.query("SELECT c.*, a.task_id, a.attempt_no FROM checkpoint_refs c JOIN task_attempts a"
                                           " ON a.id = c.attempt_id JOIN tasks t ON t.id = a.task_id WHERE t.mission_id = ?"
                                           " ORDER BY c.recorded_at", (mid,))]
        return {"events": evs, "checkpoints": ckpts}

    @app.get("/api/attempts/{aid}/replay")
    async def replay(aid: str):
        """Debug replay: the recorded model outputs and tool results of an attempt, in order.
        No model is called; replay never changes state."""
        calls = db.query("SELECT * FROM model_calls WHERE attempt_id = ? ORDER BY started_at", (aid,))
        steps = []
        for c in calls:
            out = json.loads(ctx.artifacts.read_text(c["output_artifact_id"])) if c["output_artifact_id"] else None
            steps.append({"type": "model_call", "operation_id": c["operation_id"], "purpose": c["purpose"],
                          "status": c["status"], "output": out, "context_artifact_id": c["context_artifact_id"]})
        for o in db.query("SELECT * FROM tool_operations WHERE attempt_id = ? ORDER BY created_at", (aid,)):
            steps.append({"type": "tool", "operation_id": o["operation_id"], "tool": o["tool"], "status": o["status"],
                          "args": loads(o["args_json"], {}), "result": loads(o["result_json"]) if o["result_json"] else None,
                          "reason": o["decision_reason"]})
        steps.sort(key=lambda s: s["operation_id"])
        return {"attempt_id": aid, "mode": "recorded replay (no live calls)", "steps": steps}

    @app.post("/api/tasks/{tid}/branch")
    async def branch(tid: str):
        """Explicit new branch: reopen an accepted/failed task in a new epoch and run it again
        with live calls. The previous result stays inspectable as history."""
        with db.tx() as cur:
            M.invalidate_task(cur, tid, reason="owner requested a new branch (live re-run)", actor="owner")
            t = cur.execute("SELECT mission_id FROM tasks WHERE id = ?", (tid,)).fetchone()
            m = M.get_mission(cur, t["mission_id"])
            if m["status"] in ("completed", "blocked"):
                M.set_mission_status(cur, m["id"], "running", actor="owner", reason="new branch",
                                     allowed_from={"completed", "blocked"})
        ctx.notifier._on_commit()
        return views.task_detail(db, tid)

    # ---- memory, learning, initiative --------------------------------------------------------
    @app.get("/api/projects/{pid}/memory")
    async def memory(pid: str, q: str | None = None):
        if q:
            return ctx.memory.search(q, project_id=pid, agent_id=None, limit=30)
        return [dict(r) for r in db.query("SELECT * FROM memory_items WHERE (project_id = ? OR (project_id IS NULL AND"
                                          " access = 'owner')) AND access != 'private' ORDER BY created_at DESC LIMIT 200",
                                          (pid,))]

    @app.get("/api/skills")
    async def skills():
        return [{**dict(r), "eval": loads(r["eval_json"], {}), "content": loads(r["content_json"], {}),
                 "provenance": loads(r["provenance_json"], {})} for r in
                db.query("SELECT * FROM skill_versions ORDER BY skill_name, version")]

    @app.post("/api/skills/{sid}/promote")
    async def promote(sid: str):
        SkillRegistry(db).promote(sid, actor="owner")
        return {"ok": True}

    @app.post("/api/skills/{sid}/rollback")
    async def rollback(sid: str, body: ResolveIn):
        return {"restored": SkillRegistry(db).rollback(sid, actor="owner", reason=body.resolution)}

    @app.get("/api/projects/{pid}/opportunities")
    async def opportunities(pid: str, rescan: bool = False):
        if rescan:
            O.scan(db, pid)
        out = []
        for o in db.query("SELECT * FROM opportunities WHERE project_id = ? ORDER BY created_at DESC", (pid,)):
            ok, why = O.may_run_in_background(db, o)
            out.append({**dict(o), "background_allowed": ok, "background_reason": why})
        return out

    @app.post("/api/opportunities/{oid}/{action}")
    async def opportunity_action(oid: str, action: str):
        if action not in ("approve", "dismiss"):
            raise ValueError("action must be approve or dismiss")
        with db.tx() as cur:
            o = cur.execute("SELECT * FROM opportunities WHERE id = ?", (oid,)).fetchone()
            if o is None:
                raise KeyError(oid)
            cur.execute("UPDATE opportunities SET status = ? WHERE id = ?",
                        ("approved" if action == "approve" else "dismissed", oid))
            if action == "approve" and o["kind"] in ("unblocked_work", "stale_evidence") and o["mission_id"]:
                m = M.get_mission(cur, o["mission_id"])
                if m["status"] in ("paused", "completed", "blocked"):
                    M.set_mission_status(cur, m["id"], "running", actor="owner", reason=f"approved opportunity {oid}",
                                         allowed_from={"paused", "completed", "blocked"})
            emit(cur, f"opportunity.{action}d", "owner", {"opportunity_id": oid}, project_id=o["project_id"],
                 mission_id=o["mission_id"])
        ctx.notifier._on_commit()
        return {"ok": True}

    # ---- connected services & real actions ---------------------------------------------------
    S = ctx.services

    def connector_view(c):
        acts = []
        for a in db.query("SELECT * FROM connector_actions WHERE connector_id = ? ORDER BY name", (c["id"],)):
            d = dict(a)
            d["input_schema"] = loads(d.pop("input_schema_json"), {})
            d["hints"] = loads(d.pop("hints_json"), {})
            acts.append(d)
        return {"id": c["id"], "kind": c["kind"], "label": c["label"], "status": c["status"],
                "status_detail": c["status_detail"], "validated_at": c["validated_at"], "has_secret": bool(c["secret_ref"]),
                "config": loads(c["config_json"], {}), "actions": acts}

    @app.get("/api/projects/{pid}/connectors")
    async def list_connectors(pid: str):
        return [connector_view(c) for c in db.query("SELECT * FROM connectors WHERE project_id = ? AND status != 'removed'"
                                                    " ORDER BY created_at", (pid,))]

    @app.post("/api/projects/{pid}/connectors")
    async def add_connector(pid: str, body: ConnectorIn):
        if db.one("SELECT 1 FROM projects WHERE id = ?", (pid,)) is None:
            raise KeyError(pid)
        cid = S.connectors.add(project_id=pid, kind=body.kind, label=body.label, config=body.config, secret=body.secret)
        await S.connectors.discover(cid)
        return connector_view(db.one("SELECT * FROM connectors WHERE id = ?", (cid,)))

    @app.post("/api/connectors/{cid}/discover")
    async def rediscover(cid: str):
        return await S.connectors.discover(cid)

    @app.delete("/api/connectors/{cid}")
    async def remove_connector(cid: str):
        S.connectors.remove(cid)
        return {"ok": True}

    @app.put("/api/connector-actions/{aid}")
    async def action_policy(aid: str, body: ActionPolicyIn):
        from ..connectors.service import ActionPolicyError

        try:
            S.connectors.set_policy(aid, autonomy=body.autonomy, max_auto_per_day=body.max_auto_per_day,
                                    enabled=body.enabled, actor="owner")
        except ActionPolicyError as exc:
            raise ValueError(str(exc)) from None
        return {"ok": True}

    @app.get("/api/projects/{pid}/actions")
    async def list_actions(pid: str, status: str | None = None):
        q = "SELECT id FROM action_requests WHERE project_id = ?" + (" AND status = ?" if status else "")
        rows = db.query(q + " ORDER BY created_at DESC LIMIT 300", (pid, status) if status else (pid,))
        return [S.connectors.view(r["id"]) for r in rows]

    @app.post("/api/actions/{rid}/approve")
    async def approve_action(rid: str, body: NoteIn):
        return await S.connectors.decide(rid, approve=True, note=body.note, actor="owner")

    @app.post("/api/actions/{rid}/deny")
    async def deny_action(rid: str, body: NoteIn):
        return await S.connectors.decide(rid, approve=False, note=body.note, actor="owner")

    @app.post("/api/actions/{rid}/reconcile")
    async def reconcile_action(rid: str):
        return await S.connectors.reconcile(rid)

    @app.post("/api/actions/{rid}/resolve")
    async def resolve_action(rid: str, body: ResolveActionIn):
        return S.connectors.resolve_manually(rid, outcome=body.outcome, note=body.note, actor="owner")

    # ---- internet access & search -----------------------------------------------------------
    @app.get("/api/projects/{pid}/internet")
    async def get_internet(pid: str):
        from ..connectors.service import internet_policy

        if db.one("SELECT 1 FROM projects WHERE id = ?", (pid,)) is None:
            raise KeyError(pid)
        tools = sorted(ctx.broker.capability_bundles["internet"] | ctx.broker.capability_bundles["utilities"])
        return {**internet_policy(db, pid), "tools": tools,
                "guards": ["public addresses only (local network, loopback and cloud metadata blocked unless allowlisted)",
                           "connections pinned to the vetted address (DNS rebinding)",
                           "agents cannot attach credentials (Authorization/Cookie headers stripped)",
                           "size, time and rate limits", "fetched content is untrusted and recorded as evidence"]}

    @app.put("/api/projects/{pid}/internet")
    async def put_internet(pid: str, body: InternetIn):
        from ..connectors.service import apply_internet_policy, ensure_web_connector

        if body.mode not in ("off", "read", "read_write") or body.write_autonomy not in ("ask", "auto"):
            raise ValueError("mode must be off/read/read_write and write_autonomy ask/auto")
        if not 1 <= body.max_requests_per_minute <= 1000:
            raise ValueError("max_requests_per_minute must be 1-1000")
        clean = lambda xs: sorted({x.strip().lower() for x in xs if x.strip()})
        with db.tx() as cur:
            p = cur.execute("SELECT settings_json FROM projects WHERE id = ?", (pid,)).fetchone()
            if p is None:
                raise KeyError(pid)
            s = loads(p["settings_json"], {})
            s["internet"] = {"mode": body.mode, "write_autonomy": body.write_autonomy,
                             "blocked_domains": clean(body.blocked_domains),
                             "allowed_private_hosts": clean(body.allowed_private_hosts),
                             "max_requests_per_minute": body.max_requests_per_minute,
                             "max_auto_writes_per_day": body.max_auto_writes_per_day,
                             "contact": body.contact.strip()[:200]}
            cur.execute("UPDATE projects SET settings_json = ? WHERE id = ?", (dumps(s), pid))
            emit(cur, "internet.policy_changed", "owner", s["internet"], project_id=pid)
        await ensure_web_connector(S.connectors, pid)
        apply_internet_policy(S.connectors, pid)
        return await get_internet(pid)

    @app.get("/api/projects/{pid}/autonomy")
    async def get_autonomy(pid: str):
        from ..guard.action_guard import autonomy_settings

        if db.one("SELECT 1 FROM projects WHERE id = ?", (pid,)) is None:
            raise KeyError(pid)
        return {**autonomy_settings(db, pid), "checks": [
            "destination_in_scope: only hosts/emails you wrote in the mission, your messages or this allowlist, or services you connected",
            "not_exposed: an agent that read text addressed to AI agents loses unattended writes for that attempt",
            "payload_clean: no credential-like strings; bounded size",
            "within_limits: per-mission and per-hour caps",
            "monitor: Sentinel (no tools, never reads untrusted content) judges whether the action serves your mission",
            "destructive actions always ask; repeated denials suspend the task for your review"]}

    # ---- chat ---------------------------------------------------------------------------------
    @app.get("/api/chat/conversations")
    async def list_conversations():
        return [{**dict(r), "busy": r["id"] in chat.busy} for r in db.query(
            "SELECT * FROM conversations WHERE archived = 0 ORDER BY updated_at DESC LIMIT 200")]

    @app.post("/api/chat/conversations")
    async def new_conversation(body: ChatIn):
        conv = chat.create_conversation(body.title.strip()[:60] or "New chat")
        if body.text.strip():
            chat.submit(conv["id"], body.text)
        return {**conv, "busy": conv["id"] in chat.busy}

    @app.get("/api/chat/conversations/{cid}")
    async def get_conversation(cid: str):
        c = db.one("SELECT * FROM conversations WHERE id = ?", (cid,))
        if c is None:
            raise KeyError(cid)
        return {**dict(c), "busy": cid in chat.busy, "messages": chat.messages(cid)}

    @app.post("/api/chat/conversations/{cid}/messages")
    async def chat_send(cid: str, body: ChatIn):
        chat.submit(cid, body.text)
        return {"ok": True, "busy": True}

    @app.post("/api/chat/conversations/{cid}/archive")
    async def chat_archive(cid: str):
        with db.tx() as cur:
            cur.execute("UPDATE conversations SET archived = 1 WHERE id = ?", (cid,))
            emit(cur, "chat.conversation_archived", "owner", {"conversation_id": cid})
        return {"ok": True}

    @app.post("/api/chat/missions/{mid}/start")
    async def chat_start_mission(mid: str):
        await chat.start_proposed(mid)
        return views.mission_view(db, mid)

    @app.get("/api/autopilot")
    async def list_autopilot():
        return {"standing": chat.autopilot.list(), "spent_today_usd": round(chat.autopilot._spent_today(), 4),
                "daily_limit_usd": chat.autopilot.global_daily_limit()}

    @app.put("/api/autopilot/{sid}/limits")
    async def autopilot_limits(sid: str, body: AutopilotLimitsIn):
        return chat.autopilot.set_limits(sid, run_cap_usd=body.run_cap_usd, daily_cap_usd=body.daily_cap_usd)

    @app.post("/api/autopilot/{sid}/{op}")
    async def autopilot_control(sid: str, op: str):
        return chat.autopilot.control(sid, op)

    @app.get("/api/chat/status")
    async def chat_status():
        from ..chat.service import _limits

        return {"setup": chat.setup_needed(), "limits": _limits(db), "spent_today_usd": chat._chat_spend_today()}

    @app.get("/api/projects/{pid}/sandbox")
    async def get_sandbox(pid: str):
        from ..tools.sandbox import DEFAULT_PROFILE, project_profile

        if db.one("SELECT 1 FROM projects WHERE id = ?", (pid,)) is None:
            raise KeyError(pid)
        prof = project_profile(db, pid)
        ok, why = await ctx.sandbox.available(refresh=True, image=prof["image"])
        return {**prof, "default": DEFAULT_PROFILE, "available": ok, "detail": why,
                "fixed": ["no network", "worktree mounted read-only", "no new privileges", "process limit 512"]}

    @app.put("/api/projects/{pid}/sandbox")
    async def put_sandbox(pid: str, body: SandboxIn):
        from ..tools.sandbox import clean_profile

        from ..tools.sandbox import project_profile

        raw = body.model_dump()
        if raw.get("test_command") is None:  # keep a test command set by the repository's .nexus/sandbox.json
            raw["test_command"] = project_profile(db, pid).get("test_command")
        prof = clean_profile(raw)  # raises ValueError on a bad image name or test command
        with db.tx() as cur:
            p = cur.execute("SELECT settings_json FROM projects WHERE id = ?", (pid,)).fetchone()
            if p is None:
                raise KeyError(pid)
            s = loads(p["settings_json"], {})
            s["sandbox"] = prof
            cur.execute("UPDATE projects SET settings_json = ? WHERE id = ?", (dumps(s), pid))
            emit(cur, "sandbox.profile_changed", "owner", prof, project_id=pid)
        sup._sandbox_checked_at = 0.0  # re-check availability on the next tick
        return await get_sandbox(pid)

    @app.put("/api/projects/{pid}/autonomy")
    async def put_autonomy(pid: str, body: AutonomyIn):
        if body.mode not in ("supervised", "unattended"):
            raise ValueError("mode must be supervised or unattended")
        if not (1 <= body.max_writes_per_mission <= 1000 and 1 <= body.max_writes_per_hour <= 1000
                and 1 <= body.suspend_after_violations <= 20):
            raise ValueError("limits out of range")
        clean = lambda xs: sorted({x.strip().lower() for x in xs if x.strip()})
        with db.tx() as cur:
            p = cur.execute("SELECT settings_json FROM projects WHERE id = ?", (pid,)).fetchone()
            if p is None:
                raise KeyError(pid)
            s = loads(p["settings_json"], {})
            s["autonomy"] = {"mode": body.mode, "web_hosts": clean(body.web_hosts), "email_domains": clean(body.email_domains),
                             "max_writes_per_mission": body.max_writes_per_mission,
                             "max_writes_per_hour": body.max_writes_per_hour, "monitor": body.monitor,
                             "suspend_after_violations": body.suspend_after_violations}
            cur.execute("UPDATE projects SET settings_json = ? WHERE id = ?", (dumps(s), pid))
            emit(cur, "autonomy.changed", "owner", s["autonomy"], project_id=pid)
        return await get_autonomy(pid)

    @app.get("/api/projects/{pid}/digest")
    async def digest(pid: str, since: str | None = None):
        """While you were away: what agents did, what the guard stopped, what needs you."""
        since = since or "1970-01-01"
        acts = db.query("SELECT ar.*, c.label FROM action_requests ar JOIN connectors c ON c.id = ar.connector_id WHERE"
                        " ar.project_id = ? AND ar.created_at > ? ORDER BY ar.created_at DESC", (pid, since))
        guard = db.query("SELECT * FROM guard_decisions WHERE project_id = ? AND created_at > ? AND decision != 'allow'"
                         " ORDER BY created_at DESC", (pid, since))
        exposures = db.query("SELECT * FROM exposures WHERE project_id = ? AND created_at > ? ORDER BY created_at DESC",
                             (pid, since))
        suspended = db.query("SELECT * FROM events WHERE project_id = ? AND type = 'attention.task_suspended' AND created_at > ?"
                             " ORDER BY seq DESC", (pid, since))
        missions = db.query("SELECT id, title, status, updated_at FROM missions WHERE project_id = ? AND updated_at > ?"
                            " ORDER BY updated_at DESC", (pid, since))
        spend = db.one("SELECT COALESCE(SUM(actual_usd),0) AS usd FROM model_calls WHERE project_id = ? AND started_at > ?",
                       (pid, since))["usd"]
        def act_row(a):
            return {"id": a["id"], "when": a["created_at"], "what": f"{a['label']}: {a['action']}", "status": a["status"],
                    "approval": a["approval"], "note": a["decision_note"], "mission_id": a["mission_id"],
                    "target": (loads(a["args_json"], {}).get("url") or loads(a["args_json"], {}).get("to"))}
        return {"since": since,
                "actions_done": [act_row(a) for a in acts if a["status"] == "succeeded"],
                "actions_blocked": [act_row(a) for a in acts if a["status"] == "denied"],
                "actions_waiting": [act_row(a) for a in acts if a["status"] in ("pending", "uncertain")],
                "guard": [{"when": g["created_at"], "decision": g["decision"], "task_id": g["task_id"],
                           "failed": [c for c in loads(g["checks_json"], []) if not c["passed"]]} for g in guard],
                "exposures": [{"when": e["created_at"], "source": e["source"], "kinds": [m["kind"] for m in loads(e["matches_json"], [])]}
                              for e in exposures],
                "suspended_tasks": [{"when": s["created_at"], "task_id": s["task_id"], "mission_id": s["mission_id"],
                                     **loads(s["payload_json"], {})} for s in suspended],
                "missions": [dict(m) for m in missions], "model_spend_usd": spend}

    @app.get("/api/search-provider")
    async def get_search():
        r = db.one("SELECT value_json FROM settings WHERE key = 'search.provider'")
        cfg = loads(r["value_json"], {}) if r else {}
        return {"kind": cfg.get("kind", "duckduckgo"), "base_url": cfg.get("base_url"), "has_key": bool(cfg.get("secret_ref")),
                "options": ["duckduckgo (no key)", "brave (API key)", "tavily (API key)", "searxng (your instance URL)"]}

    @app.put("/api/search-provider")
    async def put_search(body: SearchIn):
        if body.kind not in ("duckduckgo", "brave", "tavily", "searxng"):
            raise ValueError("unknown search provider")
        if body.kind == "searxng" and not (body.base_url or "").startswith(("http://", "https://")):
            raise ValueError("searxng needs its base URL")
        r = db.one("SELECT value_json FROM settings WHERE key = 'search.provider'")
        old = loads(r["value_json"], {}) if r else {}
        ref = old.get("secret_ref")
        if body.api_key:
            ref = ctx.secrets.put(body.api_key)
            ctx.secrets.delete(old.get("secret_ref"))
        if body.kind in ("brave", "tavily") and not ref:
            raise ValueError(f"{body.kind} needs an API key")
        with db.tx() as cur:
            cur.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES ('search.provider', ?, ?)",
                        (dumps({"kind": body.kind, "base_url": body.base_url, "secret_ref": ref}), now_iso()))
            emit(cur, "settings.changed", "owner", {"key": "search.provider", "kind": body.kind})
        return await get_search()

    # ---- goals, objectives, tools ----------------------------------------------------------
    @app.get("/api/projects/{pid}/goals")
    async def get_goals(pid: str):
        return S.objectives.goals(pid)

    @app.put("/api/projects/{pid}/goals")
    async def put_goals(pid: str, body: GoalsIn):
        return S.objectives.set_goals(pid, body.goals, actor="owner")

    @app.post("/api/projects/{pid}/discover")
    async def discover(pid: str, body: DiscoverIn):
        mid = S.objectives.start_discovery(pid, execution_mode=body.execution_mode, spend_cap_usd=body.spend_cap_usd,
                                           focus=body.focus)
        ctx.notifier._on_commit()
        return views.mission_view(db, mid)

    @app.get("/api/projects/{pid}/objectives")
    async def objectives(pid: str):
        out = []
        for o in db.query("SELECT * FROM objective_proposals WHERE project_id = ? ORDER BY created_at DESC", (pid,)):
            d = dict(o)
            d["success_conditions"] = loads(d.pop("success_conditions_json"), [])
            d["evidence"] = loads(d.pop("evidence_json"), [])
            out.append(d)
        return out

    @app.post("/api/objectives/{oid}/adopt")
    async def adopt(oid: str, body: AdoptIn):
        if body.execution_mode == "live" and not body.spend_cap_usd:
            raise ValueError("live missions need a spend cap")
        mid = S.objectives.adopt(oid, actor="owner", execution_mode=body.execution_mode, spend_cap_usd=body.spend_cap_usd,
                                 note=body.note)
        ctx.notifier._on_commit()
        return {"mission_id": mid}

    @app.post("/api/objectives/{oid}/reject")
    async def reject_objective(oid: str, body: NoteIn):
        S.objectives.reject(oid, note=body.note, actor="owner")
        return {"ok": True}

    @app.get("/api/projects/{pid}/tools")
    async def tool_defs(pid: str):
        out = []
        for t in db.query("SELECT * FROM tool_definitions WHERE project_id = ? ORDER BY name, version DESC", (pid,)):
            d = dict(t)
            for k in ("spec", "tests", "input_schema", "test_report"):
                d[k] = loads(d.pop(f"{k}_json"), {})
            out.append(d)
        return out

    @app.post("/api/tools/{tid}/approve")
    async def approve_tool(tid: str):
        S.toolsmith.approve(tid, actor="owner")
        return {"ok": True}

    @app.post("/api/tools/{tid}/retire")
    async def retire_tool(tid: str):
        S.toolsmith.retire(tid, actor="owner")
        return {"ok": True}

    @app.post("/api/tools/{tid}/retest")
    async def retest_tool(tid: str):
        from ..tools.broker import ToolContext
        from ..tools.builtin import expand_capabilities

        t = db.one("SELECT * FROM tool_definitions WHERE id = ?", (tid,))
        if t is None:
            raise KeyError(tid)
        if t["status"] not in ("candidate", "test_failed", "tested"):
            raise ValueError(f"tool is {t['status']}")
        agent_id = t["created_by"].removeprefix("agent:")
        v = db.one("SELECT v.tool_permissions_json FROM agent_identities a JOIN agent_versions v ON v.id ="
                   " a.current_version_id WHERE a.id = ?", (agent_id,))
        proj = db.one("SELECT workspace_root FROM projects WHERE id = ?", (t["project_id"],))
        tctx = ToolContext(project_id=t["project_id"], workspace_root=Path(proj["workspace_root"]), agent_id=agent_id,
                           allowed_tools=expand_capabilities(ctx.broker, loads(v["tool_permissions_json"], []) if v else []))
        return await S.toolsmith.run_tests(tid, tctx)

    # ---- backup ----------------------------------------------------------------------------
    @app.post("/api/backup")
    async def backup(body: SettingIn):
        from ..backup import create_backup

        return create_backup(ctx, Path(str(body.value)))

    app.state.supervisor = sup
    app.state.ctx = ctx
    return app
