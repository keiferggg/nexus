"""Projects, persistent agent identities, missions and mission revisions."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..persistence.db import ConflictError, dumps, loads, new_id, now_iso
from ..persistence.events import emit
from .tasks import ACTIVE, transition

DEFAULT_TEAM = [
    ("Atlas", "planner", "Turns owner objectives into dependency-ordered tasks with explicit acceptance checks.",
     ["read_workspace", "search_memory", "propose_strategy", "consult"]),
    ("Iris", "researcher", "Finds and reads sources, records evidence with lineage, flags conflicting evidence.",
     ["read_workspace", "read_source", "fetch_url", "search_memory", "consult", "custom_tools"]),
    ("Quill", "writer", "Writes documents grounded only in recorded evidence; cites sources by evidence id.",
     ["read_workspace", "read_source", "search_memory", "consult", "custom_tools"]),
    ("Forge", "engineer", "Changes software in an isolated git worktree and validates the change.",
     ["read_workspace", "repo_read", "repo_write", "run_tests", "consult", "create_tools", "custom_tools"]),
    ("Vera", "reviewer", "Independently reviews submissions against requirements without the author's conclusions.",
     ["read_workspace", "read_source"]),
    ("Weave", "integrator", "Combines accepted work, resolves conflicts explicitly, runs combined validation.",
     ["read_workspace", "repo_read", "repo_write", "run_tests", "consult"]),
    ("Scout", "strategist", "Discovers objectives that serve the owner's standing goals and proposes strategies, with evidence.",
     ["read_workspace", "read_source", "search_memory", "propose_objective", "propose_strategy", "consult",
      "use_connectors", "create_tools", "custom_tools", "delegate_task"]),
    ("Relay", "operator", "Takes real actions through connected services, only as tasks require, and reports outcomes.",
     ["read_workspace", "read_source", "use_connectors", "consult", "custom_tools"]),
    ("Sentinel", "guardian", "Independently checks that unattended actions serve the owner's mission. Has no tools and never "
     "reads untrusted content.", []),
]
NO_TOOL_ROLES = {"guardian"}


UNIVERSAL_CAPABILITIES = ["internet", "utilities", "use_connectors", "custom_tools"]


def ensure_team(cur: sqlite3.Cursor, project_id: str) -> list[str]:
    """Add default roles missing from a project (e.g. created before a role existed)."""
    have = {r["role_default"] for r in cur.execute(
        "SELECT v.role_default FROM agent_identities a JOIN agent_versions v ON v.id = a.current_version_id WHERE"
        " a.project_id = ? AND a.archived = 0", (project_id,)).fetchall()}
    added = []
    for agent_name, role, charter, tools in DEFAULT_TEAM:
        if role not in have:
            added.append(create_agent(cur, project_id=project_id, name=agent_name, role=role, charter=charter,
                                      tools=tools, model_policy={"provider_id": None, "model": None,
                                                                 "max_output_tokens": 8192}))
    return added


class MissionStateError(Exception):
    pass


def create_project(cur: sqlite3.Cursor, *, name: str, workspace_root: str, is_demo: bool = False,
                   default_model_policy: dict[str, Any] | None = None) -> str:
    pid = new_id("prj")
    cur.execute("INSERT INTO projects (id, name, workspace_root, is_demo, created_at) VALUES (?,?,?,?,?)",
                (pid, name, workspace_root, int(is_demo), now_iso()))
    for agent_name, role, charter, tools in DEFAULT_TEAM:
        create_agent(cur, project_id=pid, name=agent_name, role=role, charter=charter, tools=tools,
                     model_policy=default_model_policy or {"provider_id": None, "model": None, "max_output_tokens": 8192})
    emit(cur, "project.created", "owner", {"name": name, "workspace_root": workspace_root, "is_demo": is_demo},
         project_id=pid)
    return pid


def create_agent(cur: sqlite3.Cursor, *, project_id: str | None, name: str, role: str, charter: str,
                 tools: list[str], model_policy: dict[str, Any], memory_scope: str = "private") -> str:
    if role not in NO_TOOL_ROLES:
        tools = list(dict.fromkeys(list(tools) + UNIVERSAL_CAPABILITIES))
    aid = new_id("agt")
    cur.execute("INSERT INTO agent_identities (id, project_id, name, charter, created_at) VALUES (?,?,?,?,?)",
                (aid, project_id, name, charter, now_iso()))
    new_agent_version(cur, aid, role=role, model_policy=model_policy, tools=tools, memory_scope=memory_scope)
    return aid


def new_agent_version(cur: sqlite3.Cursor, agent_id: str, *, role: str, model_policy: dict[str, Any],
                      tools: list[str], memory_scope: str) -> str:
    row = cur.execute("SELECT COALESCE(MAX(version), 0) AS v FROM agent_versions WHERE agent_id = ?", (agent_id,)).fetchone()
    vid = new_id("agv")
    cur.execute(
        "INSERT INTO agent_versions (id, agent_id, version, role_default, model_policy_json, tool_permissions_json,"
        " memory_scope, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (vid, agent_id, row["v"] + 1, role, dumps(model_policy), dumps(tools), memory_scope, now_iso()),
    )
    cur.execute("UPDATE agent_identities SET current_version_id = ? WHERE id = ?", (vid, agent_id))
    return vid


def agent_for_role(cur: sqlite3.Cursor, project_id: str, role: str) -> sqlite3.Row | None:
    return cur.execute(
        "SELECT a.*, v.role_default, v.model_policy_json, v.tool_permissions_json, v.memory_scope, v.version AS agent_version"
        " FROM agent_identities a JOIN agent_versions v ON v.id = a.current_version_id"
        " WHERE (a.project_id = ? OR a.project_id IS NULL) AND v.role_default = ? AND a.archived = 0"
        " ORDER BY a.project_id IS NULL, a.created_at LIMIT 1",
        (project_id, role),
    ).fetchone()


def create_mission(
    cur: sqlite3.Cursor,
    *,
    project_id: str,
    title: str,
    objective: str,
    success_conditions: list[str],
    constraints: list[str],
    execution_mode: str,
    spend_cap_usd: float | None,
    max_active_workers: int = 4,
    max_branches: int = 2,
) -> str:
    if execution_mode == "live" and (spend_cap_usd is None or spend_cap_usd <= 0):
        raise MissionStateError("live execution requires a positive mission spend cap")
    if not 1 <= max_active_workers <= 16 or not 0 <= max_branches <= 4:
        raise MissionStateError("concurrency settings out of bounds")
    mid = new_id("msn")
    ts = now_iso()
    cur.execute(
        "INSERT INTO missions (id, project_id, title, status, current_revision, execution_mode, spend_cap_usd,"
        " max_active_workers, max_branches, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (mid, project_id, title, "draft", 1, execution_mode, spend_cap_usd, max_active_workers, max_branches, ts, ts),
    )
    sc = [{"id": f"SC{i+1}", "text": t} for i, t in enumerate(success_conditions)]
    cons = [{"id": f"C{i+1}", "text": t} for i, t in enumerate(constraints)]
    cur.execute(
        "INSERT INTO mission_revisions (id, mission_id, revision, objective, success_conditions_json, constraints_json,"
        " reason, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id("rev"), mid, 1, objective, dumps(sc), dumps(cons), "initial", "owner", ts),
    )
    planner = agent_for_role(cur, project_id, "planner")
    add_task(cur, mission_id=mid, project_id=project_id, key="plan", title="Plan the mission", kind="plan",
             description="Produce a dependency-ordered task plan with acceptance criteria and a verification plan.",
             role="planner", agent_id=planner["id"] if planner else None, revision=1,
             acceptance=[{"id": "A1", "text": "Plan is valid against the NEXUS plan schema"}],
             verification_plan=[{"criterion": "A1", "check": {"kind": "plan_schema"}, "evidence_required": "plan JSON",
                                 "verifier": "nexus:deterministic", "independence": "deterministic", "required": True,
                                 "limitations": "validates structure, not plan quality"}],
             cause={"addresses": "objective", "revision": 1})
    emit(cur, "mission.created", "owner", {"title": title, "execution_mode": execution_mode, "spend_cap_usd": spend_cap_usd},
         project_id=project_id, mission_id=mid)
    return mid


def add_task(cur: sqlite3.Cursor, *, mission_id: str, project_id: str, key: str, title: str, kind: str,
             description: str, role: str, agent_id: str | None, revision: int, acceptance: list[dict],
             verification_plan: list[dict], cause: dict, depends_on: list[str] | None = None,
             strategy: dict | None = None, priority: float = 0.0, max_attempts: int = 3) -> str:
    tid = new_id("tsk")
    ts = now_iso()
    cur.execute(
        "INSERT INTO tasks (id, mission_id, project_id, key, title, kind, description, status, mission_revision, role,"
        " agent_id, acceptance_json, verification_plan_json, strategy_json, cause_json, priority, max_attempts,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, mission_id, project_id, key, title, kind, description, "draft", revision, role, agent_id,
         dumps(acceptance), dumps(verification_plan), dumps(strategy or {}), dumps(cause), priority, max_attempts, ts, ts),
    )
    for dep in depends_on or []:
        cur.execute("INSERT INTO dependencies (task_id, depends_on_task_id) VALUES (?,?)", (tid, dep))
    emit(cur, "task.created", "nexus:planner-gate", {"key": key, "title": title, "kind": kind, "cause": cause,
                                                     "depends_on": depends_on or []},
         project_id=project_id, mission_id=mission_id, task_id=tid)
    return tid


def get_mission(cur: sqlite3.Cursor, mission_id: str) -> sqlite3.Row:
    m = cur.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
    if m is None:
        raise KeyError(mission_id)
    return m


def current_revision(cur: sqlite3.Cursor, mission_id: str) -> dict[str, Any]:
    m = get_mission(cur, mission_id)
    r = cur.execute("SELECT * FROM mission_revisions WHERE mission_id = ? AND revision = ?",
                    (mission_id, m["current_revision"])).fetchone()
    return {"revision": r["revision"], "objective": r["objective"],
            "success_conditions": loads(r["success_conditions_json"], []),
            "constraints": loads(r["constraints_json"], []), "reason": r["reason"], "created_at": r["created_at"]}


def set_mission_status(cur: sqlite3.Cursor, mission_id: str, status: str, *, actor: str, reason: str,
                       allowed_from: set[str]) -> None:
    m = get_mission(cur, mission_id)
    if m["status"] not in allowed_from:
        raise MissionStateError(f"mission is {m['status']}; cannot move to {status}")
    cur.execute("UPDATE missions SET status = ?, status_reason = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (status, reason, now_iso(), mission_id))
    emit(cur, "mission.status", actor, {"from": m["status"], "to": status, "reason": reason},
         project_id=m["project_id"], mission_id=mission_id)


def start_mission(cur: sqlite3.Cursor, mission_id: str, *, actor: str = "owner") -> None:
    m = get_mission(cur, mission_id)
    if m["execution_mode"] == "live" and not m["spend_cap_usd"]:
        raise MissionStateError("live execution requires a configured mission spend cap")
    set_mission_status(cur, mission_id, "running", actor=actor, reason="started by owner",
                       allowed_from={"draft", "paused", "blocked"})


def pause_mission(cur: sqlite3.Cursor, mission_id: str, *, actor: str = "owner") -> None:
    set_mission_status(cur, mission_id, "paused", actor=actor,
                       reason="paused: no new work is scheduled; in-flight attempts finish their current step",
                       allowed_from={"running", "blocked"})


def cancel_mission(cur: sqlite3.Cursor, mission_id: str, *, actor: str = "owner") -> list[str]:
    """Stops new scheduling immediately (status canceling). The supervisor signals workers and,
    once they have stopped, finalizes to canceled. Returns active attempt ids to signal."""
    set_mission_status(cur, mission_id, "canceling", actor=actor, reason="cancel requested by owner",
                       allowed_from={"draft", "planning", "running", "paused", "blocked"})
    attempts = cur.execute(
        "SELECT a.id FROM task_attempts a JOIN tasks t ON t.id = a.task_id WHERE t.mission_id = ? AND a.status IN"
        " ('running','submitted')", (mission_id,)).fetchall()
    for t in cur.execute("SELECT id, status FROM tasks WHERE mission_id = ? AND status IN ('draft','ready','blocked','failed')",
                         (mission_id,)).fetchall():
        transition(cur, t["id"], "canceled", actor="nexus:supervisor", reason="mission canceled")
    return [a["id"] for a in attempts]


def revise_mission(cur: sqlite3.Cursor, mission_id: str, *, objective: str | None, success_conditions: list[str] | None,
                   constraints: list[str] | None, reason: str, actor: str = "owner") -> dict[str, Any]:
    """Human goal change: new revision; affected work is invalidated explicitly, unaffected
    in-flight attempts are rebased with a recorded event."""
    if actor != "owner":
        raise PermissionError("only the owner may revise mission requirements")
    m = get_mission(cur, mission_id)
    prev = current_revision(cur, mission_id)
    new_rev = m["current_revision"] + 1
    new_obj = objective if objective is not None else prev["objective"]
    new_sc = ([{"id": f"SC{i+1}", "text": t} for i, t in enumerate(success_conditions)]
              if success_conditions is not None else prev["success_conditions"])
    new_cons = ([{"id": f"C{i+1}", "text": t} for i, t in enumerate(constraints)]
                if constraints is not None else prev["constraints"])
    old_c = {c["id"]: c["text"] for c in prev["constraints"]}
    new_c = {c["id"]: c["text"] for c in new_cons}
    changed_constraints = sorted({k for k in old_c.keys() | new_c.keys() if old_c.get(k) != new_c.get(k)})
    goal_changed = new_obj != prev["objective"] or new_sc != prev["success_conditions"]

    cur.execute(
        "INSERT INTO mission_revisions (id, mission_id, revision, objective, success_conditions_json, constraints_json,"
        " reason, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id("rev"), mission_id, new_rev, new_obj, dumps(new_sc), dumps(new_cons), reason, actor, now_iso()),
    )
    cur.execute("UPDATE missions SET current_revision = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (new_rev, now_iso(), mission_id))

    affected, rebased = [], []
    tasks = cur.execute("SELECT * FROM tasks WHERE mission_id = ? AND status NOT IN ('canceled','superseded')",
                        (mission_id,)).fetchall()
    for t in tasks:
        cause = loads(t["cause_json"], {})
        refs = set(cause.get("constraint_ids", []))
        # Conservative: tasks that declare no constraint references, integrate/write tasks, and
        # (if the goal changed) everything is affected.
        is_affected = goal_changed or bool(refs & set(changed_constraints)) or (
            changed_constraints and (not refs or t["kind"] in ("integrate", "write")))
        if t["kind"] == "plan" and not goal_changed:
            is_affected = False
        if is_affected:
            affected.append(t["id"])
        else:
            rebased.append(t["id"])
    for tid in affected:
        invalidate_task(cur, tid, reason=f"mission revision {new_rev}: {reason}", actor="nexus:revision")
    for tid in rebased:
        cur.execute("UPDATE task_attempts SET mission_revision = ? WHERE task_id = ? AND status IN ('running','submitted')",
                    (new_rev, tid))
        cur.execute("UPDATE tasks SET mission_revision = ? WHERE id = ?", (new_rev, tid))
    if goal_changed:
        planner = agent_for_role(cur, m["project_id"], "planner")
        add_task(cur, mission_id=mission_id, project_id=m["project_id"], key=f"replan-r{new_rev}",
                 title=f"Replan for revision {new_rev}", kind="plan",
                 description=f"Objective or success conditions changed: {reason}. Produce tasks for unmet conditions.",
                 role="planner", agent_id=planner["id"] if planner else None, revision=new_rev,
                 acceptance=[{"id": "A1", "text": "Plan is valid against the NEXUS plan schema"}],
                 verification_plan=[{"criterion": "A1", "check": {"kind": "plan_schema"}, "evidence_required": "plan JSON",
                                     "verifier": "nexus:deterministic", "independence": "deterministic", "required": True}],
                 cause={"addresses": "mission_revision", "revision": new_rev, "reason": reason})
    emit(cur, "mission.revised", actor,
         {"revision": new_rev, "reason": reason, "changed_constraints": changed_constraints, "goal_changed": goal_changed,
          "affected_tasks": affected, "rebased_tasks": rebased},
         project_id=m["project_id"], mission_id=mission_id)
    return {"revision": new_rev, "affected": affected, "rebased": rebased, "changed_constraints": changed_constraints}


def invalidate_task(cur: sqlite3.Cursor, task_id: str, *, reason: str, actor: str) -> None:
    """Explicit invalidation: accepted results stay historically accepted but become non-current;
    in-flight attempts are fenced; the task reopens in a new epoch for repair/re-verification."""
    from .execution import abandon_attempt  # local import to avoid cycle

    t = cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if t["status"] in ("canceled", "superseded"):
        return
    cur.execute("UPDATE acceptances SET is_current = 0, noncurrent_reason = ? WHERE task_id = ? AND is_current = 1",
                (reason, task_id))
    for a in cur.execute("SELECT id FROM task_attempts WHERE task_id = ? AND status IN ('running','submitted')",
                         (task_id,)).fetchall():
        abandon_attempt(cur, a["id"], reason=reason, actor=actor)
    t = cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    m = get_mission(cur, t["mission_id"])
    cur.execute("UPDATE tasks SET epoch = epoch + 1, is_stale = 1, stale_reason = ?, mission_revision = ?,"
                " attempt_count = 0 WHERE id = ?", (reason, m["current_revision"], task_id))
    if t["status"] == "failed":
        transition(cur, task_id, "ready", actor=actor, reason=reason)
        transition(cur, task_id, "draft", actor=actor, reason=reason)
    elif t["status"] in ("accepted", "ready", "blocked"):
        transition(cur, task_id, "draft", actor=actor, reason=reason)
    emit(cur, "task.invalidated", actor, {"reason": reason, "previous_status": t["status"]},
         project_id=t["project_id"], mission_id=t["mission_id"], task_id=task_id)


def check_mission_completion(cur: sqlite3.Cursor, mission_id: str) -> str | None:
    m = get_mission(cur, mission_id)
    if m["status"] not in ("running",):
        return None
    rows = cur.execute("SELECT status, is_stale, kind FROM tasks WHERE mission_id = ? AND status NOT IN ('canceled','superseded')",
                       (mission_id,)).fetchall()
    if not rows:
        return None
    non_plan = [r for r in rows if r["kind"] != "plan"]
    if all(r["status"] == "accepted" and not r["is_stale"] for r in rows) and non_plan:
        set_mission_status(cur, mission_id, "completed", actor="nexus:supervisor",
                           reason="all tasks accepted and current", allowed_from={"running"})
        return "completed"
    # After promote_ready has run, drafts cannot progress unless something is ready or active.
    if any(r["status"] in ("failed", "blocked") for r in rows) and not any(
            r["status"] in ACTIVE | {"ready"} for r in rows):
        set_mission_status(cur, mission_id, "blocked", actor="nexus:supervisor",
                           reason="no runnable work remains; failed/blocked tasks need owner attention",
                           allowed_from={"running"})
        return "blocked"
    return None


def require_mutable(m: sqlite3.Row) -> None:
    if m["status"] in ("canceled", "completed"):
        raise ConflictError(f"mission is {m['status']}")


def create_mission_with_tasks(cur: sqlite3.Cursor, *, project_id: str, title: str, objective: str,
                              success_conditions: list[str], constraints: list[str], execution_mode: str,
                              spend_cap_usd: float | None, tasks: list[dict[str, Any]], purpose: str = "owner_plan",
                              add_integrate: bool = False) -> str:
    """Mission with an owner-supplied (or evaluation) task list instead of a planner task.
    The task list is validated with the same plan rules a planner's output must pass."""
    from ..domain.verification import validate_plan
    from ..orchestration.strategy import materialize_plan

    mid = create_mission(cur, project_id=project_id, title=title, objective=objective,
                         success_conditions=success_conditions, constraints=constraints, execution_mode=execution_mode,
                         spend_cap_usd=spend_cap_usd)
    errs = validate_plan({"tasks": tasks}, success_condition_ids=[f"SC{i+1}" for i in range(len(success_conditions))],
                         constraint_ids=[f"C{i+1}" for i in range(len(constraints))])
    if errs:
        raise MissionStateError(f"task list invalid: {errs}")
    plan_task = cur.execute("SELECT id FROM tasks WHERE mission_id = ? AND key = 'plan'", (mid,)).fetchone()
    cur.execute("DELETE FROM tasks WHERE id = ?", (plan_task["id"],))
    materialize_plan(cur, plan_task_id=None, plan={"tasks": tasks}, sandbox_ok=False, base_commit=None, mission_id=mid,
                     add_integrate=add_integrate, planned_by=purpose)
    emit(cur, "mission.explicit_plan", "owner" if purpose == "owner_plan" else "nexus:learning",
         {"tasks": [t["key"] for t in tasks], "purpose": purpose}, project_id=project_id, mission_id=mid)
    return mid


def grant_universal_capabilities(cur: sqlite3.Cursor) -> int:
    """One-time upgrade for agents created before every agent had internet + utilities: adds the
    universal capabilities as a new agent version (history is kept)."""
    n = 0
    for a in cur.execute("SELECT a.id, v.role_default, v.model_policy_json, v.tool_permissions_json, v.memory_scope"
                         " FROM agent_identities a JOIN agent_versions v ON v.id = a.current_version_id WHERE a.archived = 0"
                         ).fetchall():
        if a["role_default"] in NO_TOOL_ROLES:
            continue
        tools = loads(a["tool_permissions_json"], [])
        missing = [c for c in UNIVERSAL_CAPABILITIES if c not in tools]
        if missing:
            new_agent_version(cur, a["id"], role=a["role_default"], model_policy=loads(a["model_policy_json"], {}),
                              tools=tools + missing, memory_scope=a["memory_scope"])
            n += 1
    if n:
        emit(cur, "agents.capabilities_upgraded", "nexus:upgrade", {"agents": n, "added": UNIVERSAL_CAPABILITIES})
    return n
