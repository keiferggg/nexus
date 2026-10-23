"""Code tasks that depend on code tasks build on their accepted work, and the combined code is what gets tested.

Seen live: a self-improvement plan had "implement the improvement" and, separately, "run the full suite in the
sandbox". Every code task used to start from the mission's base commit, so the second task would have tested the
unchanged code and passed."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import new_mission, tasks
from nexus.appctx import build_context
from nexus.bootstrap import ensure_fake_provider, fixtures_dir
from nexus.domain.missions import create_project
from nexus.orchestration.supervisor import Supervisor
from nexus.persistence.db import dumps
from nexus.providers.http_adapters import OpenAIAdapter
from nexus.secrets import MemorySecretStore
from sim_providers import ProviderSim

SC = ["Add first.txt", "Add second.txt that reports whether first.txt is present"]


class RecordingSandbox:
    """Docker stand-in that records which files each tested worktree contains."""

    def __init__(self):
        self.runs = []

    async def available(self, refresh=False, image=None):
        return True, "fake docker"

    async def run(self, worktree, command, timeout_s=None, profile=None):
        wt = Path(worktree)
        self.runs.append({"files": sorted(p.name for p in wt.glob("*.txt")),
                          "second": (wt / "second.txt").read_text() if (wt / "second.txt").exists() else None})
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False, "passed": True, "sandbox": "fake"}


def tool_results(req):
    return [m.content for m in req.messages if m.role == "tool"]


def brain(req, packet):
    role, purpose = req.metadata.get("role"), req.metadata.get("purpose")
    title = ((packet or {}).get("task") or {}).get("title", "")
    if purpose == "plan":
        return {"rationale": "chain", "tasks": [
            {"key": "first", "title": "Add first.txt", "kind": "code", "description": "Create first.txt", "depends_on": [],
             "addresses": "SC1", "acceptance": [{"id": "A1", "text": "tests pass", "check": {"kind": "sandbox_command"}}]},
            {"key": "second", "title": "Add second.txt", "kind": "code", "description": "Create second.txt",
             "depends_on": ["first"], "addresses": "SC2",
             "acceptance": [{"id": "A1", "text": "tests pass", "check": {"kind": "sandbox_command"}}]}]}
    if role == "reviewer":
        return {"verdict": "pass", "summary": "ok", "findings": []}
    if role == "engineer":
        done = tool_results(req)
        if title == "Add first.txt":
            if not done:
                return {"__tool_calls__": [{"name": "repo_write", "arguments": {"path": "first.txt", "content": "one\n"}}]}
            return {"summary": "added first.txt", "files_changed": ["first.txt"], "test_command": None, "notes": ""}
        if len(done) == 0:
            return {"__tool_calls__": [{"name": "repo_read", "arguments": {"path": "first.txt"}}]}
        if len(done) == 1:
            seen = "present" if '"ok": true' in done[0] and "one" in done[0] else "missing"
            return {"__tool_calls__": [{"name": "repo_write", "arguments": {"path": "second.txt", "content": f"first.txt {seen}\n"}}]}
        return {"summary": "added second.txt", "files_changed": ["second.txt"], "test_command": None, "notes": ""}
    return {"summary": "n/a"}


@pytest.fixture
async def env(tmp_path):
    sb = RecordingSandbox()
    c = build_context(tmp_path / "d", secrets=MemorySecretStore(), sandbox=sb)
    c.heartbeat_s, c.lease_ttl_ms = 0.2, 5000
    ensure_fake_provider(c)
    sup = Supervisor(c)
    await sup.start()
    sup._loop_task.cancel()
    sim = ProviderSim("deepseek", brain=brain)
    pid = c.registry.add(kind="openai", label="sim", base_url="https://api.deepseek.com", api_key="sk-sim-123456")
    c.registry.register_override(pid, OpenAIAdapter("sk", base_url="https://api.deepseek.com", transport=sim))
    c.registry.set_price(pid, "deepseek-chat", 0.28, 0.42, source="test")
    with c.db.tx() as cur:
        cur.execute("UPDATE providers SET status = 'ok' WHERE id = ?", (pid,))
        cur.execute("INSERT OR REPLACE INTO settings VALUES ('default_model_policy', ?, '')",
                    (dumps({"provider_id": pid, "model": "deepseek-chat", "max_output_tokens": 1024}),))
    repo = tmp_path / "repo"
    shutil.copytree(fixtures_dir() / "demo-code", repo)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    with c.db.tx() as cur:
        proj = create_project(cur, name="repo", workspace_root=str(repo))
    sup.sandbox_ok_by_project[proj] = True
    sup._sandbox_checked_at = 1e18  # keep the fake's availability (no periodic re-check)
    yield c, sup, proj, sb, sim
    await sup.stop()
    c.close()


async def test_dependent_code_task_sees_upstream_work_and_combined_code_is_tested(env):
    c, sup, proj, sb, sim = env
    mid = new_mission(c, proj, objective="Chain two code changes", success=SC, constraints=[], mode="live", cap=1.0)
    status = await sup.wait_idle(mid, timeout=90)
    ts = tasks(c, mid)
    assert status == "completed", ({k: (v["status"], v["status_reason"]) for k, v in ts.items()}, sim.violations[:3])
    second_meta = json.loads(c.db.one("SELECT s.meta_json FROM submissions s WHERE s.task_id = ? ORDER BY s.created_at DESC",
                                      (ts["second"]["id"],))["meta_json"])
    assert second_meta["changed_files"] == ["second.txt"], "its own diff excludes the upstream work it built on"
    # the second task's sandbox run saw first.txt, and second.txt says it was present
    assert any(r["files"] == ["first.txt", "second.txt"] and r["second"] == "first.txt present\n" for r in sb.runs), sb.runs
    integ_plan = c.db.one("SELECT verification_plan_json FROM tasks WHERE id = ?", (ts["integrate"]["id"],))["verification_plan_json"]
    assert "sandbox_command" in integ_plan, "the combined code must be tested too"
    assert sb.runs[-1]["files"] == ["first.txt", "second.txt"], "last run is the integration worktree with both changes"
