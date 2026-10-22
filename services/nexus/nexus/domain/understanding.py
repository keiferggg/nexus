"""Shared mission model: evidence (with lineage), claims, assumptions, uncertainties, decisions,
proposals/objections, predictions, and the provenance graph used for targeted invalidation.

Independence rule: evidence items sharing a ``lineage_root`` are one source path, no matter how
many agents (or providers) read them.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from typing import Any

from ..persistence.db import dumps, loads, new_id, now_iso
from ..persistence.events import emit


# ---- provenance graph ----------------------------------------------------------------------
def link(cur: sqlite3.Cursor, src_type: str, src_id: str, dst_type: str, dst_id: str) -> None:
    cur.execute("INSERT OR IGNORE INTO dep_edges (src_type, src_id, dst_type, dst_id) VALUES (?,?,?,?)",
                (src_type, src_id, dst_type, dst_id))


def descendants(cur: sqlite3.Cursor, src_type: str, src_id: str) -> list[tuple[str, str]]:
    """BFS over dep_edges with a visited set (cycle-safe)."""
    seen: set[tuple[str, str]] = {(src_type, src_id)}
    order: list[tuple[str, str]] = []
    q = deque([(src_type, src_id)])
    while q:
        t, i = q.popleft()
        for r in cur.execute("SELECT dst_type, dst_id FROM dep_edges WHERE src_type = ? AND src_id = ?", (t, i)).fetchall():
            node = (r["dst_type"], r["dst_id"])
            if node not in seen:
                seen.add(node)
                order.append(node)
                q.append(node)
    return order


# ---- evidence ------------------------------------------------------------------------------
def register_evidence(cur: sqlite3.Cursor, *, project_id: str, mission_id: str | None, kind: str, title: str,
                      locator: str | None, artifact_id: str | None, excerpt: str | None, lineage_root: str,
                      created_by: str) -> tuple[str, bool]:
    """Returns (evidence_id, created). Re-reading the same content at the same locator in a
    mission returns the existing record rather than inflating the evidence count."""
    if artifact_id:
        sha = cur.execute("SELECT sha256 FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()["sha256"]
        # Same content at the same locator: return the existing record *whatever its status*, so a
        # withdrawn source stays withdrawn when re-read.
        existing = cur.execute(
            "SELECT e.id FROM evidence e JOIN artifacts a ON a.id = e.artifact_id WHERE e.mission_id IS ? AND"
            " e.locator IS ? AND a.sha256 = ? ORDER BY e.created_at DESC LIMIT 1", (mission_id, locator, sha)).fetchone()
        if existing:
            return existing["id"], False
        # Same locator, different content: the earlier evidence changed underneath its dependents.
        for old in cur.execute("SELECT e.id FROM evidence e WHERE e.mission_id IS ? AND e.locator IS ? AND"
                               " e.status = 'active'", (mission_id, locator)).fetchall():
            change_evidence(cur, old["id"], new_status="changed", reason=f"content at {locator} changed",
                            actor=created_by)
    eid = new_id("ev")
    cur.execute(
        "INSERT INTO evidence (id, project_id, mission_id, kind, title, locator, artifact_id, excerpt, lineage_root,"
        " status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, project_id, mission_id, kind, title, locator, artifact_id, excerpt, lineage_root, "active", created_by,
         now_iso()),
    )
    emit(cur, "evidence.registered", created_by, {"evidence_id": eid, "title": title, "lineage_root": lineage_root,
                                                   "kind": kind, "locator": locator},
         project_id=project_id, mission_id=mission_id)
    return eid, True


def lineage_summary(cur: sqlite3.Cursor, evidence_ids: list[str]) -> dict[str, Any]:
    if not evidence_ids:
        return {"items": 0, "active": 0, "independent_lineages": 0, "lineages": [], "inactive": [], "unknown": []}
    rows = {r["id"]: r for r in cur.execute(
        f"SELECT id, lineage_root, status FROM evidence WHERE id IN ({','.join('?' * len(evidence_ids))})",
        tuple(evidence_ids)).fetchall()}
    unknown = [e for e in evidence_ids if e not in rows]
    active = [rows[e] for e in evidence_ids if e in rows and rows[e]["status"] == "active"]
    inactive = [e for e in evidence_ids if e in rows and rows[e]["status"] != "active"]
    lineages = sorted({r["lineage_root"] for r in active})
    return {"items": len(set(evidence_ids)), "active": len({r["id"] for r in active}),
            "independent_lineages": len(lineages), "lineages": lineages, "inactive": inactive, "unknown": unknown}


def add_claim(cur: sqlite3.Cursor, *, project_id: str, mission_id: str | None, proposition: str,
              supports: list[str], contradicts: list[str] | None = None, created_by: str,
              valid_until: str | None = None) -> str:
    cid = new_id("clm")
    lin = lineage_summary(cur, supports)
    contra = contradicts or []
    status = "disputed" if contra else ("supported" if lin["active"] else "proposed")
    basis = (f"{lin['active']} active supporting evidence item(s) across {lin['independent_lineages']} independent"
             f" source lineage(s)" + (f"; {len(contra)} contradicting" if contra else ""))
    cur.execute(
        "INSERT INTO claims (id, project_id, mission_id, proposition, status, confidence_basis, observed_at, valid_until,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, project_id, mission_id, proposition, status, basis, now_iso(), valid_until, created_by, now_iso()),
    )
    for e in supports:
        if lin["unknown"] and e in lin["unknown"]:
            continue
        cur.execute("INSERT OR IGNORE INTO claim_evidence VALUES (?,?,?)", (cid, e, "supports"))
        link(cur, "evidence", e, "claim", cid)
    for e in contra:
        cur.execute("INSERT OR IGNORE INTO claim_evidence VALUES (?,?,?)", (cid, e, "contradicts"))
        link(cur, "evidence", e, "claim", cid)
    emit(cur, "claim.added", created_by, {"claim_id": cid, "proposition": proposition[:300], "status": status,
                                           "basis": basis}, project_id=project_id, mission_id=mission_id)
    return cid


def _recompute_claim(cur: sqlite3.Cursor, claim_id: str) -> str:
    sup = [r["evidence_id"] for r in cur.execute(
        "SELECT ce.evidence_id FROM claim_evidence ce JOIN evidence e ON e.id = ce.evidence_id WHERE ce.claim_id = ?"
        " AND ce.relation = 'supports' AND e.status = 'active'", (claim_id,)).fetchall()]
    lin = lineage_summary(cur, sup)
    status = "supported" if sup else "withdrawn"
    basis = f"{lin['active']} active supporting evidence item(s) across {lin['independent_lineages']} lineage(s) after change"
    cur.execute("UPDATE claims SET status = ?, confidence_basis = ?, is_stale = 1, version = version + 1 WHERE id = ?",
                (status, basis, claim_id))
    return status


def change_evidence(cur: sqlite3.Cursor, evidence_id: str, *, new_status: str, reason: str, actor: str) -> dict[str, Any]:
    """Withdraw or mark changed; mark only provenance descendants stale and schedule rechecks."""
    from .missions import invalidate_task

    ev = cur.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    if ev is None:
        raise KeyError(evidence_id)
    cur.execute("UPDATE evidence SET status = ?, status_reason = ?, version = version + 1 WHERE id = ?",
                (new_status, reason, evidence_id))
    affected = descendants(cur, "evidence", evidence_id)
    result: dict[str, list] = {"claims": [], "tasks": [], "decisions": [], "rechecks": []}
    for typ, nid in affected:
        if typ == "claim":
            result["claims"].append({"id": nid, "status": _recompute_claim(cur, nid)})
        elif typ == "task":
            t = cur.execute("SELECT * FROM tasks WHERE id = ?", (nid,)).fetchone()
            if t is None or t["status"] in ("canceled", "superseded"):
                continue
            why = f"evidence {evidence_id} {new_status}: {reason}"
            invalidate_task(cur, nid, reason=why, actor=actor)
            strat = loads(t["strategy_json"], {})
            strat.update({"replanned_because": why, "previous_mode": strat.get("mode"),
                          "mode": "independent_investigation" if t["kind"] == "research" else strat.get("mode", "direct"),
                          "why": f"re-verify without {evidence_id}",
                          "switch_if": "remaining evidence cannot meet checks -> blocked for owner decision"})
            cur.execute("UPDATE tasks SET strategy_json = ? WHERE id = ?", (dumps(strat), nid))
            emit(cur, "plan.changed", "nexus:invalidation", {"task_key": t["key"], "reason": why,
                                                              "strategy": strat["mode"]},
                 project_id=t["project_id"], mission_id=t["mission_id"], task_id=nid)
            result["tasks"].append(nid)
            cur.execute("INSERT OR IGNORE INTO recheck_requests (id, target_type, target_id, cause, status, created_at)"
                        " VALUES (?,?,?,?,?,?)", (new_id("rck"), "task", nid, f"evidence {evidence_id} {new_status}",
                                                  "pending", now_iso()))
            result["rechecks"].append(nid)
        elif typ == "decision":
            cur.execute("UPDATE decisions SET status = 'stale' WHERE id = ? AND status = 'active'", (nid,))
            result["decisions"].append(nid)
    emit(cur, "evidence.changed", actor, {"evidence_id": evidence_id, "status": new_status, "reason": reason,
                                          "affected": result}, project_id=ev["project_id"], mission_id=ev["mission_id"])
    return result


# ---- uncertainty, assumptions, decisions ---------------------------------------------------
COST_POINTS = {"cheap": 3, "medium": 2, "expensive": 1}
KIND_POINTS = {"conflicting_evidence": 3, "ambiguous_intent": 3, "missing_evidence": 2, "execution": 2, "explanation": 1}
URGENCY_POINTS = {"high": 3, "normal": 1, "low": 0}


def add_uncertainty(cur: sqlite3.Cursor, *, mission_id: str, kind: str, question: str, would_change: str,
                    affects: list[str], cost_hint: str = "medium", urgency: str = "normal", created_by: str) -> str:
    uid = new_id("unc")
    cur.execute("INSERT INTO uncertainties (id, mission_id, kind, question, would_change, affects_json, cost_hint, urgency,"
                " status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uid, mission_id, kind, question, would_change, dumps(affects), cost_hint, urgency, "open", created_by,
                 now_iso()))
    m = cur.execute("SELECT project_id FROM missions WHERE id = ?", (mission_id,)).fetchone()
    emit(cur, "uncertainty.added", created_by, {"uncertainty_id": uid, "kind": kind, "question": question,
                                                "would_change": would_change, "affects": affects},
         project_id=m["project_id"], mission_id=mission_id)
    return uid


def rank_uncertainties(cur: sqlite3.Cursor, mission_id: str) -> list[dict[str, Any]]:
    """Transparent rule-based ranking. Points, not probabilities: no fabricated expected values."""
    out = []
    for u in cur.execute("SELECT * FROM uncertainties WHERE mission_id = ? AND status = 'open'", (mission_id,)).fetchall():
        affects = loads(u["affects_json"], [])
        parts = {
            "dependency_impact": min(len(affects), 5) * 2,
            "kind": KIND_POINTS.get(u["kind"], 1),
            "cheap_to_check": COST_POINTS.get(u["cost_hint"], 2),
            "urgency": URGENCY_POINTS.get(u["urgency"], 1),
        }
        score = sum(parts.values())
        expl = (f"affects {len(affects)} task(s) (+{parts['dependency_impact']}), {u['kind']} (+{parts['kind']}), "
                f"{u['cost_hint']} to check (+{parts['cheap_to_check']}), urgency {u['urgency']} (+{parts['urgency']})")
        out.append({**dict(u), "affects": affects, "score": score, "score_parts": parts, "explanation": expl})
    out.sort(key=lambda d: -d["score"])
    return out


def resolve_uncertainty(cur: sqlite3.Cursor, uid: str, *, resolution: str, actor: str, status: str = "resolved") -> None:
    cur.execute("UPDATE uncertainties SET status = ?, resolution = ? WHERE id = ?", (status, resolution, uid))
    u = cur.execute("SELECT mission_id FROM uncertainties WHERE id = ?", (uid,)).fetchone()
    emit(cur, "uncertainty.resolved", actor, {"uncertainty_id": uid, "resolution": resolution, "status": status},
         mission_id=u["mission_id"])


def add_assumption(cur: sqlite3.Cursor, *, mission_id: str, statement: str, basis: str, created_by: str) -> str:
    aid = new_id("asm")
    cur.execute("INSERT INTO assumptions (id, mission_id, statement, basis, status, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?)", (aid, mission_id, statement, basis, "open", created_by, now_iso()))
    emit(cur, "assumption.added", created_by, {"assumption_id": aid, "statement": statement}, mission_id=mission_id)
    return aid


def add_decision(cur: sqlite3.Cursor, *, mission_id: str, title: str, choice: str, rationale: str, owner: str,
                 alternatives: list[str] | None = None, depends_on: list[tuple[str, str]] | None = None) -> str:
    did = new_id("dec")
    cur.execute("INSERT INTO decisions (id, mission_id, title, choice, rationale, owner, alternatives_json, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)", (did, mission_id, title, choice, rationale, owner,
                                               dumps(alternatives or []), "active", now_iso()))
    for typ, ref in depends_on or []:
        link(cur, typ, ref, "decision", did)
    emit(cur, "decision.recorded", owner, {"decision_id": did, "title": title, "choice": choice, "rationale": rationale},
         mission_id=mission_id)
    return did


# ---- proposals & objections ----------------------------------------------------------------
class ObjectionInvalid(Exception):
    pass


def add_objection(cur: sqlite3.Cursor, *, mission_id: str, target_ref: str, summary: str, mechanism: str,
                  evidence_or_test: str, resolution_condition: str, decision_owner: str, blocking: bool,
                  created_by: str, message_id: str | None = None) -> str:
    """An objection must name the mechanism, the affected target, evidence or a proposed test, and
    the condition that resolves it - otherwise it is rejected as non-actionable."""
    missing = [k for k, v in {"mechanism": mechanism, "target_ref": target_ref, "evidence_or_test": evidence_or_test,
                              "resolution_condition": resolution_condition}.items() if not (v or "").strip()]
    if missing:
        raise ObjectionInvalid(f"objection missing required fields: {missing}")
    oid = new_id("obj")
    cur.execute("INSERT INTO proposals (id, mission_id, kind, target_ref, summary, mechanism, evidence_or_test,"
                " resolution_condition, decision_owner, blocking, status, created_by, message_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, mission_id, "objection", target_ref, summary, mechanism, evidence_or_test, resolution_condition,
                 decision_owner, int(blocking), "open", created_by, message_id, now_iso()))
    emit(cur, "objection.raised", created_by, {"objection_id": oid, "target": target_ref, "summary": summary,
                                               "blocking": blocking, "resolution_condition": resolution_condition},
         mission_id=mission_id)
    return oid


def resolve_proposal(cur: sqlite3.Cursor, pid: str, *, resolution: str, actor: str, status: str = "resolved") -> None:
    p = cur.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    if p is None:
        raise KeyError(pid)
    if actor not in (p["decision_owner"], "owner"):
        raise PermissionError(f"only {p['decision_owner']} or the owner may resolve {pid}")
    cur.execute("UPDATE proposals SET status = ?, resolution = ? WHERE id = ?", (status, resolution, pid))
    emit(cur, f"{p['kind']}.{status}", actor, {"id": pid, "resolution": resolution}, mission_id=p["mission_id"])


# ---- predictions ---------------------------------------------------------------------------
def add_prediction(cur: sqlite3.Cursor, *, mission_id: str, task_id: str | None, statement: str, observable: str,
                   expected: Any, created_by: str, consequential: bool = True) -> str:
    pid = new_id("prd")
    cur.execute("INSERT INTO predictions (id, mission_id, task_id, statement, observable, expected_json, status,"
                " consequential, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, mission_id, task_id, statement, observable, dumps(expected), "open", int(consequential), created_by,
                 now_iso()))
    return pid


def observe_predictions(cur: sqlite3.Cursor, *, task_id: str, observations: dict[str, Any],
                        execution_ok: bool) -> list[dict[str, Any]]:
    """Compare open predictions with observed values. A consequential mismatch emits a surprise
    event, diagnosed as an execution failure (if the execution itself failed) or a false
    assumption (execution fine, world differed)."""
    out = []
    for p in cur.execute("SELECT * FROM predictions WHERE task_id = ? AND status = 'open'", (task_id,)).fetchall():
        if p["observable"] not in observations:
            continue
        observed = observations[p["observable"]]
        expected = loads(p["expected_json"])
        match = observed == expected
        status = "confirmed" if match else "surprised"
        diagnosis = None if match else ("execution_failure" if not execution_ok else "false_assumption")
        cur.execute("UPDATE predictions SET observed_json = ?, status = ?, diagnosis = ? WHERE id = ?",
                    (dumps(observed), status, diagnosis, p["id"]))
        if not match and p["consequential"]:
            emit(cur, "prediction.surprise", "nexus:predictions",
                 {"prediction_id": p["id"], "statement": p["statement"], "expected": expected, "observed": observed,
                  "diagnosis": diagnosis}, mission_id=p["mission_id"], task_id=task_id)
        out.append({"id": p["id"], "status": status, "diagnosis": diagnosis})
    return out
