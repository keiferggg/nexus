"""Acceptance scenarios 2-6: commit-gap recovery, duplicate delivery, expired worker,
review rejection, changing requirements."""
import asyncio

import pytest

from helpers import events, new_mission, tasks
from scripts import SlowFirst, make_script, one_task_plan
from nexus.appctx import SimulatedCrash, build_context
from nexus.bootstrap import create_demo_project, ensure_fake_provider
from nexus.domain import rooms
from nexus.domain.execution import FencedError, StaleSubmissionError, decide
from nexus.domain.missions import revise_mission
from nexus.orchestration.adapter import SubmissionReady
from nexus.orchestration.supervisor import AttemptRunner, Supervisor
from nexus.providers.fake import FakeProvider
from nexus.secrets import MemorySecretStore
from nexus.tools.sandbox import UnavailableSandbox


def acceptances(ctx, mid):
    return ctx.db.query("SELECT a.*, t.key FROM acceptances a JOIN tasks t ON t.id = a.task_id WHERE t.mission_id = ?",
                        (mid,))


async def _crash_then_recover(ctx, tmp_path, point):
    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid)
    s1 = Supervisor(ctx)
    await s1.start()
    s1._loop_task.cancel()
    ctx.faults.armed.add(point)
    with pytest.raises(SimulatedCrash):
        await s1.wait_idle(mid, timeout=30)
    # Abrupt death: drop the in-process runners without any cleanup handlers running.
    for t in list(s1.running.values()):
        t.cancel()
    await asyncio.gather(*s1.running.values(), return_exceptions=True)
    s1.running.clear()
    ctx.close()
    ctx2 = build_context(ctx.data_dir, secrets=MemorySecretStore(), sandbox=UnavailableSandbox("test"))
    ensure_fake_provider(ctx2)
    s2 = Supervisor(ctx2)
    report = await s2.start()
    s2._loop_task.cancel()
    status = await s2.wait_idle(mid, timeout=60)
    await s2.stop()
    return ctx2, mid, status, report


@pytest.mark.parametrize("point", ["after_submission_before_verify", "after_decide_before_response",
                                   "before_submission_record"])
async def test_commit_gap_recovery_no_double_acceptance(ctx, tmp_path, point):
    ctx2, mid, status, report = await _crash_then_recover(ctx, tmp_path, point)
    try:
        assert status == "completed"
        accs = acceptances(ctx2, mid)
        keys = [a["key"] for a in accs]
        assert len(keys) == len(set(keys)), f"double acceptance: {keys}"
        assert len(tasks(ctx2, mid)) == len(keys)
        if point == "after_decide_before_response":
            # Acceptance had committed before the crash: the task is not re-executed.
            plan = tasks(ctx2, mid)["plan"]
            assert ctx2.db.one("SELECT COUNT(*) AS n FROM task_attempts WHERE task_id = ?", (plan["id"],))["n"] == 1
        else:
            assert report["abandoned_attempts"], "the crashed attempt must be reconciled"
        if point != "after_decide_before_response":
            # The suspended submission was resumed from the framework checkpoint, not redone.
            resumed = ctx2.db.query("SELECT * FROM task_attempts WHERE resumed_from_checkpoint IS NOT NULL")
            assert resumed, "expected the new attempt to resume from the recorded checkpoint"
    finally:
        ctx2.close()


async def test_duplicate_message_and_completion_delivery(ctx, sup):
    ensure_fake_provider(ctx, script=make_script(plan_fn=one_task_plan()))
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid, success=["Write a summary"])
    with ctx.db.tx() as cur:
        a, c1 = rooms.post(cur, operation_id="op-1", project_id=pid, mission_id=mid, thread_id="t", sender="owner",
                           recipients=["role:writer"], type="question", body="hi")
        b, c2 = rooms.post(cur, operation_id="op-1", project_id=pid, mission_id=mid, thread_id="t", sender="owner",
                           recipients=["role:writer"], type="question", body="hi")
    assert a == b and c1 and not c2
    assert ctx.db.one("SELECT COUNT(*) AS n FROM messages WHERE operation_id='op-1'")["n"] == 1

    # Redeliver a completion (SubmissionReady) to the gate twice for the same attempt/round.
    orig_gate = AttemptRunner.gate
    seen = {}

    async def double_gate(self, rid, req):
        v1 = await orig_gate(self, rid, req)
        v2 = await orig_gate(self, rid, req)  # duplicate delivery
        seen.setdefault(self.task_id, []).append((v1.action, v2.action))
        return v1

    AttemptRunner.gate = double_gate
    try:
        status = await sup.wait_idle(mid, timeout=30)
    finally:
        AttemptRunner.gate = orig_gate
    assert status == "completed", {k: (v["status"], v["status_reason"]) for k, v in tasks(ctx, mid).items()}
    for t in tasks(ctx, mid).values():
        n = ctx.db.one("SELECT COUNT(*) AS n FROM submissions WHERE task_id = ?", (t["id"],))["n"]
        assert n == 1, f"{t['key']} has {n} submissions"
    assert all(v == ("accept", "accept") for vs in seen.values() for v in vs)
    assert len(acceptances(ctx, mid)) == len(tasks(ctx, mid))


