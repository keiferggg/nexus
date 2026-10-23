"""Task worker: compiles bounded context, runs the model/tool loop through the gateway and
broker, and turns the agent's proposal into an immutable artifact + WorkResult.

Operation ids are deterministic per (attempt, round, step), so a replay after a crash reuses
recorded model outputs and tool results instead of paying for or re-executing them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ..appctx import AppContext
from ..domain import rooms
from ..domain.execution import check_fence
from ..domain.missions import current_revision
from ..domain.understanding import ObjectionInvalid, add_claim, add_objection, add_uncertainty
from ..memory.context import compile_packet
from ..persistence.db import dumps, loads
from ..providers.base import ChatMessage, CompletionRequest, ProviderError, parse_json_output
from ..tools.broker import ToolContext
from ..tools.builtin import expand_capabilities
from . import roles
from .adapter import WorkResult


class CapabilityMissing(Exception):
    pass


class ToolLoopExceeded(Exception):
    pass


DEFAULT_OUTPUT_TOKENS = 8192
# Tool rounds per task kind before the final no-tools answer. Code work in a real repository needs room to explore,
# edit and run tests; planning should stay brief (details belong to the tasks it creates).
TOOL_ROUNDS = {"plan": 8, "code": 30, "research": 16, "write": 12, "act": 12, "discover": 12, "check": 12, "integrate": 6}
TOOL_RESULT_CHARS = 12000      # one tool result, as sent to the model
TOOL_CONTEXT_CHARS = 40000     # all tool results together; older ones are elided beyond this
KEEP_RECENT_TOOL_RESULTS = 3


def compact_tool_results(messages: list[ChatMessage]) -> int:
    """Every model call re-sends the whole conversation, so tool results accumulate: seen live, an engineer reading a
    large repository reached 67k input tokens (~$0.09) per call and spent $1.78 before writing anything. Older tool
    results beyond the budget are cut to a short preview with a note that the tool can be called again; the most
    recent ones stay whole. Returns the number of characters removed."""
    tool_ix = [i for i, m in enumerate(messages) if m.role == "tool"]
    total = sum(len(messages[i].content) for i in tool_ix)
    removed = 0
    for i in tool_ix[:-KEEP_RECENT_TOOL_RESULTS]:
        if total <= TOOL_CONTEXT_CHARS:
            break
        c = messages[i].content
        if len(c) <= 600 or c.endswith("[elided]"):
            continue
        stub = c[:400] + f"\n… [{len(c) - 400} more characters not repeated to keep context small; call the tool again" \
                         " if you need them] [elided]"
        messages[i] = ChatMessage("tool", stub, tool_call_id=messages[i].tool_call_id, tool_name=messages[i].tool_name)
        total -= len(c) - len(stub)
        removed += len(c) - len(stub)
    return removed
LEGACY_OUTPUT_DEFAULT = 2048


def resolve_policy(ctx: AppContext, mission, agent) -> dict[str, Any]:
    if mission["execution_mode"] == "offline_fake":
        fake = ctx.db.one("SELECT id FROM providers WHERE kind = 'fake' ORDER BY created_at LIMIT 1")
        if fake is None:
            raise ProviderError("not_configured", "offline fake provider is not registered")
        return {"provider_id": fake["id"], "model": "fake-deterministic-1", "max_output_tokens": 2048}
    pol = loads(agent["model_policy_json"], {}) if agent is not None else {}
    if not pol.get("provider_id") or not pol.get("model"):
        d = ctx.db.one("SELECT value_json FROM settings WHERE key = 'default_model_policy'")
        dflt = loads(d["value_json"], {}) if d else {}
        pol = {**dflt, **{k: v for k, v in pol.items() if v}}
    if not pol.get("provider_id") or not pol.get("model"):
        raise ProviderError("not_configured", f"no model configured for role '{agent['role_default'] if agent else '?'}';"
                                              " choose one in Agent studio or Settings > Default model")
    if int(pol.get("max_output_tokens") or 0) in (0, LEGACY_OUTPUT_DEFAULT):
        pol["max_output_tokens"] = DEFAULT_OUTPUT_TOKENS  # 2048 starved reasoning models; upgrade the old default
    return pol


class TaskWorker:
    def __init__(self, ctx: AppContext, *, task_id: str, attempt: dict[str, Any], max_tool_rounds: int = 8):
        self.ctx = ctx
        self.task_id = task_id
        self.attempt = attempt
        self.max_tool_rounds = max_tool_rounds

    # ---- helpers ---------------------------------------------------------------------------
    def _load(self):
        db = self.ctx.db
        task = db.one("SELECT * FROM tasks WHERE id = ?", (self.task_id,))
        mission = db.one("SELECT * FROM missions WHERE id = ?", (task["mission_id"],))
        project = db.one("SELECT * FROM projects WHERE id = ?", (task["project_id"],))
        agent = db.one(
            "SELECT a.*, v.role_default, v.model_policy_json, v.tool_permissions_json, v.memory_scope, v.version AS"
            " agent_version FROM agent_identities a JOIN agent_versions v ON v.id = a.current_version_id WHERE a.id = ?",
            (task["agent_id"],)) if task["agent_id"] else None
        return task, mission, project, agent

    def _fence(self) -> None:
        with self.ctx.db.tx() as cur:
            check_fence(cur, self.task_id, self.attempt["attempt_id"], self.attempt["fencing_token"])

    def _inputs(self, task) -> list[dict[str, Any]]:
        rows = self.ctx.db.query(
            "SELECT t.id, t.key, t.title, t.current_artifact_id, a.sha256, s.meta_json FROM dependencies d"
            " JOIN tasks t ON t.id = d.depends_on_task_id JOIN artifacts a ON a.id = t.current_artifact_id"
            " LEFT JOIN acceptances acc ON acc.task_id = t.id AND acc.is_current = 1"
            " LEFT JOIN submissions s ON s.id = acc.submission_id"
            " WHERE d.task_id = ? AND t.status = 'accepted' AND t.is_stale = 0", (task["id"],))
        out = []
        for r in rows:
            out.append({"task_id": r["id"], "task_key": r["key"], "title": r["title"], "artifact_id": r["current_artifact_id"],
                        "sha256": r["sha256"], "meta": loads(r["meta_json"], {}),
                        "content": self.ctx.artifacts.read_text(r["current_artifact_id"])})
        return out

    def _previous_failures(self, task, feedback: list) -> list[dict[str, Any]]:
        out = [{"source": "this attempt, previous round", **f} for f in feedback]
        rows = self.ctx.db.query(
            "SELECT r.criterion, r.findings_json, s.round, a.attempt_no FROM reviews r JOIN submissions s ON s.id = r.submission_id"
            " JOIN task_attempts a ON a.id = s.attempt_id WHERE r.task_id = ? AND r.verdict != 'pass' AND a.id != ?"
            " ORDER BY r.created_at DESC LIMIT 8", (task["id"], self.attempt["attempt_id"]))
        for r in rows:
            f = loads(r["findings_json"], {})
            out.append({"source": f"attempt {r['attempt_no']} round {r['round']}", "criterion": r["criterion"],
                        "summary": f.get("summary")})
        for a in self.ctx.db.query("SELECT attempt_no, error FROM task_attempts WHERE task_id = ? AND error IS NOT NULL"
                                   " AND id != ? ORDER BY attempt_no DESC LIMIT 3", (task["id"], self.attempt["attempt_id"])):
            out.append({"source": f"attempt {a['attempt_no']}", "summary": a["error"][:300]})
        return out

    def _sections(self, task, mission, project, agent, feedback: list, contract: str) -> dict[str, Any]:
        with self.ctx.db.tx() as cur:
            rev = current_revision(cur, mission["id"])
            keys = [f"agent:{agent['id']}", f"role:{agent['role_default']}"] if agent else []
            msgs = rooms.pending_for(cur, recipient_keys=keys, mission_id=mission["id"])
            rooms.mark_delivered(cur, [m["id"] for m in msgs], keys)
        inputs = self._inputs(task)
        evidence = [{"id": e["id"], "title": e["title"], "lineage_root": e["lineage_root"], "locator": e["locator"]}
                    for e in self.ctx.db.query("SELECT * FROM evidence WHERE mission_id = ? AND status = 'active'"
                                               " ORDER BY created_at", (mission["id"],))]
        disagreements = [{"id": o["id"], "summary": o["summary"], "mechanism": o["mechanism"],
                          "resolution_condition": o["resolution_condition"], "blocking": bool(o["blocking"])}
                         for o in self.ctx.db.query("SELECT * FROM proposals WHERE mission_id = ? AND status = 'open'"
                                                    " AND (target_ref = ? OR target_ref LIKE 'mission:%')",
                                                    (mission["id"], f"task:{task['id']}"))]
        disagreements += [{"claim_id": c["id"], "proposition": c["proposition"], "status": "disputed"}
                          for c in self.ctx.db.query("SELECT * FROM claims WHERE mission_id = ? AND status = 'disputed'",
                                                     (mission["id"],))]
        override = self.ctx.db.one("SELECT value_json FROM settings WHERE key = ?", (f"mission:{mission['id']}:skills",))
        if override is not None:  # evaluation runs: exactly the candidate or the frozen baseline
            ids = loads(override["value_json"], {}).get("only", [])
            rows = [self.ctx.db.one("SELECT * FROM skill_versions WHERE id = ?", (i,)) for i in ids]
        else:
            rows = self.ctx.db.query("SELECT * FROM skill_versions WHERE status IN ('promoted','canary')"
                                     " AND target = 'procedure'")
        skills = [{"skill": s["skill_name"], "version": s["version"], "content": loads(s["content_json"], {}),
                   "conditions": s["conditions"]} for s in rows if s is not None
                  and _skill_applies(loads(s["content_json"], {}), task)]
        memory = self.ctx.memory.search(f"{task['title']} {task['description']}", project_id=project["id"],
                                        agent_id=agent["id"] if agent else None, limit=5)
        root = Path(project["workspace_root"])
        sources = sorted(str(p.relative_to(root)).replace("\\", "/") for p in (root / "sources").glob("**/*")
                         if p.is_file()) if (root / "sources").is_dir() else []
        return {
            "role": agent["role_default"] if agent else task["role"],
            "goal": {"objective": rev["objective"], "success_conditions": rev["success_conditions"],
                     "standing_goals": loads(project["settings_json"], {}).get("goals", [])},
            "mission_revision": rev["revision"],
            "task": {"key": task["key"], "title": task["title"], "kind": task["kind"], "description": task["description"],
                     "acceptance": loads(task["acceptance_json"], []), "strategy": loads(task["strategy_json"], {})},
            "hard_constraints": rev["constraints"],
            "required_output": contract,
            "input_versions": [{"task_key": i["task_key"], "artifact_id": i["artifact_id"], "sha256": i["sha256"]}
                               for i in inputs],
            "previous_failures": self._previous_failures(task, feedback),
            "disagreements": disagreements,
            "inputs": [{"id": i["artifact_id"], "task_key": i["task_key"], "title": i["title"], "content": i["content"]}
                       for i in inputs],
            "evidence": evidence,
            "skills": skills,
            "memory": memory,
            "workspace": [{"sources": sources}],
            "room_messages": msgs,
        }

    async def _call(self, req: CompletionRequest, *, op: str, purpose: str, policy, task, agent,
                    context_artifact_id: str | None, category: str = "model"):
        for k in range(4):
            opk = op if k == 0 else f"{op}#retry{k}"
            try:
                return await self.ctx.gateway.call(
                    req, operation_id=opk, provider_id=policy["provider_id"], purpose=purpose, category=category,
                    project_id=task["project_id"], mission_id=task["mission_id"], task_id=task["id"],
                    attempt_id=self.attempt["attempt_id"], agent_id=agent["id"] if agent else None,
                    context_artifact_id=context_artifact_id)
            except ProviderError as exc:
                if exc.kind == "duplicate_operation":
                    continue  # replay: this retry slot was already consumed
                if exc.kind == "rate_limit" and k < 3:
                    await asyncio.sleep(min(20.0, exc.retry_after or 2.0 ** k))
                    continue
                if exc.kind in ("server", "timeout", "network") and k < 1:
                    await asyncio.sleep(1.0)
                    continue
                raise
        raise ProviderError("rate_limit", "retry allowance exhausted")

    async def _loop(self, *, task, mission, project, agent, policy, system: str, packet_text: str, instruction: str,
                    tool_ctx: ToolContext | None, round: int, step: str, context_artifact_id: str,
                    category: str = "model") -> dict[str, Any]:
        tools = self.ctx.broker.specs_for(tool_ctx) if tool_ctx else []
        messages = [ChatMessage("user", f"{packet_text}\n\n{instruction}")]
        base = f"{self.attempt['attempt_id']}:r{round}:{step}"
        budget = self.max_tool_rounds if self.max_tool_rounds != 8 else TOOL_ROUNDS.get(task["kind"], 8)
        for i in range(budget + 1):
            self._fence()
            # Out of exploration budget: one last turn with tools disabled to answer from what was learned. Seen live:
            # a planner exploring a large repository was abandoned three times after 8 rounds without ever answering.
            final = bool(tools) and i == budget
            if final:
                messages.append(ChatMessage("user", "You have used your tool budget. Do not call any more tools: return"
                                                    " the required JSON now, based on what you have already learned."))
            req = CompletionRequest(model=policy["model"], system=system, messages=list(messages),
                                    max_output_tokens=int(policy.get("max_output_tokens", DEFAULT_OUTPUT_TOKENS)), tools=tools,
                                    tool_choice="none" if final else None,
                                    response_json=True, metadata={"purpose": step, "role": agent["role_default"] if agent else None,
                                                                  "round": round, "task_kind": task["kind"]})
            out = await self._call(req, op=f"{base}:{i}", purpose=step, policy=policy, task=task, agent=agent,
                                   context_artifact_id=context_artifact_id, category=category)
            res = out.result
            if res.tool_calls and not final:
                messages.append(ChatMessage("assistant", res.text, tool_calls=res.tool_calls, reasoning=res.reasoning))
                for j, tc in enumerate(res.tool_calls):
                    r = await self.ctx.broker.invoke(tool_ctx, tc.name, tc.arguments, operation_id=f"{base}:{i}:tool:{j}") \
                        if tool_ctx else {"ok": False, "error": "no tools available"}
                    messages.append(ChatMessage("tool", json.dumps(r)[:TOOL_RESULT_CHARS], tool_call_id=tc.id,
                                                tool_name=tc.name))
                compact_tool_results(messages)
                continue
            try:
                return parse_json_output(res.text)
            except ProviderError:
                if res.text.strip():
                    messages.append(ChatMessage("assistant", res.text))
                messages.append(ChatMessage("user", "Your previous reply was not the required JSON object. Reply again"
                                                    " with ONLY the JSON object described in REQUIRED OUTPUT."))
                req2 = CompletionRequest(model=policy["model"], system=system, messages=list(messages),
                                         max_output_tokens=int(policy.get("max_output_tokens", DEFAULT_OUTPUT_TOKENS)),
                                         tools=tools, tool_choice="none" if tools else None,
                                         response_json=True, metadata={**req.metadata, "purpose": f"{step}:repair"})
                out2 = await self._call(req2, op=f"{base}:{i}:repair", purpose=f"{step}:repair", policy=policy, task=task,
                                        agent=agent, context_artifact_id=context_artifact_id, category=category)
                return parse_json_output(out2.result.text)  # raises malformed_output if still invalid
        raise ToolLoopExceeded(f"tool loop exceeded {self.max_tool_rounds} rounds")

    def _tool_ctx(self, project, task, agent, worktree: Path | None = None) -> ToolContext:
        settings = loads(project["settings_json"], {})
        caps = loads(agent["tool_permissions_json"], []) if agent else []
        return ToolContext(project_id=project["id"], workspace_root=Path(project["workspace_root"]),
                           agent_id=agent["id"] if agent else "nexus:unknown",
                           allowed_tools=expand_capabilities(self.ctx.broker, caps), mission_id=task["mission_id"],
                           task_id=task["id"], attempt_id=self.attempt["attempt_id"], worktree=worktree,
                           fetch_allowlist=settings.get("fetch_allowlist", []),
                           extra={"external_write_hosts": settings.get("external_write_hosts", []),
                                  "internet": settings.get("internet") or {}})

    def _store_packet(self, task, sections: dict[str, Any], round: int) -> tuple[str, str]:
        packet = compile_packet(sections)
        text = packet.render()
        with self.ctx.db.tx() as cur:
            art = self.ctx.artifacts.create(cur, project_id=task["project_id"], kind="context_packet",
                                            title=f"context {task['key']} r{round}", content=text,
                                            media_type="application/json", created_by="nexus:context-compiler",
                                            mission_id=task["mission_id"], task_id=task["id"], access="project",
                                            lineage={"omitted": packet.omitted, "chars": packet.chars})
        return text, art["id"]

    # ---- entry point -----------------------------------------------------------------------
    async def produce(self, round: int, feedback: list) -> WorkResult:
        self._fence()
        task, mission, project, agent = self._load()
        policy = resolve_policy(self.ctx, mission, agent)
        kind = task["kind"]
        if kind == "plan":
            return await self._plan(task, mission, project, agent, policy, round, feedback)
        if kind == "code":
            return await self._code(task, mission, project, agent, policy, round, feedback)
        if kind == "integrate":
            return await self._integrate(task, mission, project, agent, policy, round, feedback)
        return await self._document(task, mission, project, agent, policy, round, feedback)

    async def _plan(self, task, mission, project, agent, policy, round, feedback) -> WorkResult:
        sections = self._sections(task, mission, project, agent, feedback, "plan")
        text, ctx_id = self._store_packet(task, sections, round)
        system = roles.system_prompt("planner", agent["charter"] if agent else "", roles.PLAN_OUTPUT)
        plan = await self._loop(task=task, mission=mission, project=project, agent=agent, policy=policy, system=system,
                                packet_text=text, instruction="Produce the plan now. Explore only as much as you need to plan"
                                " (a few tool calls at most); detailed reading and changes belong to the tasks you create.",
                                tool_ctx=self._tool_ctx(project, task, agent), round=round, step="plan",
                                context_artifact_id=ctx_id)
        with self.ctx.db.tx() as cur:
            art = self.ctx.artifacts.create(cur, project_id=task["project_id"], kind="plan", title=f"Plan ({task['key']})",
                                            content=json.dumps(plan, indent=2), media_type="application/json",
                                            created_by=agent["id"] if agent else "agent", mission_id=task["mission_id"],
                                            task_id=task["id"], mission_revision=sections["mission_revision"])
        return WorkResult(round, art["id"], plan.get("rationale", "plan proposed")[:500],
                          {"kind": "plan", "task_count": len(plan.get("tasks", []))})

    def _record_proposals(self, task, agent, data: dict[str, Any], round: int) -> dict[str, Any]:
        """Agent-proposed claims/uncertainties/messages, validated before commit."""
        out = {"claims": [], "rejected": []}
        sender = f"agent:{agent['id']}" if agent else "agent:unknown"
        with self.ctx.db.tx() as cur:
            for c in data.get("claims", []) or []:
                if isinstance(c, dict) and isinstance(c.get("proposition"), str):
                    cid = add_claim(cur, project_id=task["project_id"], mission_id=task["mission_id"],
                                    proposition=c["proposition"], supports=[e for e in c.get("evidence_ids", [])
                                                                            if isinstance(e, str)], created_by=sender)
                    out["claims"].append(cid)
            for u in data.get("uncertainties", []) or []:
                if isinstance(u, dict) and u.get("kind") in ("missing_evidence", "conflicting_evidence", "ambiguous_intent",
                                                             "execution", "explanation") and u.get("question"):
                    add_uncertainty(cur, mission_id=task["mission_id"], kind=u["kind"], question=u["question"],
                                    would_change=u.get("would_change") or "unspecified", affects=[task["key"]],
                                    created_by=sender)
            for k, m in enumerate(data.get("messages", []) or []):
                if not isinstance(m, dict) or m.get("type") not in rooms.MESSAGE_TYPES:
                    continue
                mid, _ = rooms.post(cur, operation_id=f"{self.attempt['attempt_id']}:r{round}:msg:{k}",
                                    project_id=task["project_id"], mission_id=task["mission_id"],
                                    thread_id=rooms.thread_for_task(task["id"]), sender=sender,
                                    recipients=[f"role:{r}" for r in m.get("to", []) if isinstance(r, str)] or ["owner"],
                                    type=m["type"], body=str(m.get("body", ""))[:4000], task_id=task["id"])
                if m["type"] == "objection":
                    try:
                        add_objection(cur, mission_id=task["mission_id"], target_ref=m.get("target") or f"task:{task['id']}",
                                      summary=str(m.get("body", ""))[:500], mechanism=m.get("mechanism", ""),
                                      evidence_or_test=m.get("evidence_or_test", ""),
                                      resolution_condition=m.get("resolution_condition", ""), decision_owner="owner",
                                      blocking=False, created_by=sender, message_id=mid)
                    except ObjectionInvalid as exc:
                        out["rejected"].append(str(exc))
            for mid in data.get("acknowledged_message_ids", []) or []:
                if isinstance(mid, str) and agent:
                    rooms.acknowledge(cur, mid, f"agent:{agent['id']}") or rooms.acknowledge(
                        cur, mid, f"role:{agent['role_default']}")
        return out

    async def _document(self, task, mission, project, agent, policy, round, feedback) -> WorkResult:
        contract = {"act": roles.ACT_OUTPUT, "discover": roles.DISCOVER_OUTPUT}.get(task["kind"], roles.DOC_OUTPUT)
        sections = self._sections(task, mission, project, agent, feedback, task["kind"] if task["kind"] in ("act", "discover")
                                  else "document")
        text, ctx_id = self._store_packet(task, sections, round)
        system = roles.system_prompt(agent["role_default"] if agent else task["role"], agent["charter"] if agent else "",
                                     contract)
        data = await self._loop(task=task, mission=mission, project=project, agent=agent, policy=policy, system=system,
                                packet_text=text, instruction="Complete the task now.",
                                tool_ctx=self._tool_ctx(project, task, agent), round=round, step="execute",
                                context_artifact_id=ctx_id)
        content = data.get("content_markdown")
        if not isinstance(content, str):
            raise ProviderError("malformed_output", "output missing content_markdown string")
        props = self._record_proposals(task, agent, data, round)
        proposed = []
        for o in data.get("objectives", []) or []:
            if isinstance(o, dict) and self.ctx.services is not None:
                r = self.ctx.services.objectives.propose(
                    project_id=task["project_id"], proposed_by=f"agent:{agent['id'] if agent else 'unknown'}",
                    title=str(o.get("title", "")), objective=str(o.get("objective", "")),
                    success_conditions=list(o.get("success_conditions", []) or []), serves_goal=str(o.get("serves_goal", "")),
                    rationale=str(o.get("rationale", "")), evidence=list(o.get("evidence_ids", []) or []),
                    est_cost_usd=float(o.get("est_cost_usd", 0) or 0), source_mission_id=task["mission_id"],
                    source_task_id=task["id"])
                proposed.append({"title": o.get("title"), **{k: r.get(k) for k in ("status", "objective_id", "problems")}})
        with self.ctx.db.tx() as cur:
            art = self.ctx.artifacts.create(cur, project_id=task["project_id"], kind="deliverable", title=task["title"],
                                            content=content, created_by=agent["id"] if agent else "agent",
                                            mission_id=task["mission_id"], task_id=task["id"],
                                            mission_revision=sections["mission_revision"],
                                            input_versions={i["task_key"]: i["sha256"] for i in sections["input_versions"]},
                                            lineage={"context_packet": ctx_id})
        return WorkResult(round, art["id"], str(data.get("summary", ""))[:500],
                          {"kind": "document", "claims": props["claims"], "rejected_proposals": props["rejected"],
                           "objectives": proposed,
                           "inputs": [{k: i[k] for k in ("task_key", "artifact_id", "sha256")} for i in sections["input_versions"]]})

    async def _ensure_repo(self, project) -> Path:
        repo = Path(project["workspace_root"])
        if not await self.ctx.worktrees.is_repo(repo):
            raise CapabilityMissing(f"workspace {repo} is not a git repository; coding tasks need one")
        return repo

    async def _code(self, task, mission, project, agent, policy, round, feedback) -> WorkResult:
        repo = await self._ensure_repo(project)
        strat = loads(task["strategy_json"], {})
        base = strat.get("base_commit") or await self.ctx.worktrees.head(repo)
        wt = await self.ctx.worktrees.create(repo, f"{task['key']}-{task['id'][-6:]}", base)
        # Code that depends on other code starts from their accepted work, not from the mission's base commit.
        # Seen live: a "run the full suite" task planned after an "implement" task would otherwise have tested the
        # unchanged code and passed.
        from ..tools.worktree import git

        upstream = [i for i in self._inputs(task) if i["meta"].get("kind") in ("code", "integrate_code") and i["meta"].get("head")]
        for i in upstream:
            m = await git(wt["path"], "merge", "--no-ff", "--no-edit", "-m", f"NEXUS: include accepted {i['task_key']}",
                          i["meta"]["head"], check=False)
            if m.returncode != 0:
                await git(wt["path"], "merge", "--abort", check=False)
                raise ProviderError("bad_request", f"accepted work from {i['task_key']} does not merge cleanly into this"
                                                   f" task's worktree: {(m.stdout + m.stderr)[-300:]}")
        own_base = await self.ctx.worktrees.head(wt["path"])  # this task's changes are measured from here
        sections = self._sections(task, mission, project, agent, feedback, "code")
        sections["workspace"] = [{"worktree_branch": wt["branch"], "base_commit": base,
                                  "includes_accepted_work_from": [i["task_key"] for i in upstream]}]
        text, ctx_id = self._store_packet(task, sections, round)
        system = roles.system_prompt("engineer", agent["charter"] if agent else "", roles.CODE_OUTPUT)
        data = await self._loop(task=task, mission=mission, project=project, agent=agent, policy=policy, system=system,
                                packet_text=text, instruction="Make the change now.",
                                tool_ctx=self._tool_ctx(project, task, agent, wt["path"]), round=round, step="execute",
                                context_artifact_id=ctx_id)
        self._fence()
        head = await self.ctx.worktrees.commit_all(wt["path"], f"NEXUS {task['key']} round {round}") or \
            await self.ctx.worktrees.head(wt["path"])
        diff = await self.ctx.worktrees.diff(wt["path"], own_base)
        changed = await self.ctx.worktrees.changed_files(wt["path"], own_base)
        with self.ctx.db.tx() as cur:
            art = self.ctx.artifacts.create(cur, project_id=task["project_id"], kind="diff", title=task["title"],
                                            content=diff or "(no changes)", media_type="text/x-diff",
                                            created_by=agent["id"] if agent else "agent", mission_id=task["mission_id"],
                                            task_id=task["id"], mission_revision=sections["mission_revision"],
                                            input_versions={"base_commit": base}, lineage={"branch": wt["branch"], "head": head})
        return WorkResult(round, art["id"], str(data.get("summary", ""))[:500],
                          {"kind": "code", "branch": wt["branch"], "head": head, "base": base, "changed_files": changed,
                           "worktree": str(wt["path"]), "test_command": data.get("test_command")})

    async def _integrate(self, task, mission, project, agent, policy, round, feedback) -> WorkResult:
        inputs = self._inputs(task)
        code_inputs = [i for i in inputs if i["meta"].get("kind") == "code"]
        if not code_inputs:
            sections = self._sections(task, mission, project, agent, feedback, "integrate")
            text, ctx_id = self._store_packet(task, sections, round)
            system = roles.system_prompt("integrator", agent["charter"] if agent else "", roles.INTEGRATE_DOC_OUTPUT)
            data = await self._loop(task=task, mission=mission, project=project, agent=agent, policy=policy,
                                    system=system, packet_text=text, instruction="Integrate now.", tool_ctx=None,
                                    round=round, step="integrate", context_artifact_id=ctx_id)
            content = data.get("content_markdown")
            if not isinstance(content, str):
                raise ProviderError("malformed_output", "output missing content_markdown string")
            with self.ctx.db.tx() as cur:
                art = self.ctx.artifacts.create(cur, project_id=task["project_id"], kind="deliverable", title=task["title"],
                                                content=content, created_by=agent["id"] if agent else "agent",
                                                mission_id=task["mission_id"], task_id=task["id"],
                                                mission_revision=sections["mission_revision"],
                                                input_versions={i["task_key"]: i["sha256"] for i in inputs})
            return WorkResult(round, art["id"], str(data.get("summary", ""))[:500],
                              {"kind": "integrate_doc", "inputs": [{k: i[k] for k in ("task_key", "artifact_id", "sha256", "title")}
                                                                   for i in inputs]})
        return await self._integrate_code(task, mission, project, agent, policy, round, feedback, inputs, code_inputs)

    async def _integrate_code(self, task, mission, project, agent, policy, round, feedback, inputs, code_inputs):
        repo = await self._ensure_repo(project)
        base = code_inputs[0]["meta"]["base"]
        name = f"{task['key']}-{task['id'][-6:]}-r{round}"
        res = await self.ctx.worktrees.integrate(repo, name, base, [i["meta"]["branch"] for i in code_inputs])
        resolution = None
        if res["conflicts"] and round > 1:
            # Repair round: reproduce the conflict in the worktree and let the integrator resolve it explicitly.
            from ..tools.worktree import git

            wt = res["path"]
            for c in res["conflicts"]:
                await git(wt, "merge", "--no-ff", "--no-edit", c["branch"], check=False)
            sections = self._sections(task, mission, project, agent, feedback, "integrate_conflict")
            sections["workspace"] = [{"conflicts": [{"branch": c["branch"], "files": c["files"], "sides": c["sides"]}
                                                    for c in res["conflicts"]]}]
            text, ctx_id = self._store_packet(task, sections, round)
            system = roles.system_prompt("integrator", agent["charter"] if agent else "", roles.INTEGRATE_CONFLICT_OUTPUT)
            resolution = await self._loop(task=task, mission=mission, project=project, agent=agent, policy=policy,
                                          system=system, packet_text=text, instruction="Resolve the conflicts now.",
                                          tool_ctx=self._tool_ctx(project, task, agent, wt), round=round,
                                          step="resolve_conflict", context_artifact_id=ctx_id)
            unresolved = (await git(wt, "grep", "-l", "-e", "<<<<<<<", "-e", ">>>>>>>", check=False)).stdout.split()
            if unresolved:
                await git(wt, "merge", "--abort", check=False)
                conflicts = res["conflicts"]
            else:
                await git(wt, "add", "-A")
                await git(wt, "commit", "--no-edit", "-m", f"NEXUS integration: resolve conflicts ({task['key']})")
                conflicts = []
            res["head"] = (await git(wt, "rev-parse", "HEAD")).stdout.strip()
        else:
            conflicts = res["conflicts"]
        changed = await self.ctx.worktrees.changed_files(res["path"], base)
        diff = await self.ctx.worktrees.diff(res["path"], base)
        from ..tools.worktree import git

        merged_branches = []
        for i in code_inputs:  # verify against git history, not against the report text
            anc = await git(res["path"], "merge-base", "--is-ancestor", i["meta"]["head"], "HEAD", check=False)
            if anc.returncode == 0:
                merged_branches.append(i["meta"]["branch"])
        report = {"merged": res["merged"], "conflicts": [{"branch": c["branch"], "files": c["files"]} for c in conflicts],
                  "resolution": resolution, "changed_files": changed}
        with self.ctx.db.tx() as cur:
            art = self.ctx.artifacts.create(cur, project_id=task["project_id"], kind="diff", title=task["title"],
                                            content=(f"# Integration report\n\n```json\n{json.dumps(report, indent=2)}\n```\n\n"
                                                     f"```diff\n{diff}\n```\n"), media_type="text/markdown",
                                            created_by=agent["id"] if agent else "agent", mission_id=task["mission_id"],
                                            task_id=task["id"], input_versions={i["task_key"]: i["meta"].get("head")
                                                                                for i in code_inputs},
                                            lineage={"branch": res["branch"], "head": res["head"]})
            if conflicts:
                rooms.post(cur, operation_id=f"{self.attempt['attempt_id']}:r{round}:conflict",
                           project_id=task["project_id"], mission_id=task["mission_id"],
                           thread_id=rooms.thread_for_task(task["id"]), sender=f"agent:{agent['id']}" if agent else "agent",
                           recipients=["owner", "role:integrator"], type="blocker", task_id=task["id"],
                           body=(f"Integration conflict: overlapping edits in {sorted({f for c in conflicts for f in c['files']})}"
                                 f" from branches {[c['branch'] for c in conflicts]}. Mechanism: both branches changed the"
                                 f" same lines. Resolution condition: a merged tree with no conflict markers that passes"
                                 f" combined validation."), refs={"artifact_id": art["id"]})
        return WorkResult(round, art["id"], f"merged {len(res['merged'])}, conflicts {len(conflicts)}",
                          {"kind": "integrate_code", "branch": res["branch"], "head": res["head"], "base": base,
                           "merged_branches": merged_branches,
                           "code_inputs": [{"task_key": i["task_key"], "branch": i["meta"]["branch"],
                                            "head": i["meta"]["head"]} for i in code_inputs],
                           "changed_files": changed, "worktree": str(res["path"]), "conflicts": conflicts,
                           "inputs": [{k: i[k] for k in ("task_key", "artifact_id", "sha256", "title")} for i in inputs]})


def _skill_applies(content: dict[str, Any], task) -> bool:
    kinds = content.get("applies_to_kinds")
    return not kinds or task["kind"] in kinds
