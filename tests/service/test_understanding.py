"""Acceptance scenarios 7, 8, 12: evidence withdrawal, false independence, workspace conflict."""
import subprocess
from pathlib import Path

from helpers import events, new_mission, tasks
from nexus.bootstrap import create_demo_project, ensure_fake_provider
from nexus.domain.understanding import change_evidence, lineage_summary


async def _completed_research(ctx, sup):
    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid)
    assert await sup.wait_idle(mid, timeout=60) == "completed"
    return mid


async def test_false_independence_is_not_counted_as_corroboration(ctx, sup):
    mid = await _completed_research(ctx, sup)
    ev = {e["locator"]: e for e in ctx.db.query("SELECT * FROM evidence WHERE mission_id = ?", (mid,))}
    survey, news = ev["sources/resident-survey.md"], ev["sources/regional-news.md"]
    assert survey["lineage_root"] == news["lineage_root"]
    with ctx.db.tx() as cur:
        lin = lineage_summary(cur, [survey["id"], news["id"]])
    assert lin["active"] == 2 and lin["independent_lineages"] == 1
    research = next(t for t in tasks(ctx, mid).values() if t["kind"] == "research")
    rejected = ctx.db.one("SELECT r.findings_json FROM reviews r JOIN submissions s ON s.id = r.submission_id"
                          " WHERE s.task_id = ? AND r.verdict = 'fail'", (research["id"],))
    assert "1 independent source lineage(s) cited; 2 required" in rejected["findings_json"]


async def test_withdrawing_evidence_invalidates_only_dependents(ctx, sup):
    mid = await _completed_research(ctx, sup)
    before = tasks(ctx, mid)
    news = ctx.db.one("SELECT * FROM evidence WHERE mission_id = ? AND locator = 'sources/regional-news.md'", (mid,))
    council = ctx.db.one("SELECT * FROM evidence WHERE mission_id = ? AND locator = 'sources/council-minutes.md'", (mid,))
    claims_council_only = [c["claim_id"] for c in ctx.db.query(
        "SELECT claim_id FROM claim_evidence GROUP BY claim_id HAVING SUM(evidence_id = ?) = COUNT(*)", (council["id"],))]
    assert claims_council_only
    with ctx.db.tx() as cur:
        result = change_evidence(cur, news["id"], new_status="withdrawn", reason="syndicated copy retracted",
                                 actor="owner")
    after = tasks(ctx, mid)
    assert after["plan"]["status"] == "accepted" and not after["plan"]["is_stale"], "plan does not depend on it"
    reopened = {k for k, v in after.items() if v["epoch"] > before[k]["epoch"]}
    research_key = next(k for k, v in after.items() if v["kind"] == "research")
    assert research_key in reopened and "plan" not in reopened
    for cid in claims_council_only:
        c = ctx.db.one("SELECT status, is_stale FROM claims WHERE id = ?", (cid,))
        assert c["status"] == "supported" and not c["is_stale"], "unrelated claims stay current"
    news_claims = ctx.db.query("SELECT c.* FROM claims c JOIN claim_evidence ce ON ce.claim_id = c.id WHERE"
                               " ce.evidence_id = ?", (news["id"],))
    assert all(c["is_stale"] for c in news_claims)
    old_acc = ctx.db.query("SELECT * FROM acceptances WHERE task_id = ?", (after[research_key]["id"],))
    assert old_acc and all(a["is_current"] == 0 for a in old_acc), "historical acceptance kept, no longer current"
    assert events(ctx, mid, "plan.changed")
    # Repair: the mission resumes; reopened work is re-verified against the remaining evidence.
    from nexus.domain.missions import set_mission_status

    with ctx.db.tx() as cur:
        set_mission_status(cur, mid, "running", actor="owner", reason="re-run after evidence change",
                           allowed_from={"completed"})
    assert await sup.wait_idle(mid, timeout=60) == "completed"
    final = tasks(ctx, mid)
    new_acc = ctx.db.one("SELECT * FROM acceptances WHERE task_id = ? AND is_current = 1", (final[research_key]["id"],))
    assert new_acc is not None
    content = ctx.artifacts.read_text(new_acc["artifact_id"])
    assert news["id"] not in content, "withdrawn evidence must not be cited again"
    reviews = ctx.db.query("SELECT verdict FROM reviews WHERE submission_id = ?", (new_acc["submission_id"],))
    assert reviews and all(r["verdict"] == "pass" for r in reviews)
    assert result["rechecks"]


