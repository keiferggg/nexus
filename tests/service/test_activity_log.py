"""Live activity log: readable + JSONL files written as a mission runs; no duplicates after restart."""
import json

from helpers import new_mission
from nexus.bootstrap import create_demo_project, ensure_fake_provider


async def test_activity_log_follows_a_mission(ctx, sup):
    from nexus.activity_log import ActivityLog

    ensure_fake_provider(ctx)
    pid = create_demo_project(ctx, "research")
    mid = new_mission(ctx, pid)
    assert await sup.wait_idle(mid, timeout=60) == "completed"
    act = ActivityLog(ctx)
    act.process()
    while act.process():
        pass
    logs = list(act.folder.glob("activity-*.log"))
    text = logs[0].read_text(encoding="utf-8")
    for needle in ("PLAN created", "tool read_source succeeded", "review A1 cites_evidence: FAIL", "ACCEPTED",
                   "MISSION running -> completed", "calls tools: read_source"):
        assert needle in text, needle
    rows = [json.loads(l) for l in next(act.folder.glob("activity-*.jsonl")).read_text(encoding="utf-8").splitlines()]
    finished = [r for r in rows if r["type"] == "model_call.finished"]
    assert finished and all("output_tokens" in r["detail"] for r in finished)
    assert any(r["detail"].get("text") for r in finished), "model output excerpts are included"
    n = len(rows)
    act2 = ActivityLog(ctx)  # restart: resumes from the cursor, no duplicates
    act2.process()
    assert len(next(act.folder.glob("activity-*.jsonl")).read_text(encoding="utf-8").splitlines()) == n
    assert "sk-" not in text
