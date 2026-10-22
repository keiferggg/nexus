"""Supervisor: scheduling, leases/heartbeats, attempt execution, cancellation and recovery.

Reconciliation protocol between the framework checkpoint store and the NEXUS database
(the two are NOT one transaction):

  1. Every attempt has an id and fencing token recorded in SQLite before any work starts.
  2. Each model/tool step has a deterministic operation id; its result is recorded in SQLite
     (model_calls/tool_operations) - replays reuse it.
  3. After each workflow segment the latest checkpoint id is recorded as a CheckpointReference.
  4. A submission is recorded idempotently (operation id = attempt:round) under the fencing
     token; acceptance is a single SQLite transaction guarded by UNIQUE(task, epoch).
  5. On startup every 'running'/'submitted' attempt belongs to a dead process: it is fenced
     (abandoned); unverified submissions become stale; in-flight model calls and tool writes
     become 'uncertain' with unresolved reservations. The task is re-queued with a pointer to
     the attempt's latest checkpoint (discovered from the checkpoint store if the reference
     was never recorded). The new attempt resumes the workflow; the pending SubmissionReady
     request is re-emitted and re-submitted under the new attempt's operation id.
  6. If the task was already accepted before the crash, nothing is resumed.
"""

from __future__ import annotations

import asyncio
import os
import json
import logging
from typing import Any

from ..appctx import AppContext, SimulatedCrash
from ..domain import rooms
from ..domain.budget import BudgetExceeded
from ..domain.execution import (
    FencedError,
    StaleSubmissionError,
    abandon_attempt,
    acquire_lease,
    begin_verification,
    decide,
    end_attempt,
    expire_leases,
    heartbeat,
    record_submission,
    start_attempt,
)
from ..domain.missions import check_mission_completion, get_mission, set_mission_status
from ..domain.tasks import TransitionError, get_task, promote_ready, transition
from ..domain.understanding import link, observe_predictions
from ..domain.verification import CITE_RE
from ..persistence.db import ConflictError, dumps, loads, new_id, now_iso
from ..persistence.events import emit
from ..providers.base import ProviderError
from .adapter import AttemptStart, GateVerdict, SubmissionReady
from .strategy import materialize_plan, maybe_switch_to_recovery, prioritize
from .verifier import Verifier
from .worker import CapabilityMissing, TaskWorker, ToolLoopExceeded

log = logging.getLogger("nexus.supervisor")

BLOCKING_PROVIDER_ERRORS = {"auth", "not_configured", "bad_request"}


