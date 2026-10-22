"""Runs a task's verification plan against a submission.

* Deterministic checks run in application code; raw results are stored as immutable artifacts.
* Model review uses a *different* agent identity than the author (enforced here, not in the
  prompt) and an unprimed packet: requirements + artifact + evidence index, never the author's
  summary or self-assessment.
* Sandbox checks report 'inconclusive' when the sandbox is unavailable - never 'pass'.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..appctx import AppContext
from ..domain.execution import record_review
from ..domain.missions import current_revision
from ..domain.verification import run_check
from ..memory.context import compile_packet
from ..persistence.db import dumps, loads
from ..providers.base import ChatMessage, CompletionRequest, ProviderError, parse_json_output
from . import roles
from .worker import resolve_policy


class Verifier:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def _store_raw(self, task, submission_id: str, criterion: str, result: dict[str, Any]) -> str:
        with self.ctx.db.tx() as cur:
            art = self.ctx.artifacts.create(cur, project_id=task["project_id"], kind="verification_result",
                                            title=f"check {criterion} for {submission_id}",
                                            content=json.dumps(result, indent=2, default=str),
                                            media_type="application/json", created_by="nexus:verifier",
                                            mission_id=task["mission_id"], task_id=task["id"])
        return art["id"]

    async def verify(self, submission_id: str, attempt: dict[str, Any]) -> list[str]:
        db = self.ctx.db
        s = db.one("SELECT * FROM submissions WHERE id = ?", (submission_id,))
        task = db.one("SELECT * FROM tasks WHERE id = ?", (s["task_id"],))
        content = self.ctx.artifacts.read_text(s["artifact_id"])
        meta = loads(s["meta_json"], {})
        meta["_task_id"], meta["_epoch"] = s["task_id"], s["epoch"]
        meta["_artifact_reader"] = lambda sha: self.ctx.artifacts.read_sha(sha).decode("utf-8", errors="replace")
        worktree = Path(meta["worktree"]) if meta.get("worktree") else None
        with db.tx() as cur:
            rev = current_revision(cur, task["mission_id"])
        plan_ctx = {"success_condition_ids": [c["id"] for c in rev["success_conditions"]],
                    "constraint_ids": [c["id"] for c in rev["constraints"]]}
        review_ids = []
        for item in loads(task["verification_plan_json"], []):
            check = item.get("check") or {}
            kind = check.get("kind")
            crit = item["criterion"]
            limitations = item.get("limitations")
            if kind == "reviewer":
                result, reviewer, independence = await self._model_review(task, s, content, attempt, item)
            elif kind == "sandbox_command":
                result, reviewer, independence = await self._sandbox(check, worktree, meta), "nexus:sandbox", "deterministic"
            elif kind == "owner_approval":
                result, reviewer, independence = self._owner_approval(s, crit), "owner", "owner"
            else:
                with db.tx() as cur:
                    result = run_check(cur, check, content=content, mission_id=task["mission_id"], submission_meta=meta,
                                       worktree=worktree, plan_context=plan_ctx)
                reviewer, independence = "nexus:deterministic", "deterministic"
            raw = self._store_raw(task, submission_id, crit, result)
            if result.get("limitation"):
                limitations = f"{limitations or ''} {result['limitation']}".strip()
            with db.tx() as cur:
                review_ids.append(record_review(cur, submission_id=submission_id, criterion=crit, check_kind=kind or "?",
                                                reviewer=reviewer, independence=independence, verdict=result["verdict"],
                                                findings={"summary": result.get("summary"), **{k: v for k, v in
                                                          result.items() if k in ("findings",)}},
                                                raw_artifact_id=raw, blocking=item.get("required", True),
                                                limitations=limitations))
        return review_ids

    async def _sandbox(self, check, worktree: Path | None, meta) -> dict[str, Any]:
        ok, why = await self.ctx.sandbox.available()
        if not ok:
            return {"verdict": "inconclusive", "summary": f"sandbox unavailable: {why}",
                    "limitation": "code was not executed; behavior unverified"}
        if worktree is None:
            return {"verdict": "inconclusive", "summary": "no worktree"}
        cmd = (check.get("args") or {}).get("command") or meta.get("test_command") or ["python", "-m", "pytest", "-q"]
        res = await self.ctx.sandbox.run(worktree, cmd, timeout_s=180)
        return {"verdict": "pass" if res["passed"] else "fail",
                "summary": f"exit {res['exit_code']}{' (timeout)' if res['timed_out'] else ''}", "details": res}

    def _owner_approval(self, s, crit: str) -> dict[str, Any]:
        sha = self.ctx.db.one("SELECT sha256 FROM artifacts WHERE id = ?", (s["artifact_id"],))["sha256"]
        r = self.ctx.db.one(
            "SELECT r.verdict FROM reviews r JOIN submissions s2 ON s2.id = r.submission_id JOIN artifacts a ON"
            " a.id = s2.artifact_id WHERE r.reviewer = 'owner' AND r.criterion = ? AND a.sha256 = ? AND r.task_id = ?"
            " ORDER BY r.created_at DESC LIMIT 1", (crit, sha, s["task_id"]))
        if r is None:
            return {"verdict": "inconclusive", "summary": "awaiting owner approval"}
        return {"verdict": r["verdict"], "summary": f"owner recorded {r['verdict']}"}

    def _pick_reviewer(self, task):
        rows = self.ctx.db.query(
            "SELECT a.*, v.role_default, v.model_policy_json, v.tool_permissions_json, v.memory_scope FROM agent_identities a"
            " JOIN agent_versions v ON v.id = a.current_version_id WHERE (a.project_id = ? OR a.project_id IS NULL)"
            " AND v.role_default = 'reviewer' AND a.archived = 0 ORDER BY a.created_at", (task["project_id"],))
        for r in rows:
            if r["id"] != task["agent_id"]:
                return r
        # The only reviewer wrote this work (e.g. a planned "check" task): fall back to another agent, never the
        # author and never the tool-less guardian, so verification stays independent instead of blocking.
        others = self.ctx.db.query(
            "SELECT a.*, v.role_default, v.model_policy_json, v.tool_permissions_json, v.memory_scope FROM agent_identities a"
            " JOIN agent_versions v ON v.id = a.current_version_id WHERE (a.project_id = ? OR a.project_id IS NULL)"
            " AND v.role_default NOT IN ('reviewer', 'guardian') AND a.archived = 0 AND a.id != ?"
            " ORDER BY CASE v.role_default WHEN 'planner' THEN 0 WHEN 'strategist' THEN 1 ELSE 2 END, a.created_at",
            (task["project_id"], task["agent_id"] or ""))
        return others[0] if others else None

    async def _model_review(self, task, s, content: str, attempt, item) -> tuple[dict[str, Any], str, str]:
        reviewer = self._pick_reviewer(task)
        if reviewer is None:
            return ({"verdict": "inconclusive", "summary": "no reviewer independent of the author is configured"},
                    "nexus:verifier", "none")
        mission = self.ctx.db.one("SELECT * FROM missions WHERE id = ?", (task["mission_id"],))
        author = self.ctx.db.one("SELECT model_policy_json FROM agent_versions v JOIN agent_identities a ON"
                                 " a.current_version_id = v.id WHERE a.id = ?", (task["agent_id"],)) if task["agent_id"] else None
        try:
            policy = resolve_policy(self.ctx, mission, reviewer)
        except ProviderError as exc:
            return {"verdict": "inconclusive", "summary": f"reviewer model unavailable: {exc.detail}"}, \
                   f"agent:{reviewer['id']}", "independent_agent"
        with self.ctx.db.tx() as cur:
            rev = current_revision(cur, task["mission_id"])
        evidence = [{"id": e["id"], "title": e["title"], "lineage_root": e["lineage_root"], "excerpt": (e["excerpt"] or "")[:1500]}
                    for e in self.ctx.db.query("SELECT * FROM evidence WHERE mission_id = ? AND status = 'active'",
                                               (task["mission_id"],))]
        packet = compile_packet({
            "role": "reviewer",
            "goal": {"objective": rev["objective"]},
            "mission_revision": rev["revision"],
            "task": {"key": task["key"], "title": task["title"], "description": task["description"],
                     "acceptance": loads(task["acceptance_json"], []), "review_focus": item.get("text")},
            "hard_constraints": rev["constraints"],
            "required_output": "review",
            "input_versions": [{"artifact_id": s["artifact_id"]}],
            "inputs": [{"id": s["artifact_id"], "title": "submission under review", "content": content}],
            "evidence": evidence,
        })
        text = packet.render()
        with self.ctx.db.tx() as cur:
            ctx_art = self.ctx.artifacts.create(cur, project_id=task["project_id"], kind="context_packet",
                                                title=f"review context {task['key']}", content=text,
                                                media_type="application/json", created_by="nexus:context-compiler",
                                                mission_id=task["mission_id"], task_id=task["id"])
        system = roles.system_prompt("reviewer", reviewer["charter"], roles.REVIEW_OUTPUT)
        req = CompletionRequest(model=policy["model"], system=system,
                                messages=[ChatMessage("user", f"{text}\n\nReview the submission now.")],
                                max_output_tokens=int(policy.get("max_output_tokens", 8192)), response_json=True,
                                metadata={"purpose": "review", "role": "reviewer", "task_kind": task["kind"]})
        op = f"{attempt['attempt_id']}:sub:{s['id']}:review:{item['criterion']}"
        limitation = None
        if author and loads(author["model_policy_json"], {}).get("model") == policy.get("model"):
            limitation = "reviewer uses the same model as the author; reasoning independence is not guaranteed"
        try:
            out = await self.ctx.gateway.call(req, operation_id=op, provider_id=policy["provider_id"], purpose="review",
                                              category="verification", project_id=task["project_id"],
                                              mission_id=task["mission_id"], task_id=task["id"],
                                              attempt_id=attempt["attempt_id"], agent_id=reviewer["id"],
                                              context_artifact_id=ctx_art["id"])
            data = parse_json_output(out.result.text)
        except ProviderError as exc:
            if exc.kind in ("not_configured",):
                raise
            return ({"verdict": "inconclusive", "summary": f"review failed: {exc.kind}: {exc.detail[:200]}",
                     "limitation": limitation}, f"agent:{reviewer['id']}", "independent_agent")
        verdict = data.get("verdict") if data.get("verdict") in ("pass", "fail", "inconclusive") else "inconclusive"
        return ({"verdict": verdict, "summary": str(data.get("summary", ""))[:600], "findings": data.get("findings", []),
                 "limitation": limitation}, f"agent:{reviewer['id']}", "independent_agent")
