"""A task whose job is a real external effect must actually perform it before it can be accepted.

Seen live (deepseek-v4-pro, unattended): the planner typed "Post the summary to httpbin" as an integrate task.
Integrate tasks have no tools, so the integrator wrote "prepared POST to httpbin.org", the reviewer accepted the
claim on the second round, and the mission reported completed with zero action requests."""
import json

import pytest

from helpers import new_mission, tasks
from nexus.appctx import build_context
from nexus.bootstrap import create_demo_project, ensure_fake_provider
from nexus.domain.verification import run_check
from nexus.orchestration.strategy import external_write_intent, materialize_plan
from nexus.persistence.db import loads
from nexus.secrets import MemorySecretStore
from nexus.tools.sandbox import UnavailableSandbox

SC = ("Summarize the workspace sources", "Post the summary to https://httpbin.org/post")


@pytest.fixture
def ctx(tmp_path):
    c = build_context(tmp_path / "d", secrets=MemorySecretStore(), sandbox=UnavailableSandbox("test"))
    ensure_fake_provider(c)
    yield c
    c.close()


def test_write_intent_detection():
    assert external_write_intent("Post the summary to https://httpbin.org/post")
    assert external_write_intent(None, "Email the brief to team@example.org")
    assert external_write_intent("Summarize the blog post at https://example.org/a") is None
    assert external_write_intent("Research using at least two independent web sources") is None
    assert external_write_intent("Write a one-page recommendation with citations") is None


def test_post_step_planned_as_integrate_becomes_an_act_task_that_needs_a_real_action(ctx):
    proj = create_demo_project(ctx, "research")
    mid = new_mission(ctx, proj, objective="Summarize the sources and post the summary as JSON to https://httpbin.org/post",
                      success=SC, constraints=(), start=False)
    plan = {"rationale": "as seen live", "tasks": [
        {"key": "summarize-workspace-sources", "title": "Summarize workspace sources", "kind": "write",
         "description": "Summarize both files.", "depends_on": [], "addresses": "SC1",
         "acceptance": [{"id": "A1", "text": "has words", "check": {"kind": "min_words", "args": {"min": 20}}}]},
        {"key": "post-summary-to-httpbin", "title": "Post the summary JSON to httpbin", "kind": "integrate",
         "description": "Integrate the accepted summary and POST it.", "depends_on": ["summarize-workspace-sources"],
         "addresses": "SC2",
         "acceptance": [{"id": "A1", "text": "POST confirmed", "check": {"kind": "reviewer"}}]}]}
    with ctx.db.tx() as cur:
        materialize_plan(cur, plan_task_id=None, plan=plan, sandbox_ok=False, base_commit=None, mission_id=mid,
                         add_integrate=False)
    t = tasks(ctx, mid)["post-summary-to-httpbin"]
    assert t["kind"] == "act" and t["role"] == "operator"
    plan_checks = [p["check"]["kind"] for p in loads(t["verification_plan_json"], [])]
    assert "actions_settled" in plan_checks
    # With no action on record, the effect is not proven: the check fails, whatever the agent claims.
    with ctx.db.tx() as cur:
        r = run_check(cur, {"kind": "actions_settled", "args": {"min_actions": 1}},
                      content="I POSTed the summary to https://httpbin.org/post (status 200).", mission_id=mid,
                      submission_meta={"_task_id": t["id"], "_epoch": t["epoch"]}, worktree=None)
    assert r["verdict"] == "fail", r
    assert tasks(ctx, mid)["summarize-workspace-sources"]["kind"] == "write"  # ordinary work is left alone


def test_file_checks_on_non_code_tasks_are_dropped(ctx):
    """Seen live: the planner gave a write task 'summary.json exists' (file_exists). Only code tasks have a
    worktree, so it was always inconclusive and blocked a summary that passed every content check."""
    proj = create_demo_project(ctx, "research")
    mid = new_mission(ctx, proj, success=SC, constraints=(), start=False)
    plan = {"rationale": "as seen live", "tasks": [
        {"key": "summarize-workspace-sources", "title": "Summarize the workspace sources as JSON", "kind": "write",
         "description": "Write summary.json.", "depends_on": [], "addresses": "SC1",
         "acceptance": [{"id": "A1", "text": "summary.json exists", "check": {"kind": "file_exists", "args": {"path": "summary.json"}}},
                        {"id": "A2", "text": "valid JSON", "check": {"kind": "json_valid", "args": {"path": "summary.json"}}}]},
        {"key": "only-files", "title": "Write notes", "kind": "write", "description": "x", "depends_on": [],
         "addresses": "SC1", "acceptance": [{"id": "A1", "text": "notes.md exists", "check": {"kind": "file_exists"}}]}]}
    with ctx.db.tx() as cur:
        materialize_plan(cur, plan_task_id=None, plan=plan, sandbox_ok=False, base_commit=None, mission_id=mid,
                         add_integrate=False)
    ts = tasks(ctx, mid)
    kinds = [p["check"]["kind"] for p in loads(ts["summarize-workspace-sources"]["verification_plan_json"], [])]
    assert "file_exists" not in kinds and "json_valid" in kinds
    assert [p["check"]["kind"] for p in loads(ts["only-files"]["verification_plan_json"], [])] == ["reviewer"]