class AttemptRunner:
    def __init__(self, sup: "Supervisor", task_id: str, attempt: dict[str, Any], resume_checkpoint: str | None):
        self.sup = sup
        self.ctx = sup.ctx
        self.task_id = task_id
        self.attempt = attempt
        self.resume_checkpoint = resume_checkpoint
        self.worker = TaskWorker(self.ctx, task_id=task_id, attempt=attempt)
        self.verifier = Verifier(self.ctx)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.ctx.heartbeat_s)
            try:
                with self.ctx.db.tx() as cur:
                    heartbeat(cur, self.task_id, self.attempt["attempt_id"], self.attempt["fencing_token"],
                              self.ctx.lease_ttl_ms)
            except FencedError:
                return

    async def _record_checkpoint(self) -> None:
        name = f"nexus-task-{self.task_id}"
        cp = await self.ctx.runtime.latest_checkpoint(name)
        if cp is None:
            return
        with self.ctx.db.tx() as cur:
            cur.execute("INSERT OR IGNORE INTO checkpoint_refs (id, attempt_id, workflow_name, checkpoint_id,"
                        " pending_request_ids_json, source, recorded_at) VALUES (?,?,?,?,?,?,?)",
                        (new_id("ckr"), self.attempt["attempt_id"], name, cp["checkpoint_id"],
                         dumps(cp["pending_request_ids"]), "recorded", now_iso()))

    async def run(self) -> None:
        hb = asyncio.create_task(self._heartbeat())
        aid = self.attempt["attempt_id"]
        try:
            with self.ctx.db.tx() as cur:
                start_attempt(cur, self.task_id, aid, self.attempt["fencing_token"])
                t = get_task(cur, self.task_id)
                emit(cur, "attempt.started", "nexus:supervisor",
                     {"attempt_id": aid, "attempt_no": self.attempt["attempt_no"], "fencing_token": self.attempt["fencing_token"],
                      "resumed_from_checkpoint": self.resume_checkpoint, "agent_id": t["agent_id"]},
                     project_id=t["project_id"], mission_id=t["mission_id"], task_id=self.task_id)
            wf = self.ctx.runtime.build_attempt_workflow(self.task_id, self.worker.produce)
            outcome = await self.ctx.runtime.drive(
                wf, start=None if self.resume_checkpoint else AttemptStart(self.task_id, aid),
                resume_checkpoint_id=self.resume_checkpoint, on_submission=self.gate, on_segment=self._record_checkpoint)
            if outcome is None:
                self._fail_attempt("workflow went idle without an outcome", retry=True)
        except SimulatedCrash:
            hb.cancel()
            raise  # models abrupt process death: no cleanup; the harness sees the crashed task
        except FencedError as exc:
            self._mark_fenced(str(exc))
        except asyncio.CancelledError:
            self._abandon("canceled by owner")
            hb.cancel()
            self.sup.running.pop(aid, None)
            raise
        except BudgetExceeded as exc:
            self._block(f"budget: {exc}")
        except StaleSubmissionError as exc:
            self._abandon(f"stale submission: {exc}")
        except ProviderError as exc:
            if exc.kind in BLOCKING_PROVIDER_ERRORS:
                self._block(f"provider {exc.kind}: {exc.detail}")
            else:
                self._fail_attempt(f"provider {exc.kind}: {exc.detail}", retry=True)
        except CapabilityMissing as exc:
            self._block(f"capability missing: {exc}")
        except ToolLoopExceeded as exc:
            self._fail_attempt(str(exc), retry=True)
        except Exception as exc:  # unexpected: record, never silently succeed
            log.exception("attempt %s crashed", aid)
            self._fail_attempt(f"internal error {type(exc).__name__}: {exc}", retry=True)
        hb.cancel()
        self.sup.running.pop(aid, None)
        self.ctx.notifier._on_commit()

    # ---- gate: NEXUS verification + acceptance ---------------------------------------------
    async def gate(self, request_id: str, req: SubmissionReady) -> GateVerdict:
        aid, token = self.attempt["attempt_id"], self.attempt["fencing_token"]
        op = f"{aid}:r{req.round}"
        self.ctx.faults.hit("before_submission_record")
        with self.ctx.db.tx() as cur:
            sub = record_submission(cur, task_id=self.task_id, attempt_id=aid, token=token, operation_id=op,
                                    artifact_id=req.artifact_id, round=req.round,
                                    input_versions={i["task_key"]: i.get("sha256") for i in req.meta.get("inputs", [])},
                                    meta={k: v for k, v in req.meta.items()})
            t = get_task(cur, self.task_id)
            rooms.post(cur, operation_id=f"{op}:review_request", project_id=t["project_id"], mission_id=t["mission_id"],
                       thread_id=rooms.thread_for_task(self.task_id), sender=f"agent:{t['agent_id']}",
                       recipients=["role:reviewer", "owner"], type="review_request", task_id=self.task_id,
                       body=f"Round {req.round} submitted: {req.summary}", refs={"artifact_id": req.artifact_id,
                                                                                 "submission_id": sub["id"]})
        if sub["duplicate"] and sub["status"] not in ("received",):
            # Redelivered request after a decision was already made for this operation.
            return self._verdict_from_existing(sub["id"])
        self.ctx.faults.hit("after_submission_before_verify")
        with self.ctx.db.tx() as cur:
            begin_verification(cur, sub["id"])
        await self.verifier.verify(sub["id"], self.attempt)
        cited = sorted(set(CITE_RE.findall(self.ctx.artifacts.read_text(req.artifact_id))))
        with self.ctx.db.tx() as cur:
            result = decide(cur, sub["id"], evidence_ids=cited)
            t = get_task(cur, self.task_id)
            if result["verdict"] == "accepted":
                self._post_accept(cur, t, sub["id"], req, cited)
            elif result["verdict"] == "repair":
                switched = maybe_switch_to_recovery(cur, self.task_id, result["feedback"])
                if switched and switched.get("to") == "escalated":
                    transition(cur, self.task_id, "blocked", actor="nexus:strategy",
                               reason="recovery did not change the failure; owner decision needed")
                    end_attempt(cur, aid, "failed", "escalated after recovery")
                    cur.execute("DELETE FROM leases WHERE task_id = ?", (self.task_id,))
                    result = {"verdict": "blocked", "feedback": result["feedback"]}
            observe_predictions(cur, task_id=self.task_id, execution_ok=True,
                                observations={"first_round_passes": result["verdict"] == "accepted" and req.round == 1,
                                              "accepted": result["verdict"] == "accepted", "rounds": req.round})
            rooms.post(cur, operation_id=f"{op}:verdict", project_id=t["project_id"], mission_id=t["mission_id"],
                       thread_id=rooms.thread_for_task(self.task_id), sender="nexus:verifier",
                       recipients=[f"agent:{t['agent_id']}", "owner"], type="decision", task_id=self.task_id,
                       body=_verdict_text(result), refs={"submission_id": sub["id"]})
        self.ctx.faults.hit("after_decide_before_response")
        if result["verdict"] == "accepted":
            return GateVerdict("accept", [], "accepted")
        if result["verdict"] == "repair":
            return GateVerdict("revise", result["feedback"], "repair requested")
        return GateVerdict("stop", result.get("feedback", []), result.get("reason", result["verdict"]))

    def _verdict_from_existing(self, sub_id: str) -> GateVerdict:
        s = self.ctx.db.one("SELECT * FROM submissions WHERE id = ?", (sub_id,))
        if s["status"] == "accepted":
            return GateVerdict("accept", [], "already accepted")
        if s["status"] == "rejected":
            fb = [{"criterion": r["criterion"], **loads(r["findings_json"], {})} for r in
                  self.ctx.db.query("SELECT * FROM reviews WHERE submission_id = ? AND verdict = 'fail'", (sub_id,))]
            return GateVerdict("revise", fb, "already rejected")
        return GateVerdict("stop", [], f"submission {s['status']}")

    def _post_accept(self, cur, t, submission_id: str, req: SubmissionReady, cited: list[str]) -> None:
        post_accept(self.sup, cur, t, submission_id, meta=req.meta, artifact_id=req.artifact_id, round=req.round,
                    cited=cited)

    # ---- failure handling -----------------------------------------------------------------
    def _mark_fenced(self, reason: str) -> None:
        with self.ctx.db.tx() as cur:
            cur.execute("UPDATE task_attempts SET status = 'fenced', error = ?, ended_at = ? WHERE id = ? AND status IN"
                        " ('running','submitted')", (reason, now_iso(), self.attempt["attempt_id"]))
            t = get_task(cur, self.task_id)
            emit(cur, "attempt.fenced", "nexus:supervisor", {"attempt_id": self.attempt["attempt_id"], "reason": reason,
                                                             "note": "late result discarded; not published"},
                 project_id=t["project_id"], mission_id=t["mission_id"], task_id=self.task_id)

    def _owns_lease(self, cur) -> bool:
        lease = cur.execute("SELECT attempt_id FROM leases WHERE task_id = ?", (self.task_id,)).fetchone()
        return lease is not None and lease["attempt_id"] == self.attempt["attempt_id"]

    def _abandon(self, reason: str) -> None:
        with self.ctx.db.tx() as cur:
            a = cur.execute("SELECT status FROM task_attempts WHERE id = ?", (self.attempt["attempt_id"],)).fetchone()
            if a and a["status"] in ("running", "submitted") and self._owns_lease(cur):
                abandon_attempt(cur, self.attempt["attempt_id"], reason=reason, actor="nexus:supervisor")

    def _block(self, reason: str) -> None:
        with self.ctx.db.tx() as cur:
            if not self._owns_lease(cur):
                return
            t = get_task(cur, self.task_id)
            end_attempt(cur, self.attempt["attempt_id"], "failed", reason)
            cur.execute("DELETE FROM leases WHERE task_id = ?", (self.task_id,))
            if t["status"] in ("running", "verifying"):
                transition(cur, self.task_id, "blocked", actor="nexus:supervisor", reason=reason)
            emit(cur, "attention.task_blocked", "nexus:supervisor", {"reason": reason, "task_key": t["key"]},
                 project_id=t["project_id"], mission_id=t["mission_id"], task_id=self.task_id)

    def _fail_attempt(self, reason: str, *, retry: bool) -> None:
        with self.ctx.db.tx() as cur:
            if not self._owns_lease(cur):
                return
            abandon_attempt(cur, self.attempt["attempt_id"], reason=reason, actor="nexus:supervisor")
            cur.execute("UPDATE task_attempts SET status = 'failed' WHERE id = ?", (self.attempt["attempt_id"],))