async def test_workspace_conflict_detected_and_integrated(ctx, sup):
    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "code")
    mid = new_mission(ctx, pid, objective="Extend textkit",
                      success=["Implement slugify(text) in textkit/textutils.py with a test",
                               "Implement truncate(text, n) in textkit/textutils.py with a test"],
                      constraints=["Keep the public API backwards compatible"])
    await sup.wait_idle(mid, timeout=60)
    ts = tasks(ctx, mid)
    # No sandbox here: behavior tests became explicit owner-approval criteria, so the coding tasks wait.
    for key in ("slugify", "truncate"):
        assert ts[key]["status"] == "blocked" and "inconclusive" in ts[key]["status_reason"], ts[key]
        plan = ctx.db.one("SELECT verification_plan_json FROM tasks WHERE id = ?", (ts[key]["id"],))["verification_plan_json"]
        assert "owner_approval" in plan
    # Each worker used its own worktree/branch.
    branches = {ctx.db.one("SELECT meta_json FROM submissions WHERE task_id = ?", (ts[k]["id"],))["meta_json"] for k in
                ("slugify", "truncate")}
    assert len(branches) == 2
    for key in ("slugify", "truncate"):
        sub = ctx.db.one("SELECT id FROM submissions WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (ts[key]["id"],))
        crit = ctx.db.one("SELECT criterion FROM reviews WHERE submission_id = ? AND verdict = 'inconclusive'",
                          (sub["id"],))["criterion"]
        r = sup.owner_review(sub["id"], criterion=crit, verdict="pass", note="owner ran the tests locally")
        assert r["verdict"] == "accepted"
    st = await sup.wait_idle(mid, timeout=60)
    integ = tasks(ctx, mid)["integrate"]
    if st != "completed":
        for r in ctx.db.query("SELECT s.round, s.status, r.check_kind, r.verdict, r.findings_json FROM submissions s LEFT JOIN"
                              " reviews r ON r.submission_id = s.id WHERE s.task_id = ?", (integ["id"],)):
            print(dict(r))
        print({k: (v["status"], v["status_reason"]) for k, v in tasks(ctx, mid).items()})
        print([dict(a) for a in ctx.db.query("SELECT attempt_no, status, error FROM task_attempts WHERE task_id = ?", (integ["id"],))])
    assert st == "completed"
    subs = ctx.db.query("SELECT * FROM submissions WHERE task_id = ? ORDER BY round", (integ["id"],))
    assert [s["status"] for s in subs] == ["rejected", "accepted"]
    first = ctx.db.query("SELECT check_kind, verdict FROM reviews WHERE submission_id = ?", (subs[0]["id"],))
    assert ("no_conflicts", "fail") in {(r["check_kind"], r["verdict"]) for r in first}
    blocker = ctx.db.one("SELECT body FROM messages WHERE task_id = ? AND type = 'blocker'", (integ["id"],))
    assert "textkit/textutils.py" in blocker["body"]
    import json
    meta = json.loads(subs[1]["meta_json"])
    combined = (Path(meta["worktree"]) / "textkit" / "textutils.py").read_text()
    assert "def slugify" in combined and "def truncate" in combined and "<<<<<<<" not in combined
    log = subprocess.run(["git", "log", "--oneline", meta["head"]], cwd=meta["worktree"], capture_output=True,
                         text=True).stdout
    assert "resolve conflicts" in log
