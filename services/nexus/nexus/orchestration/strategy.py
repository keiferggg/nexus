"""Bounded strategy controller and plan materialization.

Explicit rules (no learned routing until there is task-specific evidence):
  direct                     - default for separable tasks
  independent_investigation  - research needing >= 2 independent lineages, or open conflicting evidence
  targeted_adversarial_review- deliverables whose claims feed the final integration
  integration                - combine accepted work with combined validation
  recovery                   - same failure fingerprint twice: diagnose before retrying
Each choice records why it fits and what observation would trigger a switch.
"""

from __future__ import annotations

import re

import json
import sqlite3
from typing import Any

from ..domain.missions import add_task, agent_for_role, current_revision
from ..domain.understanding import add_assumption, add_prediction, add_uncertainty, link
from ..domain.verification import ROLE_FOR_KIND, addressed, build_verification_plan
from ..persistence.db import dumps, loads, now_iso
from ..persistence.events import emit


# "Post the summary to https://…", "email the brief to team@…": work whose job is an external effect.
WRITE_INTENT_RE = re.compile(r"\b(post|send|submit|upload|e-?mail|publish|deliver|forward|notify)\b[^.\n]{0,80}?\bto\b\s*"
                             r"(https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+)", re.I)


def external_write_intent(*texts: str | None) -> str | None:
    for t in texts:
        m = WRITE_INTENT_RE.search(t or "")
        if m:
            return m.group(0)
    return None


def choose_strategy(t: dict[str, Any], *, open_conflicts: bool, sandbox_ok: bool) -> dict[str, Any]:
    kind = t["kind"]
    accept = t.get("acceptance", [])
    needs_two = any((a.get("check") or {}).get("kind") == "cites_evidence" and
                    int(((a.get("check") or {}).get("args") or {}).get("min_independent", 1)) >= 2 for a in accept)
    if kind == "integrate":
        return {"mode": "integration", "why": "combines accepted work; needs combined validation",
                "switch_if": "merge conflict or combined check failure -> explicit repair round"}
    if kind == "research" and (needs_two or open_conflicts):
        return {"mode": "independent_investigation",
                "why": "acceptance requires independent source lineages" if needs_two else "conflicting evidence is open",
                "switch_if": "cited sources collapse to one lineage -> recovery (seek a distinct source)"}
    if kind == "code":
        return {"mode": "direct", "why": "separable change in an isolated worktree",
                "switch_if": "same failing check twice -> recovery",
                "sandbox": "available" if sandbox_ok else "unavailable: behavior checks need owner approval"}
    return {"mode": "direct", "why": "separable task with deterministic checks",
            "switch_if": "same failing check twice -> recovery"}


def failure_fingerprint(feedback: list[dict[str, Any]]) -> str:
    return "|".join(sorted({str(f.get("criterion")) for f in feedback}))