def post_accept(sup: "Supervisor", cur, t, submission_id: str, *, meta: dict[str, Any], artifact_id: str, round: int,
                cited: list[str]) -> None:
    """Runs inside the acceptance transaction: provenance edges, plan materialization, episodic memory."""
    for e in cited:
        link(cur, "evidence", e, "task", t["id"])
    for c in meta.get("claims", []):
        link(cur, "claim", c, "task", t["id"])
    for inp in meta.get("inputs", []):
        dep = cur.execute("SELECT id FROM tasks WHERE mission_id = ? AND key = ?", (t["mission_id"], inp["task_key"])).fetchone()
        if dep:
            link(cur, "task", dep["id"], "task", t["id"])
    if t["kind"] == "plan":
        plan = json.loads(sup.ctx.artifacts.read_text(artifact_id))
        materialize_plan(cur, plan_task_id=t["id"], plan=plan,
                         sandbox_ok=sup.sandbox_ok_by_project.get(t["project_id"], sup.sandbox_ok),
                         base_commit=sup.base_commits.get(t["project_id"]))
    acc = cur.execute("SELECT id FROM acceptances WHERE submission_id = ?", (submission_id,)).fetchone()
    mem_id = new_id("mem")
    text = f"Accepted '{t['title']}' ({t['kind']}) after {round} round(s)."
    cur.execute("INSERT INTO memory_items (id, project_id, layer, access, mission_id, content, source_refs_json,"
                " circumstances, status, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (mem_id, t["project_id"], "episodic", "team", t["mission_id"], text, dumps([f"acceptance:{acc['id']}"]),
                 f"strategy={loads(t['strategy_json'], {}).get('mode')}", "active", "nexus:supervisor", now_iso(),
                 now_iso()))
    cur.execute("INSERT INTO memory_fts (content, item_id) VALUES (?, ?)", (text, mem_id))


