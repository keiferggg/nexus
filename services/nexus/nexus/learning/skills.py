"""Measured procedural learning: candidates, frozen-baseline evaluation, promotion, rollback.

* A candidate carries a causal hypothesis, provenance, applicable conditions and a version.
* Evaluation compares candidate vs the frozen baseline (the currently promoted version, or no
  skill) on separate splits: ``train`` (optimization feedback), ``heldout`` (never used to build
  the candidate) and ``past_failures``. The suite is frozen by content hash in the report.
* Outcome = NEXUS-verified acceptance of the eval task, never an agent self-report or HTTP 200.
* Promotion policy (all must hold): held-out pass rate >= baseline and >= ``min_heldout``;
  an improvement on train or held-out; no regression on past failures.
* Auto-promotion only when the owner enabled it for that skill's narrow scope; it goes to
  ``canary`` first. Otherwise eligible candidates wait for the owner. Rollback restores the
  previous version. Nothing here modifies running code, permissions or budgets.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from ..persistence.db import Database, dumps, loads, new_id, now_iso
from ..persistence.events import emit

TARGETS = {"procedure", "retrieval", "routing", "coordination", "verification", "planning"}


@dataclass
class EvalCase:
    id: str
    split: str  # train | heldout | past_failures
    task: dict[str, Any]  # explicit task spec (kind, title, description, acceptance)


@dataclass
class EvalSuite:
    name: str
    cases: list[EvalCase]

    def frozen_hash(self) -> str:
        blob = json.dumps([{"id": c.id, "split": c.split, "task": c.task} for c in self.cases], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


class EvalHarness(Protocol):
    async def run_case(self, case: EvalCase, skill_version_id: str | None) -> dict[str, Any]: ...


@dataclass
class PromotionPolicy:
    min_heldout: float = 0.8
    min_improvement: float = 0.01


class SkillRegistry:
    def __init__(self, db: Database):
        self.db = db

    def propose(self, *, skill_name: str, target: str, hypothesis: str, provenance: dict[str, Any], conditions: str,
                content: dict[str, Any], proposer: str) -> str:
        if target not in TARGETS:
            raise ValueError(f"target must be one of {sorted(TARGETS)}")
        if not hypothesis.strip() or not conditions.strip():
            raise ValueError("a candidate needs a causal hypothesis and applicable conditions")
        if not provenance:
            raise ValueError("a candidate needs provenance")
        with self.db.tx() as cur:
            v = cur.execute("SELECT COALESCE(MAX(version),0) AS v FROM skill_versions WHERE skill_name = ?",
                            (skill_name,)).fetchone()["v"] + 1
            parent = cur.execute("SELECT id FROM skill_versions WHERE skill_name = ? AND status = 'promoted'",
                                 (skill_name,)).fetchone()
            sid = new_id("skl")
            cur.execute("INSERT INTO skill_versions (id, skill_name, version, status, target, hypothesis, provenance_json,"
                        " conditions, content_json, parent_version_id, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, skill_name, v, "candidate", target, hypothesis, dumps(provenance), conditions, dumps(content),
                         parent["id"] if parent else None, now_iso(), now_iso()))
            emit(cur, "skill.candidate", proposer, {"skill_version_id": sid, "skill": skill_name, "version": v,
                                                    "hypothesis": hypothesis})
        return sid

    def get(self, sid: str):
        r = self.db.one("SELECT * FROM skill_versions WHERE id = ?", (sid,))
        if r is None:
            raise KeyError(sid)
        return r

    def auto_promotion_enabled(self, skill_name: str) -> bool:
        r = self.db.one("SELECT value_json FROM settings WHERE key = 'learning.auto_promote'")
        cfg = loads(r["value_json"], {}) if r else {}
        return bool(cfg.get("enabled")) and skill_name in cfg.get("scopes", [])

    async def evaluate(self, sid: str, suite: EvalSuite, harness: EvalHarness,
                       policy: PromotionPolicy | None = None) -> dict[str, Any]:
        policy = policy or PromotionPolicy()
        cand = self.get(sid)
        if cand["status"] not in ("candidate", "evaluating"):
            raise ValueError(f"skill version is {cand['status']}")
        baseline_id = cand["parent_version_id"]
        with self.db.tx() as cur:
            cur.execute("UPDATE skill_versions SET status = 'evaluating', updated_at = ? WHERE id = ?", (now_iso(), sid))
        splits: dict[str, dict[str, Any]] = {}
        per_case = []
        for case in suite.cases:
            base = await harness.run_case(case, baseline_id)
            new = await harness.run_case(case, sid)
            per_case.append({"case": case.id, "split": case.split, "baseline": base["passed"], "candidate": new["passed"],
                             "baseline_cost_usd": base.get("cost_usd"), "candidate_cost_usd": new.get("cost_usd"),
                             "candidate_detail": new.get("detail")})
            s = splits.setdefault(case.split, {"n": 0, "baseline_pass": 0, "candidate_pass": 0})
            s["n"] += 1
            s["baseline_pass"] += int(base["passed"])
            s["candidate_pass"] += int(new["passed"])
        rates = {k: {"baseline": v["baseline_pass"] / v["n"], "candidate": v["candidate_pass"] / v["n"], "n": v["n"]}
                 for k, v in splits.items()}
        held = rates.get("heldout")
        reasons = []
        if held is None:
            reasons.append("no held-out cases: cannot establish generalization")
        else:
            if held["candidate"] < held["baseline"]:
                reasons.append(f"held-out regression: {held['candidate']:.2f} < baseline {held['baseline']:.2f}")
            if held["candidate"] < policy.min_heldout:
                reasons.append(f"held-out pass rate {held['candidate']:.2f} below policy minimum {policy.min_heldout}")
        pf = rates.get("past_failures")
        if pf and pf["candidate"] < pf["baseline"]:
            reasons.append("regression on relevant past failures")
        improved = any(r["candidate"] - r["baseline"] >= policy.min_improvement for k, r in rates.items()
                       if k in ("train", "heldout"))
        if not improved:
            reasons.append("no measured improvement over the frozen baseline")
        eligible = not reasons
        if not eligible:
            status = "rejected"
        elif self.auto_promotion_enabled(cand["skill_name"]):
            status = "canary"
        else:
            status = "evaluating"  # eligible; awaiting owner promotion
        report = {"suite": suite.name, "suite_hash": suite.frozen_hash(), "baseline_version_id": baseline_id,
                  "rates": rates, "cases": per_case, "eligible": eligible, "reasons": reasons,
                  "policy": policy.__dict__, "decided_status": status, "evaluated_at": now_iso(),
                  "note": "Outcomes are NEXUS-verified acceptances of eval tasks; not a general intelligence claim."}
        with self.db.tx() as cur:
            cur.execute("UPDATE skill_versions SET status = ?, eval_json = ?, updated_at = ? WHERE id = ?",
                        (status, dumps(report), now_iso(), sid))
            emit(cur, "skill.evaluated", "nexus:learning", {"skill_version_id": sid, "status": status, "rates": rates,
                                                            "reasons": reasons})
        return report

    def promote(self, sid: str, *, actor: str) -> None:
        cand = self.get(sid)
        if cand["status"] not in ("evaluating", "canary"):
            raise ValueError(f"cannot promote a {cand['status']} version")
        rep = loads(cand["eval_json"], {})
        if not rep.get("eligible"):
            raise ValueError("version did not pass its evaluation policy")
        if cand["status"] == "evaluating" and actor != "owner":
            raise PermissionError("only the owner may promote without auto-promotion enabled")
        with self.db.tx() as cur:
            cur.execute("UPDATE skill_versions SET status = 'retired', updated_at = ? WHERE skill_name = ? AND"
                        " status = 'promoted'", (now_iso(), cand["skill_name"]))
            cur.execute("UPDATE skill_versions SET status = 'promoted', updated_at = ? WHERE id = ?", (now_iso(), sid))
            emit(cur, "skill.promoted", actor, {"skill_version_id": sid, "skill": cand["skill_name"],
                                               "version": cand["version"]})

    def rollback(self, sid: str, *, actor: str, reason: str) -> str | None:
        cand = self.get(sid)
        if cand["status"] not in ("promoted", "canary"):
            raise ValueError(f"cannot roll back a {cand['status']} version")
        with self.db.tx() as cur:
            cur.execute("UPDATE skill_versions SET status = 'rolled_back', updated_at = ? WHERE id = ?", (now_iso(), sid))
            restored = None
            if cand["parent_version_id"]:
                cur.execute("UPDATE skill_versions SET status = 'promoted', updated_at = ? WHERE id = ? AND status ="
                            " 'retired'", (now_iso(), cand["parent_version_id"]))
                restored = cand["parent_version_id"] if cur.rowcount else None
            emit(cur, "skill.rolled_back", actor, {"skill_version_id": sid, "reason": reason, "restored": restored})
        return restored


class MissionEvalHarness:
    """Runs each eval case as an isolated single-task mission through the real NEXUS pipeline
    (context compiler -> gateway -> verifier). Pass = the task was accepted."""

    def __init__(self, sup, project_id: str, *, mode: str = "offline_fake", spend_cap_usd: float | None = None):
        self.sup = sup
        self.project_id = project_id
        self.mode = mode
        self.cap = spend_cap_usd

    async def run_case(self, case: EvalCase, skill_version_id: str | None) -> dict[str, Any]:
        from ..domain.budget import summary
        from ..domain.missions import create_mission_with_tasks, start_mission

        db = self.sup.ctx.db
        with db.tx() as cur:
            mid = create_mission_with_tasks(cur, project_id=self.project_id, title=f"eval {case.id}",
                                            objective=case.task.get("description", case.id),
                                            success_conditions=[case.task.get("title", case.id)], constraints=[],
                                            execution_mode=self.mode, spend_cap_usd=self.cap,
                                            tasks=[{**case.task, "key": case.task.get("key", "case"), "addresses": "SC1"}],
                                            purpose="evaluation")
            cur.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?,?,?)",
                        (f"mission:{mid}:skills", dumps({"only": [skill_version_id] if skill_version_id else []}),
                         now_iso()))
            start_mission(cur, mid, actor="nexus:learning")
        status = await self.sup.wait_idle(mid, timeout=120)
        t = db.one("SELECT status FROM tasks WHERE mission_id = ? AND key = ?", (mid, case.task.get("key", "case")))
        with db.tx() as cur:
            cost = summary(cur, mid)["committed_usd"]
        return {"passed": t["status"] == "accepted", "cost_usd": cost, "detail": f"mission {mid} {status}/{t['status']}"}
