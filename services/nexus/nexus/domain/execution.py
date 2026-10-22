"""Leases with fencing tokens, execution attempts, submissions, reviews and acceptance.

Invariants enforced here (not in prompts):
  * a submission is accepted only under the attempt's *current* fencing token, the task's
    current epoch and the mission's current revision;
  * at most one acceptance per (task, epoch) - enforced by a UNIQUE constraint as well;
  * workers never accept: accept() requires an application actor;
  * failed or inconclusive verification never becomes success.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from ..persistence.db import ConflictError, dumps, loads, new_id, now_iso
from ..persistence.events import emit
from .tasks import actor_class, get_task, transition


class FencedError(Exception):
    """The caller's fencing token is no longer current: its result must not be published."""


class StaleSubmissionError(Exception):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def acquire_lease(cur: sqlite3.Cursor, task_id: str, *, holder: str, ttl_ms: int, agent_id: str | None,
                  resume_checkpoint: str | None = None) -> dict[str, Any]:
    t = get_task(cur, task_id)
    if t["status"] != "ready":
        raise ConflictError(f"task {task_id} not ready ({t['status']})")
    if cur.execute("SELECT 1 FROM leases WHERE task_id = ?", (task_id,)).fetchone():
        raise ConflictError(f"task {task_id} already leased")
    token = t["fencing_seq"] + 1
    # attempt_no is monotonic per task (history); attempt_count is the per-epoch retry budget.
    attempt_no = cur.execute("SELECT COALESCE(MAX(attempt_no), 0) + 1 AS n FROM task_attempts WHERE task_id = ?",
                             (task_id,)).fetchone()["n"]
    attempt_id = new_id("att")
    mission = cur.execute("SELECT current_revision FROM missions WHERE id = ?", (t["mission_id"],)).fetchone()
    cur.execute(
        "INSERT INTO task_attempts (id, task_id, attempt_no, fencing_token, epoch, mission_revision, agent_id, status,"
        " workflow_name, resumed_from_checkpoint, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, task_id, attempt_no, token, t["epoch"], mission["current_revision"], agent_id, "running",
         f"nexus-task-{task_id}", resume_checkpoint, now_iso()),
    )
    cur.execute(
        "INSERT INTO leases (task_id, attempt_id, holder, fencing_token, expires_at_ms, heartbeat_at) VALUES (?,?,?,?,?,?)",
        (task_id, attempt_id, holder, token, now_ms() + ttl_ms, now_iso()),
    )
    transition(cur, task_id, "leased", actor="nexus:scheduler", reason=f"lease to {holder} token={token}",
               extra={"fencing_seq": token, "attempt_count": t["attempt_count"] + 1})
    return {"attempt_id": attempt_id, "fencing_token": token, "attempt_no": attempt_no, "epoch": t["epoch"],
            "mission_revision": mission["current_revision"]}


def check_fence(cur: sqlite3.Cursor, task_id: str, attempt_id: str, token: int) -> sqlite3.Row:
    lease = cur.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()
    if lease is None or lease["attempt_id"] != attempt_id or lease["fencing_token"] != token:
        raise FencedError(f"attempt {attempt_id} token {token} is not the current lease holder for {task_id}")
    if lease["expires_at_ms"] < now_ms():
        raise FencedError(f"lease for attempt {attempt_id} expired")
    return lease


def start_attempt(cur: sqlite3.Cursor, task_id: str, attempt_id: str, token: int) -> None:
    check_fence(cur, task_id, attempt_id, token)
    transition(cur, task_id, "running", actor="nexus:scheduler", reason=f"attempt {attempt_id} started",
               expected_from={"leased"})


def heartbeat(cur: sqlite3.Cursor, task_id: str, attempt_id: str, token: int, ttl_ms: int) -> None:
    check_fence(cur, task_id, attempt_id, token)
    cur.execute("UPDATE leases SET expires_at_ms = ?, heartbeat_at = ? WHERE task_id = ?",
                (now_ms() + ttl_ms, now_iso(), task_id))