async def test_expired_worker_cannot_commit_late_result(ctx, sup):
    inner = FakeProvider(make_script(plan_fn=one_task_plan()))
    ensure_fake_provider(ctx)
    fake_id = ctx.db.one("SELECT id FROM providers WHERE kind='fake'")["id"]
    ctx.registry.register_override(fake_id, SlowFirst(inner, "writer", 1.5))
    ctx.heartbeat_s = 100  # the slow worker never heartbeats -> its lease expires
    ctx.lease_ttl_ms = 700
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid, success=["Write a summary"])
    status = await sup.wait_idle(mid, timeout=30)
    if sup.running:
        await asyncio.gather(*sup.running.values(), return_exceptions=True)
    assert status == "completed"
    draft = tasks(ctx, mid)["draft"]
    attempts = ctx.db.query("SELECT * FROM task_attempts WHERE task_id = ? ORDER BY attempt_no", (draft["id"],))
    assert len(attempts) >= 2
    assert attempts[0]["status"] in ("fenced", "abandoned") and attempts[0]["fencing_token"] < attempts[-1]["fencing_token"]
    subs = ctx.db.query("SELECT * FROM submissions WHERE task_id = ?", (draft["id"],))
    assert all(s["attempt_id"] != attempts[0]["id"] for s in subs), "the expired attempt must not publish a submission"
    assert len([a for a in acceptances(ctx, mid) if a["key"] == "draft"]) == 1
    assert events(ctx, mid, "attempt.fenced"), "the late result should be recorded as fenced"


async def test_deterministic_check_failure_denies_acceptance_and_repairs(ctx, sup):
    def writer(req, packet, n):
        if not packet.get("previous_failures"):
            return {"content_markdown": "I am done. Trust me.", "summary": "complete!"}  # claims completion
        return {"content_markdown": "word " * 30, "summary": "expanded after feedback"}

    ensure_fake_provider(ctx, script=make_script(plan_fn=one_task_plan(), writer=writer))
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid, success=["Write a summary"])
    assert await sup.wait_idle(mid, timeout=30) == "completed"
    draft = tasks(ctx, mid)["draft"]
    subs = ctx.db.query("SELECT * FROM submissions WHERE task_id = ? ORDER BY round", (draft["id"],))
    assert [s["status"] for s in subs] == ["rejected", "accepted"]
    first_reviews = ctx.db.query("SELECT * FROM reviews WHERE submission_id = ?", (subs[0]["id"],))
    assert [r["verdict"] for r in first_reviews] == ["fail"]
    acc = ctx.db.one("SELECT * FROM acceptances WHERE task_id = ?", (draft["id"],))
    assert acc["submission_id"] == subs[1]["id"]


async def test_workers_cannot_accept(ctx, sup):
    ensure_fake_provider(ctx, script=make_script(plan_fn=one_task_plan()))
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid, success=["Write a summary"])
    await sup.wait_idle(mid, timeout=30)
    sub = ctx.db.one("SELECT id FROM submissions LIMIT 1")
    with pytest.raises(PermissionError):
        with ctx.db.tx() as cur:
            decide(cur, sub["id"], actor="agent:agt_x")


async def test_requirement_change_makes_inflight_and_accepted_work_stale(ctx, sup):
    inner = FakeProvider(make_script(plan_fn=one_task_plan(constraint_ids=["C1"])))
    ensure_fake_provider(ctx)
    fake_id = ctx.db.one("SELECT id FROM providers WHERE kind='fake'")["id"]
    ctx.registry.register_override(fake_id, SlowFirst(inner, "writer", 1.0))
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid, constraints=["Use only workspace sources"], success=["Write a summary"])
    # Run until the writer attempt is in flight, then revise the constraint it depends on.
    for _ in range(200):
        await sup.tick()
        await asyncio.sleep(0.02)
        d = tasks(ctx, mid).get("draft")
        if d and d["status"] == "running":
            break
    inflight = ctx.db.one("SELECT * FROM task_attempts WHERE task_id = ? AND status = 'running'", (d["id"],))
    with ctx.db.tx() as cur:
        res = revise_mission(cur, mid, objective=None, success_conditions=None,
                             constraints=["Use only workspace sources dated 2026"], reason="owner tightened sources")
    assert d["id"] in res["affected"]
    status = await sup.wait_idle(mid, timeout=30)
    if sup.running:
        await asyncio.gather(*sup.running.values(), return_exceptions=True)
    assert status == "completed"
    old = ctx.db.one("SELECT * FROM task_attempts WHERE id = ?", (inflight["id"],))
    assert old["status"] in ("abandoned", "fenced")
    assert not ctx.db.query("SELECT 1 FROM submissions WHERE attempt_id = ? AND status = 'accepted'", (inflight["id"],))
    cur_acc = ctx.db.query("SELECT * FROM acceptances WHERE task_id = ? AND is_current = 1", (d["id"],))
    assert len(cur_acc) == 1 and cur_acc[0]["mission_revision"] == 2

    # Accepted work under revision 2 becomes non-current under revision 3, and its old
    # submission can never be re-accepted.
    with ctx.db.tx() as cur:
        revise_mission(cur, mid, objective=None, success_conditions=None, constraints=["Any public source"],
                       reason="relaxed")
    acc_rows = ctx.db.query("SELECT * FROM acceptances WHERE task_id = ? ORDER BY created_at", (d["id"],))
    assert acc_rows[-1]["is_current"] == 0 and "revision 3" in acc_rows[-1]["noncurrent_reason"]
    with pytest.raises((FencedError, StaleSubmissionError)):
        with ctx.db.tx() as cur:
            decide(cur, acc_rows[-1]["submission_id"])
