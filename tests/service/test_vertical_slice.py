"""Stage 1 vertical slice: planner -> researcher/writer -> reviewer -> integrate, offline fake."""
from helpers import events, new_mission, tasks
from nexus.bootstrap import create_demo_project, ensure_fake_provider


async def test_demo_research_mission_completes_with_verified_acceptance(ctx, sup):
    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid)
    status = await sup.wait_idle(mid, timeout=60)
    ts = tasks(ctx, mid)
    assert status == "completed", (status, {k: (v["status"], v["status_reason"]) for k, v in ts.items()})
    assert set(ts) >= {"plan", "integrate"} and len(ts) == 4
    # Every acceptance is backed by passing reviews and an immutable artifact hash.
    for acc in ctx.db.query("SELECT * FROM acceptances a JOIN tasks t ON t.id = a.task_id WHERE t.mission_id = ?", (mid,)):
        reviews = ctx.db.query("SELECT verdict FROM reviews WHERE submission_id = ?", (acc["submission_id"],))
        assert reviews and all(r["verdict"] == "pass" for r in reviews)
        assert ctx.artifacts.read(acc["artifact_id"])  # hash verified on read
    # The research task was rejected once (shared lineage) and repaired.
    research = next(v for k, v in ts.items() if v["kind"] == "research")
    subs = ctx.db.query("SELECT status, round FROM submissions WHERE task_id = ? ORDER BY round", (research["id"],))
    assert [s["status"] for s in subs] == ["rejected", "accepted"], subs
    fail = ctx.db.one("SELECT * FROM reviews r JOIN submissions s ON s.id = r.submission_id WHERE s.task_id = ? AND"
                      " r.verdict = 'fail'", (research["id"],))
    assert fail["check_kind"] == "cites_evidence" and "independent source lineage" in fail["findings_json"]
    assert events(ctx, mid, "prediction.surprise"), "first-round prediction mismatch should surface"
