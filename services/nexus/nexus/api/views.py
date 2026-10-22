"""Read models (projections) for the API. Everything is computed from recorded rows."""

from __future__ import annotations

from typing import Any

from ..domain.budget import summary as budget_summary
from ..domain.missions import current_revision
from ..domain.tasks import task_to_dict
from ..domain.understanding import lineage_summary, rank_uncertainties
from ..persistence.db import Database, loads

AGENT_STATE = {
    "draft": "waiting for dependencies",
    "ready": "queued",
    "leased": "starting",
    "running": "working",
    "submitted": "submitted for verification",
    "verifying": "under verification",
    "accepted": "done",
    "blocked": "blocked",
    "failed": "failed",
    "canceled": "canceled",
    "superseded": "superseded",
}


def mission_view(db: Database, mission_id: str) -> dict[str, Any]:
    m = db.one("SELECT * FROM missions WHERE id = ?", (mission_id,))
    if m is None:
        raise KeyError(mission_id)
    with db.tx() as cur:
        rev = current_revision(cur, mission_id)
        budget = budget_summary(cur, mission_id)
    tasks = [task_to_dict(t) for t in db.query("SELECT * FROM tasks WHERE mission_id = ? ORDER BY created_at", (mission_id,))]
    deps = [dict(r) for r in db.query("SELECT d.* FROM dependencies d JOIN tasks t ON t.id = d.task_id WHERE t.mission_id = ?",
                                      (mission_id,))]
    agents = {a["id"]: dict(a) for a in db.query(
        "SELECT a.id, a.name, v.role_default FROM agent_identities a JOIN agent_versions v ON v.id = a.current_version_id")}
    by_task: dict[str, list[str]] = {}
    for d in deps:
        by_task.setdefault(d["task_id"], []).append(d["depends_on_task_id"])
    for t in tasks:
        t["depends_on"] = by_task.get(t["id"], [])
        a = agents.get(t["agent_id"] or "")
        t["agent"] = {"id": a["id"], "name": a["name"], "role": a["role_default"]} if a else None
        t["activity"] = AGENT_STATE.get(t["status"], t["status"])
        if t["status"] == "verifying":
            t["activity"] = "running checks"
        if t["status"] == "draft" and t["depends_on"]:
            pending = [x["key"] for x in tasks if x["id"] in t["depends_on"] and x["status"] != "accepted"]
            t["activity"] = f"blocked by dependency: {', '.join(pending)}" if pending else "dependencies satisfied"
        acc = db.one("SELECT id, sha256, artifact_id, is_current, noncurrent_reason FROM acceptances WHERE task_id = ?"
                     " ORDER BY created_at DESC LIMIT 1", (t["id"],))
        t["latest_acceptance"] = dict(acc) if acc else None
    final = next((t for t in tasks if t["kind"] == "integrate" and t["status"] == "accepted" and not t["is_stale"]), None)
    revisions = [dict(r) for r in db.query("SELECT revision, reason, created_by, created_at FROM mission_revisions"
                                           " WHERE mission_id = ? ORDER BY revision", (mission_id,))]
    project = db.one("SELECT id, name, is_demo, workspace_root FROM projects WHERE id = ?", (m["project_id"],))
    return {
        "mission": dict(m),
        "project": dict(project),
        "revision": rev,
        "revisions": revisions,
        "tasks": tasks,
        "budget": budget,
        "final_artifact_id": final["current_artifact_id"] if final else None,
        "counts": {s: sum(1 for t in tasks if t["status"] == s) for s in AGENT_STATE},
        "is_simulated": m["execution_mode"] == "offline_fake",
    }