def _release(cur: sqlite3.Cursor, task_id: str, attempt_id: str) -> None:
    cur.execute("DELETE FROM leases WHERE task_id = ? AND attempt_id = ?", (task_id, attempt_id))


def end_attempt(cur: sqlite3.Cursor, attempt_id: str, status: str, error: str | None = None) -> None:
    cur.execute("UPDATE task_attempts SET status = ?, error = COALESCE(?, error), ended_at = ? WHERE id = ?"
                " AND status IN ('running','submitted')", (status, error, now_iso(), attempt_id))


def abandon_attempt(cur: sqlite3.Cursor, attempt_id: str, *, reason: str, actor: str = "nexus:reconciler") -> str:
    """Fence an attempt (crash, expiry, cancel). Returns the task's resulting status."""
    a = cur.execute("SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if a is None:
        raise KeyError(attempt_id)
    t = get_task(cur, a["task_id"])
    _release(cur, a["task_id"], attempt_id)
    end_attempt(cur, attempt_id, "abandoned", reason)
    # Unverified submissions of a fenced attempt can never be accepted.
    cur.execute("UPDATE submissions SET status = 'stale', status_reason = ? WHERE attempt_id = ?"
                " AND status IN ('received','verifying')", (f"attempt abandoned: {reason}", attempt_id))
    emit(cur, "attempt.abandoned", actor, {"attempt_id": attempt_id, "reason": reason, "fencing_token": a["fencing_token"]},
         project_id=t["project_id"], mission_id=t["mission_id"], task_id=t["id"])
    if t["status"] in ("leased", "running", "submitted", "verifying"):
        if t["attempt_count"] >= t["max_attempts"]:
            transition(cur, t["id"], "failed", actor=actor, reason=f"attempt abandoned: {reason}; attempts exhausted")
        else:
            transition(cur, t["id"], "ready", actor=actor, reason=f"attempt abandoned: {reason}")
    return get_task(cur, t["id"])["status"]


def expire_leases(cur: sqlite3.Cursor) -> list[str]:
    expired = cur.execute("SELECT * FROM leases WHERE expires_at_ms < ?", (now_ms(),)).fetchall()
    out = []
    for lease in expired:
        abandon_attempt(cur, lease["attempt_id"], reason="lease expired", actor="nexus:lease-monitor")
        out.append(lease["attempt_id"])
    return out


def record_submission(
    cur: sqlite3.Cursor,
    *,
    task_id: str,
    attempt_id: str,
    token: int,
    operation_id: str,
    artifact_id: str,
    round: int,
    input_versions: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Idempotent by operation_id. Raises FencedError / StaleSubmissionError."""
    existing = cur.execute("SELECT * FROM submissions WHERE operation_id = ?", (operation_id,)).fetchone()
    if existing is not None:
        return {"id": existing["id"], "duplicate": True, "status": existing["status"]}
    check_fence(cur, task_id, attempt_id, token)
    t = get_task(cur, task_id)
    a = cur.execute("SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)).fetchone()
    m = cur.execute("SELECT current_revision FROM missions WHERE id = ?", (t["mission_id"],)).fetchone()
    sid = new_id("sub")
    stale_reason = None
    if a["mission_revision"] != m["current_revision"]:
        stale_reason = f"built against mission revision {a['mission_revision']}, current is {m['current_revision']}"
    elif a["epoch"] != t["epoch"]:
        stale_reason = f"built for task epoch {a['epoch']}, current is {t['epoch']}"
    cur.execute(
        "INSERT INTO submissions (id, operation_id, task_id, attempt_id, fencing_token, epoch, mission_revision,"
        " artifact_id, input_versions_json, meta_json, round, status, status_reason, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, operation_id, task_id, attempt_id, token, a["epoch"], a["mission_revision"], artifact_id,
         dumps(input_versions), dumps(meta or {}), round, "stale" if stale_reason else "received", stale_reason, now_iso()),
    )
    emit(cur, "submission.received", "nexus:gate",
         {"submission_id": sid, "artifact_id": artifact_id, "round": round, "stale_reason": stale_reason},
         project_id=t["project_id"], mission_id=t["mission_id"], task_id=task_id)
    if stale_reason:
        raise StaleSubmissionError(stale_reason)
    transition(cur, task_id, "submitted", actor="nexus:gate", reason=f"submission {sid} round {round}",
               expected_from={"running"})
    cur.execute("UPDATE task_attempts SET status = 'submitted' WHERE id = ?", (attempt_id,))
    return {"id": sid, "duplicate": False, "status": "received"}


def begin_verification(cur: sqlite3.Cursor, submission_id: str) -> None:
    s = cur.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    check_fence(cur, s["task_id"], s["attempt_id"], s["fencing_token"])
    cur.execute("UPDATE submissions SET status = 'verifying' WHERE id = ?", (submission_id,))
    transition(cur, s["task_id"], "verifying", actor="nexus:verifier", reason=f"verifying {submission_id}",
               expected_from={"submitted"})


def record_review(
    cur: sqlite3.Cursor,
    *,
    submission_id: str,
    criterion: str | None,
    check_kind: str,
    reviewer: str,
    independence: str,
    verdict: str,
    findings: dict[str, Any],
    raw_artifact_id: str | None = None,
    blocking: bool = True,
    limitations: str | None = None,
) -> str:
    s = cur.execute("SELECT task_id FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    rid = new_id("rev")
    cur.execute(
        "INSERT INTO reviews (id, submission_id, task_id, criterion, check_kind, reviewer, independence, verdict,"
        " blocking, findings_json, raw_artifact_id, limitations, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, submission_id, s["task_id"], criterion, check_kind, reviewer, independence, verdict, int(blocking),
         dumps(findings), raw_artifact_id, limitations, now_iso()),
    )
    t = get_task(cur, s["task_id"])
    emit(cur, "review.recorded", reviewer,
         {"review_id": rid, "submission_id": submission_id, "criterion": criterion, "check": check_kind,
          "verdict": verdict, "independence": independence, "summary": findings.get("summary")},
         project_id=t["project_id"], mission_id=t["mission_id"], task_id=t["id"])
    return rid


def open_blocking_objections(cur: sqlite3.Cursor, task_id: str) -> list[sqlite3.Row]:
    return cur.execute(
        "SELECT * FROM proposals WHERE kind = 'objection' AND blocking = 1 AND status IN ('open','escalated')"
        " AND target_ref = ?",
        (f"task:{task_id}",),
    ).fetchall()


def decide(cur: sqlite3.Cursor, submission_id: str, *, actor: str = "nexus:verifier",
           evidence_ids: list[str] | None = None, owner_resume: bool = False) -> dict[str, Any]:
    """Apply the acceptance policy to the recorded reviews of a submission.

    Returns {"verdict": "accepted"|"repair"|"blocked"|"failed", "feedback": [...]}."""
    if actor_class(actor) == "agent":
        raise PermissionError("agents cannot accept their own (or any) task")
    s = cur.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    t = get_task(cur, s["task_id"])
    if not owner_resume:
        check_fence(cur, t["id"], s["attempt_id"], s["fencing_token"])
    m = cur.execute("SELECT * FROM missions WHERE id = ?", (t["mission_id"],)).fetchone()
    if s["mission_revision"] != m["current_revision"] or s["epoch"] != t["epoch"]:
        cur.execute("UPDATE submissions SET status='stale', status_reason='revision/epoch changed' WHERE id=?", (submission_id,))
        raise StaleSubmissionError("submission no longer matches current revision/epoch")
    reviews = cur.execute("SELECT * FROM reviews WHERE submission_id = ?", (submission_id,)).fetchall()
    plan = loads(t["verification_plan_json"], [])
    required = [p for p in plan if p.get("required", True)]
    by_criterion: dict[str, list[sqlite3.Row]] = {}
    for r in reviews:
        by_criterion.setdefault(r["criterion"] or "", []).append(r)
    failures, inconclusive, missing = [], [], []
    for p in required:
        rs = by_criterion.get(p["criterion"], [])
        if not rs:
            missing.append(p["criterion"])
            continue
        if any(r["verdict"] == "fail" and r["blocking"] for r in rs):
            failures.extend({"criterion": p["criterion"], **loads(r["findings_json"], {})} for r in rs if r["verdict"] == "fail")
        elif any(r["verdict"] == "inconclusive" for r in rs) and not any(r["verdict"] == "pass" for r in rs):
            inconclusive.append(p["criterion"])
    # Non-plan blocking reviews (e.g. owner objection review) also count.
    for r in reviews:
        if r["criterion"] not in {p["criterion"] for p in plan} and r["blocking"] and r["verdict"] == "fail":
            failures.append({"criterion": r["criterion"], **loads(r["findings_json"], {})})
    objections = open_blocking_objections(cur, t["id"])
    if not plan:
        missing.append("<no verification plan>")
    if failures:
        outcome = "repair"
    elif missing or inconclusive:
        outcome = "inconclusive"
    elif objections:
        outcome = "blocked_objection"
    else:
        outcome = "accepted"

    if outcome == "accepted":
        art = cur.execute("SELECT sha256 FROM artifacts WHERE id = ?", (s["artifact_id"],)).fetchone()
        aid = new_id("acc")
        try:
            cur.execute(
                "INSERT INTO acceptances (id, task_id, epoch, submission_id, artifact_id, sha256, mission_revision,"
                " input_versions_json, review_ids_json, evidence_ids_json, accepted_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, t["id"], t["epoch"], submission_id, s["artifact_id"], art["sha256"], s["mission_revision"],
                 s["input_versions_json"], dumps([r["id"] for r in reviews]), dumps(evidence_ids or []), actor, now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"task {t['id']} epoch {t['epoch']} already accepted") from exc
        cur.execute("UPDATE submissions SET status = 'accepted' WHERE id = ?", (submission_id,))
        transition(cur, t["id"], "accepted", actor=actor, reason=f"all {len(required)} required checks passed",
                   expected_from={"verifying"}, extra={"current_artifact_id": s["artifact_id"], "is_stale": 0,
                                                       "stale_reason": None})
        end_attempt(cur, s["attempt_id"], "completed")
        _release(cur, t["id"], s["attempt_id"])
        emit(cur, "task.accepted", actor, {"acceptance_id": aid, "artifact_id": s["artifact_id"], "sha256": art["sha256"]},
             project_id=t["project_id"], mission_id=t["mission_id"], task_id=t["id"])
        return {"verdict": "accepted", "acceptance_id": aid, "feedback": []}

    if outcome == "repair":
        cur.execute("UPDATE submissions SET status = 'rejected', status_reason = ? WHERE id = ?",
                    (f"{len(failures)} check(s) failed", submission_id))
        if s["round"] >= m["max_revision_rounds"]:
            transition(cur, t["id"], "failed", actor=actor, reason=f"repair rounds exhausted ({s['round']})")
            end_attempt(cur, s["attempt_id"], "failed", "repair rounds exhausted")
            _release(cur, t["id"], s["attempt_id"])
            return {"verdict": "failed", "feedback": failures}
        if owner_resume:
            # The original attempt already ended (task was blocked): repair needs a fresh attempt,
            # which receives these failures as previous_failures in its context packet.
            transition(cur, t["id"], "ready", actor=actor,
                       reason=f"re-verification failed ({len(failures)} check(s)); new attempt scheduled")
            return {"verdict": "repair_new_attempt", "feedback": failures}
        transition(cur, t["id"], "running", actor=actor, reason=f"repair requested: {len(failures)} failed check(s)")
        cur.execute("UPDATE task_attempts SET status = 'running' WHERE id = ?", (s["attempt_id"],))
        return {"verdict": "repair", "feedback": failures}

    # Inconclusive or blocked by objection: explicit blocked status, never silent success.
    why = "; ".join(f"{c}: {loads(r['findings_json'], {}).get('summary')}" for c in inconclusive
                    for r in by_criterion.get(c, []) if r["verdict"] == "inconclusive")
    reason = (f"verification inconclusive — {why or ''}{' missing checks: ' + str(missing) if missing else ''}".strip(" —")
              if outcome == "inconclusive" else f"blocking objection(s) open: {[o['id'] for o in objections]}")
    cur.execute("UPDATE submissions SET status = 'inconclusive', status_reason = ? WHERE id = ?", (reason, submission_id))
    transition(cur, t["id"], "blocked", actor=actor, reason=reason)
    end_attempt(cur, s["attempt_id"], "failed", reason)
    _release(cur, t["id"], s["attempt_id"])
    return {"verdict": "blocked", "feedback": [{"criterion": c, "summary": "inconclusive"} for c in missing + inconclusive],
            "reason": reason}


def owner_review(cur: sqlite3.Cursor, submission_id: str, *, criterion: str, verdict: str, note: str) -> dict[str, Any]:
    """Owner records a verdict for a criterion automation could not decide (e.g. tests needing a
    sandbox); the acceptance policy is then re-applied to that same submission."""
    if verdict not in ("pass", "fail"):
        raise ValueError("verdict must be pass or fail")
    s = cur.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    if s is None:
        raise KeyError(submission_id)
    t = get_task(cur, s["task_id"])
    latest = cur.execute("SELECT id FROM submissions WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                         (t["id"],)).fetchone()
    if s["status"] != "inconclusive" or latest["id"] != submission_id or t["status"] != "blocked":
        raise ConflictError("owner review applies only to the task's latest inconclusive submission while blocked")
    if s["epoch"] != t["epoch"]:
        raise StaleSubmissionError("submission is from an earlier task epoch")
    cur.execute("UPDATE reviews SET blocking = 0 WHERE submission_id = ? AND criterion = ? AND verdict = 'inconclusive'",
                (submission_id, criterion))
    record_review(cur, submission_id=submission_id, criterion=criterion, check_kind="owner_approval", reviewer="owner",
                  independence="owner", verdict=verdict, findings={"summary": note or f"owner {verdict}"},
                  blocking=True, limitations="human judgment")
    cur.execute("UPDATE submissions SET status = 'verifying' WHERE id = ?", (submission_id,))
    transition(cur, t["id"], "verifying", actor="owner", reason=f"owner review of {criterion}: {verdict}")
    if verdict == "fail":
        cur.execute("UPDATE submissions SET status = 'rejected', status_reason = 'owner rejected' WHERE id = ?",
                    (submission_id,))
        transition(cur, t["id"], "blocked", actor="owner", reason=f"owner rejected {criterion}: {note}")
        return {"verdict": "blocked", "feedback": [{"criterion": criterion, "summary": note}]}
    return decide(cur, submission_id, actor="nexus:verifier", owner_resume=True)


def reverify(cur: sqlite3.Cursor, submission_id: str, *, results: dict[str, dict[str, Any]],
             actor: str = "nexus:verifier") -> dict[str, Any]:
    """Re-apply the acceptance policy to a blocked task's latest inconclusive submission after the
    inputs of some criteria changed (e.g. a requested action was approved and executed).
    ``results`` maps criterion -> {"check_kind", "verdict", "summary"}. Superseded inconclusive
    reviews stay in history but stop blocking."""
    s = cur.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    t = get_task(cur, s["task_id"])
    latest = cur.execute("SELECT id FROM submissions WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                         (t["id"],)).fetchone()
    if s["status"] != "inconclusive" or latest["id"] != submission_id or t["status"] != "blocked" or s["epoch"] != t["epoch"]:
        return {"verdict": "skipped", "reason": "not the task's latest inconclusive submission"}
    for crit, r in results.items():
        cur.execute("UPDATE reviews SET blocking = 0 WHERE submission_id = ? AND criterion = ? AND verdict = 'inconclusive'",
                    (submission_id, crit))
        record_review(cur, submission_id=submission_id, criterion=crit, check_kind=r["check_kind"], reviewer=actor,
                      independence="deterministic", verdict=r["verdict"], findings={"summary": r.get("summary")},
                      blocking=True)
    cur.execute("UPDATE submissions SET status = 'verifying' WHERE id = ?", (submission_id,))
    transition(cur, t["id"], "verifying", actor="nexus:verifier", reason=f"re-verifying {sorted(results)}")
    return decide(cur, submission_id, actor="nexus:verifier", owner_resume=True)
