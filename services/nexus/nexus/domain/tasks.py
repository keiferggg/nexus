"""Task lifecycle state machine.

Model agents never call these functions directly: they propose; application code validates and
commits. Every transition names the actor class allowed to perform it, its preconditions, and
emits an event in the same transaction.

Actor classes:
  app      - NEXUS application code (scheduler, verifier, reconciler)
  owner    - the human owner via the UI/API
  agent:*  - a model agent. Agents may not perform any transition themselves.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..persistence.db import ConflictError, dumps, loads, now_iso
from ..persistence.events import emit

TERMINAL = {"accepted", "canceled", "superseded", "failed"}
ACTIVE = {"leased", "running", "submitted", "verifying"}


@dataclass(frozen=True)
class Rule:
    actors: frozenset[str]
    description: str


def _r(actors: str, desc: str) -> Rule:
    return Rule(frozenset(actors.split(",")), desc)


# (from, to) -> rule.  This table is the specification of legal transitions.
TRANSITIONS: dict[tuple[str, str], Rule] = {
    ("draft", "ready"): _r("app,owner", "all required dependencies accepted+current or waived"),
    ("draft", "blocked"): _r("app", "a dependency failed/canceled or a capability is missing"),
    ("ready", "leased"): _r("app", "scheduler acquired a lease with a new fencing token"),
    ("ready", "draft"): _r("app", "a dependency became stale or was reopened"),
    ("leased", "running"): _r("app", "attempt started under the current fencing token"),
    ("leased", "ready"): _r("app", "lease expired or released before start"),
    ("running", "submitted"): _r("app", "worker submission recorded with valid fencing token and revision"),
    ("running", "ready"): _r("app", "attempt abandoned (lease expiry/crash); retry permitted"),
    ("running", "failed"): _r("app", "attempt failed and attempt budget exhausted"),
    ("running", "blocked"): _r("app", "budget, capability, provider or uncertain side effect blocks progress"),
    ("submitted", "verifying"): _r("app", "verification plan execution started"),
    ("submitted", "ready"): _r("app", "submission abandoned with its attempt"),
    ("verifying", "accepted"): _r("app", "all required checks passed and no blocking objection is open"),
    ("verifying", "running"): _r("app", "verification failed; repair requested within attempt"),
    ("verifying", "blocked"): _r("app", "verification inconclusive and no automatic repair path"),
    ("verifying", "failed"): _r("app", "repair rounds exhausted"),
    ("verifying", "ready"): _r("app", "attempt abandoned during verification"),
    ("blocked", "ready"): _r("app,owner", "blocking condition resolved"),
    ("ready", "blocked"): _r("app", "suspended for owner review (e.g. repeated guard violations)"),
    ("blocked", "verifying"): _r("owner,app", "owner supplied the missing verification (e.g. approval)"),
    ("blocked", "draft"): _r("app,owner", "waiting on dependencies again"),
    ("failed", "ready"): _r("owner,app", "owner requested retry, or strategy replan"),
    ("accepted", "draft"): _r("app,owner", "accepted result became stale; task reopened in a new epoch"),
    ("accepted", "ready"): _r("app,owner", "accepted result became stale; task reopened in a new epoch"),
}
for _s in ("draft", "ready", "leased", "running", "submitted", "verifying", "blocked", "failed"):
    TRANSITIONS[(_s, "canceled")] = _r("app,owner", "mission or task canceled")
    TRANSITIONS[(_s, "superseded")] = _r("app,owner", "mission revision invalidated this task")
for _s in ("leased", "submitted"):
    TRANSITIONS[(_s, "failed")] = _r("app", "attempt abandoned and attempt budget exhausted")
TRANSITIONS[("accepted", "superseded")] = _r("app,owner", "mission revision invalidated this task")


class TransitionError(Exception):
    pass


def actor_class(actor: str) -> str:
    if actor == "owner":
        return "owner"
    if actor.startswith("nexus:"):
        return "app"
    return "agent"


def get_task(cur: sqlite3.Cursor, task_id: str) -> sqlite3.Row:
    row = cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    return row


def transition(
    cur: sqlite3.Cursor,
    task_id: str,
    to: str,
    *,
    actor: str,
    reason: str,
    expected_version: int | None = None,
    expected_from: set[str] | None = None,
    extra: dict[str, Any] | None = None,
    causal_parent: str | None = None,
) -> sqlite3.Row:
    t = get_task(cur, task_id)
    frm = t["status"]
    if expected_from is not None and frm not in expected_from:
        raise ConflictError(f"task {task_id} is {frm}, expected one of {sorted(expected_from)}")
    if expected_version is not None and t["version"] != expected_version:
        raise ConflictError(f"task {task_id} version {t['version']} != expected {expected_version}")
    rule = TRANSITIONS.get((frm, to))
    if rule is None:
        raise TransitionError(f"illegal transition {frm} -> {to} for task {task_id}")
    cls = actor_class(actor)
    if cls not in rule.actors:
        raise TransitionError(f"actor {actor} ({cls}) may not perform {frm} -> {to}")
    sets = {"status": to, "status_reason": reason, "updated_at": now_iso()}
    if extra:
        sets.update(extra)
    cols = ", ".join(f"{k} = ?" for k in sets)
    cur.execute(
        f"UPDATE tasks SET {cols}, version = version + 1 WHERE id = ? AND version = ?",
        (*sets.values(), task_id, t["version"]),
    )
    if cur.rowcount != 1:
        raise ConflictError(f"concurrent update on task {task_id}")
    emit(
        cur,
        "task.transition",
        actor,
        {"from": frm, "to": to, "reason": reason, "rule": rule.description, "key": t["key"], "title": t["title"]},
        project_id=t["project_id"],
        mission_id=t["mission_id"],
        task_id=task_id,
        causal_parent=causal_parent,
    )
    return get_task(cur, task_id)


def dependencies_satisfied(cur: sqlite3.Cursor, task_id: str) -> tuple[bool, list[str], bool]:
    """Returns (satisfied, unmet_keys, any_dependency_dead)."""
    rows = cur.execute(
        "SELECT d.waived, t.status, t.is_stale, t.key FROM dependencies d JOIN tasks t ON t.id = d.depends_on_task_id"
        " WHERE d.task_id = ?",
        (task_id,),
    ).fetchall()
    unmet, dead = [], False
    for r in rows:
        if r["waived"]:
            continue
        if r["status"] == "accepted" and not r["is_stale"]:
            continue
        if r["status"] in ("failed", "canceled", "superseded"):
            dead = True
        unmet.append(r["key"])
    return (not unmet, unmet, dead)


def promote_ready(cur: sqlite3.Cursor, mission_id: str) -> list[str]:
    """draft -> ready where dependencies are valid; draft -> blocked if a dependency is dead."""
    promoted = []
    for t in cur.execute("SELECT id FROM tasks WHERE mission_id = ? AND status = 'draft'", (mission_id,)).fetchall():
        ok, unmet, dead = dependencies_satisfied(cur, t["id"])
        if ok:
            transition(cur, t["id"], "ready", actor="nexus:scheduler", reason="dependencies satisfied")
            promoted.append(t["id"])
        elif dead:
            transition(cur, t["id"], "blocked", actor="nexus:scheduler", reason=f"dependency not completable: {unmet}")
    return promoted


def task_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for k in ("acceptance_json", "verification_plan_json", "strategy_json", "cause_json"):
        d[k.removesuffix("_json")] = loads(d.pop(k), [] if k in ("acceptance_json", "verification_plan_json") else {})
    d["is_stale"] = bool(d["is_stale"])
    return d


def waive_dependency(cur: sqlite3.Cursor, task_id: str, dep_task_id: str, *, actor: str) -> None:
    if actor != "owner":
        raise TransitionError("only the owner may waive a dependency")
    cur.execute(
        "UPDATE dependencies SET waived = 1, waived_by = ? WHERE task_id = ? AND depends_on_task_id = ?",
        (actor, task_id, dep_task_id),
    )
    t = get_task(cur, task_id)
    emit(cur, "task.dependency_waived", actor, {"depends_on": dep_task_id}, project_id=t["project_id"],
         mission_id=t["mission_id"], task_id=task_id)


def transitions_spec() -> list[dict[str, Any]]:
    return [
        {"from": f, "to": t, "actors": sorted(r.actors), "precondition": r.description}
        for (f, t), r in sorted(TRANSITIONS.items())
    ]


__all__ = ["transition", "promote_ready", "TRANSITIONS", "TransitionError", "task_to_dict", "dumps"]