def maybe_switch_to_recovery(cur: sqlite3.Cursor, task_id: str, feedback: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Repeated similar failures cause diagnosis, not unlimited retries."""
    t = cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    strat = loads(t["strategy_json"], {})
    fp = failure_fingerprint(feedback)
    history = strat.get("failure_history", [])
    history.append(fp)
    strat["failure_history"] = history[-6:]
    switched = None
    if len(history) >= 2 and history[-1] == history[-2] and strat.get("mode") != "recovery":
        switched = {"from": strat.get("mode"), "to": "recovery",
                    "trigger": f"same failing criteria twice in a row: {fp}",
                    "diagnosis_required": "distinguish execution failure from a false assumption before the next attempt"}
        strat.update({"previous_mode": strat.get("mode"), "mode": "recovery", "why": switched["trigger"],
                      "switch_if": "recovery round fails the same way -> stop and escalate to owner"})
    elif strat.get("mode") == "recovery" and len(history) >= 3 and history[-1] == history[-2] == history[-3]:
        strat["escalate"] = True
        switched = {"from": "recovery", "to": "escalated", "trigger": "recovery did not change the failure"}
    cur.execute("UPDATE tasks SET strategy_json = ? WHERE id = ?", (dumps(strat), task_id))
    if switched:
        emit(cur, "strategy.switched", "nexus:strategy", {**switched, "task_key": t["key"]},
             project_id=t["project_id"], mission_id=t["mission_id"], task_id=task_id)
    return switched


def materialize_plan(cur: sqlite3.Cursor, *, plan_task_id: str | None, plan: dict[str, Any], sandbox_ok: bool,
                     base_commit: str | None, mission_id: str | None = None, add_integrate: bool = True,
                     planned_by: str | None = None) -> list[str]:
    """Turn an accepted plan into tasks. The plan was validated by the plan_schema check; this
    re-applies structural rules defensively and records causal links (why each task exists)."""
    if plan_task_id:
        pt = cur.execute("SELECT * FROM tasks WHERE id = ?", (plan_task_id,)).fetchone()
        mission_id = pt["mission_id"]
    project_id = cur.execute("SELECT project_id FROM missions WHERE id = ?", (mission_id,)).fetchone()["project_id"]
    planned_by = planned_by or plan_task_id
    rev = current_revision(cur, mission_id)
    open_conflicts = cur.execute("SELECT 1 FROM uncertainties WHERE mission_id = ? AND status = 'open' AND"
                                 " kind = 'conflicting_evidence'", (mission_id,)).fetchone() is not None
    existing = {r["key"]: r["id"] for r in cur.execute("SELECT key, id FROM tasks WHERE mission_id = ? AND status NOT IN"
                                                       " ('superseded','canceled')", (mission_id,)).fetchall()}
    tasks = list(plan.get("tasks", []))
    if add_integrate and not any(t.get("kind") == "integrate" for t in tasks):
        tasks.append({"key": "integrate", "title": "Integrate accepted work into the final deliverable", "kind": "integrate",
                      "description": "Combine every accepted deliverable into one coherent result.",
                      "depends_on": [t["key"] for t in tasks], "addresses": "integration",
                      "acceptance": [{"id": "A1", "text": "Includes every accepted input",
                                      "check": {"kind": "includes_inputs"}}]})
    key_to_id: dict[str, str] = {}
    created = []
    for t in tasks:  # topological order not required: dependencies inserted after all tasks exist
        key = t["key"] if t["key"] not in existing else f"{t['key']}-r{rev['revision']}"
        acceptance = list(t.get("acceptance", []))
        addr = addressed(t)
        sc_text = " ".join(c["text"] for c in rev["success_conditions"] if c.get("id") in addr) or None
        intent = external_write_intent(t.get("title"), sc_text)
        if intent and t["kind"] in ("write", "integrate", "research", "check"):
            # Only act tasks can call connectors, and only actions_settled proves the effect happened. Seen live: a
            # "post to httpbin" step planned as integrate claimed a POST that never ran, and a reviewer accepted it.
            t = {**t, "kind": "act", "description": f"{t.get('description', '')}\n\nThis task must perform the real"
                                                    f" action ({intent}) through a connected service tool."}
        if t["kind"] not in ("code", "integrate"):
            # Only code work has a worktree; elsewhere the deliverable is the submitted content itself. Seen live: a
            # "summary.json exists" check on a write task could never pass and blocked an otherwise accepted summary.
            fileless = [a for a in acceptance if (a.get("check") or {}).get("kind") not in ("file_exists", "python_syntax")]
            if len(fileless) != len(acceptance):
                acceptance = fileless or [{"id": "A1", "text": "Meets the task requirements", "check": {"kind": "reviewer"}}]
        if t["kind"] == "code":
            kinds = {(a.get("check") or {}).get("kind") for a in acceptance}
            if "python_syntax" not in kinds:
                acceptance.append({"id": f"A{len(acceptance)+1}", "text": "Changed Python files parse",
                                   "check": {"kind": "python_syntax"}})
            if not sandbox_ok:
                # Behavior checks cannot run: replace with explicit owner approval rather than skipping.
                for a in acceptance:
                    if (a.get("check") or {}).get("kind") == "sandbox_command":
                        a["check"] = {"kind": "owner_approval", "args": {"reason": "sandbox unavailable"}}
                        a["text"] = f"{a.get('text', 'tests pass')} (owner approval: sandbox unavailable)"
        flags_row = cur.execute("SELECT value_json FROM settings WHERE key = ?", (f"mission:{mission_id}:flags",)).fetchone()
        flags = loads(flags_row["value_json"], {}) if flags_row else {}
        if flags:  # evaluation ablations: switch individual verification modules off
            drop = set()
            if flags.get("no_reviewer"):
                drop.add("reviewer")
            if flags.get("no_quote_check"):
                drop.add("quotes_verified")
            acceptance = [a for a in acceptance if (a.get("check") or {}).get("kind") not in drop] or acceptance[:1]
            if flags.get("no_lineage"):
                for a in acceptance:
                    if (a.get("check") or {}).get("kind") == "cites_evidence":
                        a["check"] = {"kind": "cites_evidence", "args": {"min_independent": 1}}
        proj = cur.execute("SELECT settings_json FROM projects WHERE id = ?", (project_id,)).fetchone()
        unattended = (loads(proj["settings_json"], {}).get("autonomy") or {}).get("mode") == "unattended"
        if unattended and t["kind"] in ("research", "write", "integrate", "discover") and \
                "no_injected_instructions" not in {(a.get("check") or {}).get("kind") for a in acceptance}:
            acceptance.append({"id": f"A{len(acceptance)+1}", "text": "Output carries no instructions copied from sources",
                               "check": {"kind": "no_injected_instructions"}})
        if t["kind"] == "act" and "actions_settled" not in {(a.get("check") or {}).get("kind") for a in acceptance}:
            acceptance.append({"id": f"A{len(acceptance)+1}", "text": "Requested real actions completed",
                               "check": {"kind": "actions_settled", "args": {"min_actions": 1}}})
        if t["kind"] == "integrate":
            kinds = {(a.get("check") or {}).get("kind") for a in acceptance}
            deps_code = any(next((x for x in tasks if x["key"] == d), {}).get("kind") == "code" for d in t.get("depends_on", []))
            if deps_code and "no_conflicts" not in kinds:
                acceptance.append({"id": f"A{len(acceptance)+1}", "text": "Branches integrate without unresolved conflicts",
                                   "check": {"kind": "no_conflicts"}})
                acceptance.append({"id": f"A{len(acceptance)+1}", "text": "Combined result parses",
                                   "check": {"kind": "python_syntax"}})
        tdict = {**t, "acceptance": acceptance}
        strat = choose_strategy(tdict, open_conflicts=open_conflicts, sandbox_ok=sandbox_ok)
        if t["kind"] in ("code", "integrate") and base_commit:
            strat["base_commit"] = base_commit
        role = ROLE_FOR_KIND.get(t["kind"], "writer")
        agent = agent_for_role(cur, project_id, role)
        tid = add_task(cur, mission_id=mission_id, project_id=project_id, key=key, title=t["title"], kind=t["kind"],
                       description=t["description"], role=role, agent_id=agent["id"] if agent else None,
                       revision=rev["revision"], acceptance=acceptance,
                       verification_plan=build_verification_plan({"acceptance": acceptance}),
                       cause={"addresses": t.get("addresses"), "constraint_ids": t.get("constraint_ids", []),
                              "planned_by": planned_by, "revision": rev["revision"]},
                       strategy=strat, priority=0.0)
        key_to_id[t["key"]] = tid
        created.append(tid)
        if plan_task_id:
            link(cur, "task", plan_task_id, "task", tid)
        for p in t.get("predictions", []) or []:
            if isinstance(p, dict) and p.get("observable"):
                add_prediction(cur, mission_id=mission_id, task_id=tid, statement=str(p.get("statement", "")),
                               observable=str(p["observable"]), expected=p.get("expected"), created_by="agent:planner")
    for t in tasks:
        tid = key_to_id[t["key"]]
        for d in t.get("depends_on", []) or []:
            dep = key_to_id.get(d) or existing.get(d)
            if dep:
                cur.execute("INSERT OR IGNORE INTO dependencies (task_id, depends_on_task_id) VALUES (?,?)", (tid, dep))
    for u in plan.get("uncertainties", []) or []:
        if isinstance(u, dict) and u.get("question"):
            add_uncertainty(cur, mission_id=mission_id, kind=u.get("kind") if u.get("kind") in (
                "missing_evidence", "conflicting_evidence", "ambiguous_intent", "execution", "explanation") else "missing_evidence",
                question=u["question"], would_change=u.get("would_change", "unspecified"),
                affects=[a for a in u.get("affects", []) if isinstance(a, str)], cost_hint=u.get("cost_hint", "medium"),
                created_by="agent:planner")
    for a in plan.get("assumptions", []) or []:
        if isinstance(a, dict) and a.get("statement"):
            add_assumption(cur, mission_id=mission_id, statement=a["statement"], basis=a.get("basis", "planner"),
                           created_by="agent:planner")
    prioritize(cur, mission_id)
    emit(cur, "plan.materialized", "nexus:planner-gate", {"plan_task_id": plan_task_id, "created": len(created),
                                                          "keys": list(key_to_id)},
         project_id=project_id, mission_id=mission_id, task_id=plan_task_id)
    return created


def prioritize(cur: sqlite3.Cursor, mission_id: str) -> None:
    """Uncertainty-driven priority: tasks named by open uncertainties (cheap discriminating checks
    first) and tasks with many dependents run earlier. Rule-based and recorded."""
    from ..domain.understanding import rank_uncertainties

    ranked = rank_uncertainties(cur, mission_id)
    keyscore: dict[str, float] = {}
    for u in ranked:
        for k in u["affects"]:
            keyscore[k] = max(keyscore.get(k, 0), u["score"])
    for t in cur.execute("SELECT id, key FROM tasks WHERE mission_id = ? AND status IN ('draft','ready')",
                         (mission_id,)).fetchall():
        dependents = cur.execute("SELECT COUNT(*) AS n FROM dependencies WHERE depends_on_task_id = ?", (t["id"],)).fetchone()["n"]
        pr = keyscore.get(t["key"], 0) + dependents
        cur.execute("UPDATE tasks SET priority = ? WHERE id = ?", (pr, t["id"]))