def _verdict_text(result: dict[str, Any]) -> str:
    v = result["verdict"]
    if v == "accepted":
        return "Accepted: all required checks passed and no blocking objection is open."
    fb = "; ".join(f"{f.get('criterion')}: {f.get('summary')}" for f in result.get("feedback", [])[:6])
    if v == "repair":
        return f"Rejected, repair requested. Failed checks: {fb}"
    return f"{v.capitalize()}: {result.get('reason') or fb}"


class Supervisor:
    def __init__(self, ctx: AppContext, *, holder: str = "nexus-local"):
        self.ctx = ctx
        self.holder = holder
        self.running: dict[str, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.sandbox_ok = False
        self.sandbox_ok_by_project: dict[str, bool] = {}  # projects may use their own sandbox image
        self._sandbox_checked_at = 0.0
        self.base_commits: dict[str, str] = {}
        self.last_error: str | None = None

    # ---- lifecycle -------------------------------------------------------------------------
    async def start(self) -> dict[str, Any]:
        self.ctx.notifier.bind_loop(asyncio.get_running_loop())
        self.sandbox_ok = (await self.ctx.sandbox.available())[0]
        if self.ctx.services is not None:
            self.ctx.services.connectors.on_settled = self.recheck_task
            self.ctx.services.connectors.on_violation = self.on_guard_violation
        from ..domain.missions import ensure_team

        from ..domain.missions import grant_universal_capabilities

        with self.ctx.db.tx() as cur:
            projects = [p["id"] for p in cur.execute("SELECT id FROM projects").fetchall()]
            for pid in projects:
                ensure_team(cur, pid)
            if cur.execute("SELECT 1 FROM settings WHERE key = 'upgrade.universal_capabilities_v1'").fetchone() is None:
                grant_universal_capabilities(cur)
                cur.execute("INSERT INTO settings VALUES ('upgrade.universal_capabilities_v1', 'true', ?)", (now_iso(),))
        if self.ctx.services is not None:
            from ..connectors.service import ensure_web_connector

            for pid in projects:
                await ensure_web_connector(self.ctx.services.connectors, pid)
        report = await self.reconcile_on_startup()
        if os.environ.get("NEXUS_ACTIVITY_LOG", "1") != "0":
            from ..activity_log import ActivityLog

            self.activity = ActivityLog(self.ctx)
            self.activity.start()
        self._loop_task = asyncio.create_task(self._loop())
        return report

    async def stop(self) -> None:
        self._stop.set()
        if getattr(self, "activity", None) is not None:
            await self.activity.stop()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in list(self.running.values()):
            t.cancel()
        if self.running:
            await asyncio.gather(*self.running.values(), return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
                self.last_error = None
            except SimulatedCrash:
                raise
            except Exception as exc:  # keep scheduling; surface the error in diagnostics
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("supervisor tick failed")
            await self.ctx.notifier.wait(self.ctx.tick_s)

    async def reconcile_on_startup(self) -> dict[str, Any]:
        report: dict[str, Any] = {"abandoned_attempts": [], "uncertain_model_calls": 0, "uncertain_tool_ops": 0,
                                  "reconciled_checkpoints": 0, "canceled_missions": []}
        with self.ctx.db.tx() as cur:
            n = cur.execute("UPDATE model_calls SET status = 'uncertain', error_kind = 'process_terminated',"
                            " error_detail = 'service stopped during call; provider may have billed', ended_at = ?"
                            " WHERE status IN ('reserved','running')", (now_iso(),)).rowcount
            report["uncertain_model_calls"] = n
            cur.execute("UPDATE budget_reservations SET status = 'unresolved', updated_at = ? WHERE status = 'reserved'",
                        (now_iso(),))
            m = cur.execute("UPDATE tool_operations SET status = 'uncertain', decision_reason = 'service stopped while"
                            " running; outcome unknown', updated_at = ? WHERE status = 'running' AND effect_class IN"
                            " ('external_write','destructive')", (now_iso(),)).rowcount
            cur.execute("UPDATE tool_operations SET status = 'failed', decision_reason = 'service stopped while running',"
                        " updated_at = ? WHERE status = 'running'", (now_iso(),))
            report["uncertain_tool_ops"] = m
            attempts = cur.execute("SELECT * FROM task_attempts WHERE status IN ('running','submitted')").fetchall()
        for a in attempts:
            name = f"nexus-task-{a['task_id']}"
            cp = await self.ctx.runtime.latest_checkpoint(name)
            with self.ctx.db.tx() as cur:
                if cp and not cur.execute("SELECT 1 FROM checkpoint_refs WHERE attempt_id = ? AND checkpoint_id = ?",
                                          (a["id"], cp["checkpoint_id"])).fetchone():
                    cur.execute("INSERT INTO checkpoint_refs (id, attempt_id, workflow_name, checkpoint_id,"
                                " pending_request_ids_json, source, recorded_at) VALUES (?,?,?,?,?,?,?)",
                                (new_id("ckr"), a["id"], name, cp["checkpoint_id"], dumps(cp["pending_request_ids"]),
                                 "reconciled", now_iso()))
                    report["reconciled_checkpoints"] += 1
                t = get_task(cur, a["task_id"])
                abandon_attempt(cur, a["id"], reason="service restarted: attempt's process is gone",
                                actor="nexus:reconciler")
                report["abandoned_attempts"].append({"attempt_id": a["id"], "task_key": t["key"],
                                                     "task_status_was": t["status"],
                                                     "checkpoint": cp["checkpoint_id"] if cp else None})
            # Leases of tasks that were never started
        with self.ctx.db.tx() as cur:
            for lease in cur.execute("SELECT * FROM leases").fetchall():
                abandon_attempt(cur, lease["attempt_id"], reason="service restarted", actor="nexus:reconciler")
            for m in cur.execute("SELECT id FROM missions WHERE status = 'canceling'").fetchall():
                set_mission_status(cur, m["id"], "canceled", actor="nexus:reconciler",
                                   reason="cancel completed after restart", allowed_from={"canceling"})
                report["canceled_missions"].append(m["id"])
            emit(cur, "service.reconciled", "nexus:reconciler", report)
        return report

    def resume_point(self, task_id: str) -> str | None:
        """Latest checkpoint of the most recent abandoned attempt of the current epoch, if the
        task has not been invalidated since (a new epoch starts fresh)."""
        r = self.ctx.db.one(
            "SELECT c.checkpoint_id, c.pending_request_ids_json FROM checkpoint_refs c JOIN task_attempts a ON a.id = c.attempt_id"
            " JOIN tasks t ON t.id = a.task_id WHERE a.task_id = ? AND a.status = 'abandoned' AND a.epoch = t.epoch"
            " AND a.mission_revision = t.mission_revision AND a.attempt_no = (SELECT MAX(attempt_no) FROM task_attempts"
            " WHERE task_id = ?) ORDER BY c.recorded_at DESC LIMIT 1", (task_id, task_id))
        if r is None or not loads(r["pending_request_ids_json"], []):
            return None  # only resume suspended (submission-pending) checkpoints; otherwise restart cleanly
        return r["checkpoint_id"]

    # ---- scheduling ------------------------------------------------------------------------
    async def refresh_sandbox_status(self) -> None:
        """Per-project sandbox availability (each project's profile may name a different image). At most once a
        minute: a missing image or a stopped Docker Desktop is re-checked without restarting NEXUS."""
        import time

        from ..tools.sandbox import project_profile

        if time.monotonic() - self._sandbox_checked_at < 60:
            return
        self._sandbox_checked_at = time.monotonic()
        for p in self.ctx.db.query("SELECT id FROM projects"):
            image = project_profile(self.ctx.db, p["id"])["image"]
            self.sandbox_ok_by_project[p["id"]] = (await self.ctx.sandbox.available(image=image))[0]

    async def tick(self) -> None:
        await self.refresh_sandbox_status()
        if self.ctx.services is not None:
            from ..connectors.service import ensure_web_connector

            for p in self.ctx.db.query("SELECT id FROM projects WHERE id NOT IN (SELECT project_id FROM connectors WHERE"
                                       " kind = 'web')"):
                await ensure_web_connector(self.ctx.services.connectors, p["id"])
        with self.ctx.db.tx() as cur:
            expire_leases(cur)
            rooms.expire_overdue(cur)
        missions = self.ctx.db.query("SELECT * FROM missions WHERE status IN ('running','canceling')")
        for m in missions:
            if m["status"] == "canceling":
                self._finish_cancel(m["id"])
                continue
            with self.ctx.db.tx() as cur:
                promote_ready(cur, m["id"])
                check_mission_completion(cur, m["id"])
            m = self.ctx.db.one("SELECT * FROM missions WHERE id = ?", (m["id"],))
            if m["status"] != "running":
                continue
            active = self.ctx.db.one("SELECT COUNT(*) AS n FROM tasks WHERE mission_id = ? AND status IN"
                                     " ('leased','running','submitted','verifying')", (m["id"],))["n"]
            capacity = m["max_active_workers"] - active
            if capacity <= 0:
                continue
            ready = self.ctx.db.query("SELECT * FROM tasks WHERE mission_id = ? AND status = 'ready'"
                                      " ORDER BY priority DESC, created_at LIMIT ?", (m["id"], capacity))
            for t in ready:
                await self._launch(t)

    async def _launch(self, t) -> None:
        if t["kind"] in ("code", "integrate"):
            proj = self.ctx.db.one("SELECT workspace_root FROM projects WHERE id = ?", (t["project_id"],))
            if t["project_id"] not in self.base_commits:
                from pathlib import Path

                if await self.ctx.worktrees.is_repo(Path(proj["workspace_root"])):
                    self.base_commits[t["project_id"]] = await self.ctx.worktrees.head(Path(proj["workspace_root"]))
        resume = self.resume_point(t["id"])
        try:
            with self.ctx.db.tx() as cur:
                att = acquire_lease(cur, t["id"], holder=self.holder, ttl_ms=self.ctx.lease_ttl_ms,
                                    agent_id=t["agent_id"], resume_checkpoint=resume)
        except ConflictError:
            return
        runner = AttemptRunner(self, t["id"], att, resume)
        self.running[att["attempt_id"]] = asyncio.create_task(runner.run(), name=f"attempt-{att['attempt_id']}")

    def _finish_cancel(self, mission_id: str) -> None:
        active = [aid for aid, task in self.running.items() if not task.done() and
                  self.ctx.db.one("SELECT 1 FROM task_attempts a JOIN tasks t ON t.id = a.task_id WHERE a.id = ? AND"
                                  " t.mission_id = ?", (aid, mission_id))]
        for aid in active:
            self.running[aid].cancel()
        if active:
            return
        with self.ctx.db.tx() as cur:
            for t in cur.execute("SELECT id, status FROM tasks WHERE mission_id = ? AND status NOT IN"
                                 " ('accepted','canceled','superseded')", (mission_id,)).fetchall():
                try:
                    transition(cur, t["id"], "canceled", actor="nexus:supervisor", reason="mission canceled")
                except TransitionError:
                    pass
            unresolved = cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(reserved_usd),0) AS usd FROM budget_reservations"
                                     " WHERE mission_id = ? AND status = 'unresolved'", (mission_id,)).fetchone()
            uncertain_ops = cur.execute("SELECT COUNT(*) AS n FROM tool_operations WHERE mission_id = ? AND status ="
                                        " 'uncertain'", (mission_id,)).fetchone()["n"]
            set_mission_status(cur, mission_id, "canceled", actor="nexus:supervisor",
                               reason=(f"canceled; {unresolved['n']} unresolved charge(s) up to ${unresolved['usd']:.4f}"
                                       f" and {uncertain_ops} uncertain operation(s) retained. Cancellation does not"
                                       f" guarantee a provider stopped billing."), allowed_from={"canceling"})

    def owner_review(self, submission_id: str, *, criterion: str, verdict: str, note: str) -> dict[str, Any]:
        from ..domain.execution import owner_review

        with self.ctx.db.tx() as cur:
            result = owner_review(cur, submission_id, criterion=criterion, verdict=verdict, note=note)
            if result["verdict"] == "accepted":
                s = cur.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
                t = get_task(cur, s["task_id"])
                cited = sorted(set(CITE_RE.findall(self.ctx.artifacts.read_text(s["artifact_id"]))))
                post_accept(self, cur, t, submission_id, meta=loads(s["meta_json"], {}), artifact_id=s["artifact_id"],
                            round=s["round"], cited=cited)
                m = get_mission(cur, t["mission_id"])
                if m["status"] == "blocked":
                    set_mission_status(cur, m["id"], "running", actor="owner", reason="owner approval unblocked work",
                                       allowed_from={"blocked"})
        return result

    def recheck_task(self, task_id: str) -> dict[str, Any] | None:
        """Called when a task's requested actions settle: re-run the action criteria of its latest
        inconclusive submission and re-apply the acceptance policy."""
        from ..domain.execution import reverify
        from ..domain.verification import run_check

        t = self.ctx.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if t is None or t["status"] != "blocked":
            return None
        s = self.ctx.db.one("SELECT * FROM submissions WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                            (task_id,))
        if s is None or s["status"] != "inconclusive":
            return None
        plan = [p for p in loads(t["verification_plan_json"], []) if (p.get("check") or {}).get("kind") == "actions_settled"]
        if not plan:
            return None
        meta = {**loads(s["meta_json"], {}), "_task_id": task_id, "_epoch": s["epoch"]}
        with self.ctx.db.tx() as cur:
            results = {}
            for p in plan:
                r = run_check(cur, p["check"], content="", mission_id=t["mission_id"], submission_meta=meta, worktree=None)
                results[p["criterion"]] = {"check_kind": "actions_settled", "verdict": r["verdict"], "summary": r["summary"]}
            if all(r["verdict"] == "inconclusive" for r in results.values()):
                return {"verdict": "still_waiting"}
            out = reverify(cur, s["id"], results=results)
            if out.get("verdict") == "accepted":
                cited = sorted(set(CITE_RE.findall(self.ctx.artifacts.read_text(s["artifact_id"]))))
                post_accept(self, cur, get_task(cur, task_id), s["id"], meta=loads(s["meta_json"], {}),
                            artifact_id=s["artifact_id"], round=s["round"], cited=cited)
            m = get_mission(cur, t["mission_id"])
            if m["status"] == "blocked" and out.get("verdict") in ("accepted", "repair", "repair_new_attempt"):
                set_mission_status(cur, m["id"], "running", actor="nexus:connectors", reason="requested actions settled",
                                   allowed_from={"blocked"})
        self.ctx.notifier._on_commit()
        return out

    def on_guard_violation(self, attempt_id: str, task_id: str | None) -> None:
        """Circuit breaker: an attempt whose actions keep being denied by the unattended guard is
        probably being steered. Suspend its task for owner review; other tasks keep running."""
        from ..guard.action_guard import autonomy_settings

        a = self.ctx.db.one("SELECT * FROM task_attempts WHERE id = ?", (attempt_id,))
        if a is None or a["status"] not in ("running", "submitted"):
            return
        t = self.ctx.db.one("SELECT * FROM tasks WHERE id = ?", (a["task_id"],))
        limit = int(autonomy_settings(self.ctx.db, t["project_id"])["suspend_after_violations"])
        n = self.ctx.db.one("SELECT COUNT(*) AS n FROM guard_decisions WHERE attempt_id = ? AND decision = 'deny'",
                            (attempt_id,))["n"]
        if n < limit:
            return
        reason = f"suspended: {n} actions denied by the unattended guard (possible steering); review before resuming"
        with self.ctx.db.tx() as cur:
            abandon_attempt(cur, attempt_id, reason=reason, actor="nexus:guard")
            cur_t = get_task(cur, a["task_id"])
            if cur_t["status"] == "ready":
                transition(cur, a["task_id"], "blocked", actor="nexus:guard", reason=reason)
            emit(cur, "attention.task_suspended", "nexus:guard", {"reason": reason, "task_key": t["key"], "attempt_id": attempt_id},
                 project_id=t["project_id"], mission_id=t["mission_id"], task_id=t["id"])

    def retry_task(self, task_id: str, *, actor: str = "owner") -> None:
        """Owner retry of a blocked/failed task: a fresh attempt budget in the same epoch."""
        with self.ctx.db.tx() as cur:
            t = get_task(cur, task_id)
            if t["status"] not in ("blocked", "failed"):
                raise ConflictError(f"task is {t['status']}; only blocked or failed tasks can be retried")
            cur.execute("UPDATE tasks SET attempt_count = 0 WHERE id = ?", (task_id,))
            transition(cur, task_id, "ready", actor=actor, reason="owner requested retry")
            m = get_mission(cur, t["mission_id"])
            if m["status"] == "blocked":
                set_mission_status(cur, m["id"], "running", actor=actor, reason="owner retried a task",
                                   allowed_from={"blocked"})

    async def cancel_mission(self, mission_id: str) -> None:
        from ..domain.missions import cancel_mission

        with self.ctx.db.tx() as cur:
            ids = cancel_mission(cur, mission_id)
        for aid in ids:
            task = self.running.get(aid)
            if task:
                task.cancel()
        if ids:
            await asyncio.gather(*[self.running[a] for a in ids if a in self.running], return_exceptions=True)
        self._finish_cancel(mission_id)

    async def wait_idle(self, mission_id: str, timeout: float = 60.0) -> str:
        """Test helper: tick until the mission is no longer running or nothing is runnable."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            await self.tick()
            if self.running:
                tasks_ = list(self.running.values())
                done, _ = await asyncio.wait(tasks_, timeout=0.2)
                for d in done:
                    if not d.cancelled() and isinstance(d.exception(), SimulatedCrash):
                        raise d.exception()
            m = self.ctx.db.one("SELECT status FROM missions WHERE id = ?", (mission_id,))
            if m["status"] not in ("running", "canceling"):
                return m["status"]
            busy = self.ctx.db.one("SELECT COUNT(*) AS n FROM tasks WHERE mission_id = ? AND status IN"
                                   " ('ready','leased','running','submitted','verifying')", (mission_id,))["n"]
            if not busy and not self.running:
                drafts = self.ctx.db.one("SELECT COUNT(*) AS n FROM tasks WHERE mission_id = ? AND status = 'draft'",
                                         (mission_id,))["n"]
                await self.tick()
                if not drafts and not self.running:
                    return self.ctx.db.one("SELECT status FROM missions WHERE id = ?", (mission_id,))["status"]
            await asyncio.sleep(0.01)
        raise TimeoutError(f"mission {mission_id} still busy after {timeout}s")