def task_detail(db: Database, task_id: str) -> dict[str, Any]:
    t = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if t is None:
        raise KeyError(task_id)
    d = task_to_dict(t)
    d["attempts"] = [dict(a) for a in db.query("SELECT * FROM task_attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,))]
    subs = []
    for s in db.query("SELECT * FROM submissions WHERE task_id = ? ORDER BY created_at", (task_id,)):
        sd = dict(s)
        sd["meta"] = loads(sd.pop("meta_json"), {})
        sd["input_versions"] = loads(sd.pop("input_versions_json"), {})
        sd["reviews"] = [{**dict(r), "findings": loads(r["findings_json"], {})} for r in
                         db.query("SELECT * FROM reviews WHERE submission_id = ? ORDER BY created_at", (s["id"],))]
        for r in sd["reviews"]:
            r.pop("findings_json", None)
        subs.append(sd)
    d["submissions"] = subs
    d["acceptances"] = [dict(a) for a in db.query("SELECT * FROM acceptances WHERE task_id = ? ORDER BY created_at", (task_id,))]
    d["model_calls"] = [dict(c) for c in db.query(
        "SELECT id, purpose, provider_kind, model, status, error_kind, input_tokens, output_tokens, actual_usd,"
        " usage_reported, context_artifact_id, output_artifact_id, started_at, ended_at FROM model_calls WHERE task_id = ?"
        " ORDER BY started_at", (task_id,))]
    d["tool_operations"] = [{**dict(o), "args": loads(o["args_json"], {})} for o in db.query(
        "SELECT id, tool, effect_class, status, decision_reason, args_json, created_at FROM tool_operations WHERE task_id = ?"
        " ORDER BY created_at", (task_id,))]
    d["dependencies"] = [dict(r) for r in db.query(
        "SELECT t.id, t.key, t.title, t.status, d.waived FROM dependencies d JOIN tasks t ON t.id = d.depends_on_task_id"
        " WHERE d.task_id = ?", (task_id,))]
    d["why"] = why_running(db, task_id)
    return d


def why_running(db: Database, task_id: str) -> list[dict[str, Any]]:
    """Causal chain from recorded data: task cause -> success condition / revision / evidence
    change, plus the strategy rationale."""
    t = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    cause = loads(t["cause_json"], {})
    chain = []
    rev = db.one("SELECT * FROM mission_revisions WHERE mission_id = ? AND revision = ?", (t["mission_id"], cause.get(
        "revision") or t["mission_revision"]))
    if cause.get("addresses") and rev:
        sc = next((c for c in loads(rev["success_conditions_json"], []) if c["id"] == cause["addresses"]), None)
        chain.append({"kind": "success_condition", "ref": cause["addresses"],
                      "text": sc["text"] if sc else ("combine accepted work" if cause["addresses"] == "integration"
                                                     else cause["addresses"])})
    if cause.get("addresses") == "objective" and rev:
        chain.append({"kind": "objective", "text": rev["objective"]})
    if cause.get("addresses") == "mission_revision":
        chain.append({"kind": "mission_revision", "text": cause.get("reason")})
    if cause.get("planned_by"):
        chain.append({"kind": "planned_by", "ref": cause["planned_by"]})
    for c in cause.get("constraint_ids", []):
        chain.append({"kind": "constraint", "ref": c})
    strat = loads(t["strategy_json"], {})
    if strat:
        chain.append({"kind": "strategy", "text": f"{strat.get('mode')}: {strat.get('why')}",
                      "switch_if": strat.get("switch_if")})
    if t["is_stale"] or t["stale_reason"]:
        chain.append({"kind": "reopened", "text": t["stale_reason"]})
    for u in db.query("SELECT * FROM uncertainties WHERE mission_id = ? AND status = 'open'", (t["mission_id"],)):
        if t["key"] in loads(u["affects_json"], []):
            chain.append({"kind": "uncertainty", "ref": u["id"], "text": u["question"]})
    return chain


def understanding(db: Database, mission_id: str, since_seq: int = 0) -> dict[str, Any]:
    evidence = [dict(e) for e in db.query("SELECT id, kind, title, locator, lineage_root, status, status_reason, created_by,"
                                          " created_at FROM evidence WHERE mission_id = ? ORDER BY created_at", (mission_id,))]
    lineages: dict[str, list[str]] = {}
    for e in evidence:
        lineages.setdefault(e["lineage_root"], []).append(e["id"])
    claims = []
    for c in db.query("SELECT * FROM claims WHERE mission_id = ? ORDER BY created_at", (mission_id,)):
        sup = [r["evidence_id"] for r in db.query("SELECT evidence_id FROM claim_evidence WHERE claim_id = ? AND"
                                                  " relation = 'supports'", (c["id"],))]
        with db.tx() as cur:
            lin = lineage_summary(cur, sup)
        claims.append({**dict(c), "supports": sup, "independent_lineages": lin["independent_lineages"]})
    with db.tx() as cur:
        ranked = rank_uncertainties(cur, mission_id)
    since = db.query("SELECT * FROM events WHERE mission_id = ? AND seq > ? AND type IN ('evidence.changed',"
                     " 'claim.added','task.invalidated','mission.revised','plan.changed','prediction.surprise',"
                     " 'strategy.switched','objection.raised','decision.recorded') ORDER BY seq", (mission_id, since_seq))
    return {
        "evidence": evidence,
        "lineages": [{"lineage_root": k, "evidence_ids": v, "count": len(v)} for k, v in lineages.items()],
        "claims": claims,
        "assumptions": [dict(a) for a in db.query("SELECT * FROM assumptions WHERE mission_id = ?", (mission_id,))],
        "uncertainties": ranked + [dict(u) for u in db.query("SELECT * FROM uncertainties WHERE mission_id = ? AND status"
                                                              " != 'open'", (mission_id,))],
        "decisions": [dict(d) for d in db.query("SELECT * FROM decisions WHERE mission_id = ?", (mission_id,))],
        "proposals": [dict(p) for p in db.query("SELECT * FROM proposals WHERE mission_id = ? ORDER BY created_at",
                                                (mission_id,))],
        "predictions": [dict(p) for p in db.query("SELECT * FROM predictions WHERE mission_id = ?", (mission_id,))],
        "changes": [{"seq": e["seq"], "type": e["type"], "payload": loads(e["payload_json"], {}), "at": e["created_at"]}
                    for e in since],
    }


def attention(db: Database, mission_id: str | None = None) -> list[dict[str, Any]]:
    """What needs the owner: blocked/failed tasks, owner approvals, open blocking objections,
    expired messages, uncertain operations, unresolved spend."""
    q = " AND t.mission_id = ?" if mission_id else ""
    p: tuple = (mission_id,) if mission_id else ()
    items = []
    for t in db.query(f"SELECT t.* FROM tasks t WHERE t.status IN ('blocked','failed'){q}", p):
        awaiting = db.one("SELECT s.id, r.criterion FROM submissions s JOIN reviews r ON r.submission_id = s.id WHERE"
                          " s.task_id = ? AND s.status = 'inconclusive' AND r.verdict = 'inconclusive' AND"
                          " r.check_kind = 'owner_approval' ORDER BY s.created_at DESC LIMIT 1", (t["id"],))
        waiting_actions = db.one("SELECT COUNT(*) AS n FROM action_requests WHERE task_id = ? AND status IN"
                                 " ('pending','approved','executing','uncertain')", (t["id"],))["n"]
        if waiting_actions and t["status"] == "blocked":
            # The action approval itself is listed below; retrying the task would only re-request it.
            items.append({"kind": "task_awaiting_action", "task_id": t["id"], "mission_id": t["mission_id"],
                          "title": t["title"], "detail": f"waiting on {waiting_actions} requested action(s)"})
            continue
        if awaiting is None and t["status"] == "blocked":
            # Verification could not decide (e.g. no independent reviewer): the owner may judge it instead of a retry.
            awaiting = db.one("SELECT s.id, r.criterion FROM submissions s JOIN reviews r ON r.submission_id = s.id WHERE"
                              " s.task_id = ? AND s.status = 'inconclusive' AND r.verdict = 'inconclusive' AND r.blocking = 1"
                              " AND s.id = (SELECT id FROM submissions WHERE task_id = s.task_id ORDER BY created_at DESC,"
                              " rowid DESC LIMIT 1) LIMIT 1", (t["id"],))
            awaiting_owner = False
        else:
            awaiting_owner = awaiting is not None
        items.append({"kind": "owner_approval" if awaiting_owner else f"task_{t['status']}", "task_id": t["id"],
                      "mission_id": t["mission_id"], "title": t["title"], "detail": t["status_reason"],
                      "submission_id": awaiting["id"] if awaiting else None,
                      "criterion": awaiting["criterion"] if awaiting else None})
    mq = " AND mission_id = ?" if mission_id else ""
    for o in db.query(f"SELECT * FROM proposals WHERE kind = 'objection' AND status = 'open' AND blocking = 1{mq}", p):
        items.append({"kind": "blocking_objection", "mission_id": o["mission_id"], "proposal_id": o["id"],
                      "title": o["summary"], "detail": o["resolution_condition"]})
    for op in db.query(f"SELECT * FROM tool_operations WHERE status = 'uncertain'{mq}", p):
        items.append({"kind": "uncertain_operation", "mission_id": op["mission_id"], "tool_operation_id": op["id"],
                      "title": f"{op['tool']} outcome unknown", "detail": op["decision_reason"]})
    for c in db.query(f"SELECT * FROM model_calls WHERE status = 'uncertain'{mq}", p):
        items.append({"kind": "unresolved_charge", "mission_id": c["mission_id"], "model_call_id": c["id"],
                      "title": f"{c['provider_kind']} call {c['purpose']} may have been billed",
                      "detail": c["error_detail"]})
    aq = " AND ar.mission_id = ?" if mission_id else ""
    for a in db.query("SELECT ar.*, c.label FROM action_requests ar JOIN connectors c ON c.id = ar.connector_id WHERE"
                      f" ar.status IN ('pending','uncertain'){aq} ORDER BY ar.created_at", p):
        items.append({"kind": "action_approval" if a["status"] == "pending" else "action_uncertain",
                      "mission_id": a["mission_id"], "task_id": a["task_id"], "action_request_id": a["id"],
                      "effect_class": a["effect_class"], "title": f"{a['label']}: {a['action']} ({a['effect_class']})",
                      "detail": a["rationale"] if a["status"] == "pending" else a["error"],
                      "args": loads(a["args_json"], {})})
    for s in db.query(f"SELECT * FROM events WHERE type = 'attention.task_suspended'{mq} ORDER BY seq DESC LIMIT 20", p):
        t = db.one("SELECT status FROM tasks WHERE id = ?", (s["task_id"],))
        if t and t["status"] == "blocked":
            items.append({"kind": "task_suspended", "mission_id": s["mission_id"], "task_id": s["task_id"],
                          "title": f"Suspended: {loads(s['payload_json'], {}).get('task_key')}",
                          "detail": loads(s["payload_json"], {}).get("reason")})
    if not mission_id:
        for o in db.query("SELECT * FROM objective_proposals WHERE status = 'proposed' ORDER BY created_at"):
            items.append({"kind": "objective_proposed", "mission_id": None, "objective_id": o["id"],
                          "title": f"Proposed objective: {o['title']}", "detail": o["rationale"]})
        for t in db.query("SELECT * FROM tool_definitions WHERE status = 'tested' ORDER BY created_at"):
            items.append({"kind": "tool_approval", "mission_id": None, "tool_definition_id": t["id"],
                          "title": f"New tool awaiting approval: {t['name']} v{t['version']} ({t['effect_class']})",
                          "detail": t["description"]})
    for e in db.query(f"SELECT * FROM events WHERE type = 'attention.message_expired'{mq} ORDER BY seq DESC LIMIT 20", p):
        items.append({"kind": "message_expired", "mission_id": e["mission_id"], "title": "request expired",
                      "detail": loads(e["payload_json"], {}).get("body")})
    return items
