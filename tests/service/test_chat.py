"""Chat front door over the real provider protocol (DeepSeek simulator): answer, mission (auto-started and proposed),
narration back into the thread, setup gating, folder safety and the daily chat budget."""
import json
import re
import shutil

import pytest

from nexus.appctx import build_context
from nexus.bootstrap import ensure_fake_provider, fixtures_dir
from nexus.chat.narrator import Narrator
from nexus.chat.service import ChatService
from nexus.demo.script import demo_script
from nexus.domain.missions import create_project
from nexus.orchestration.supervisor import Supervisor
from nexus.persistence.db import dumps, now_iso
from nexus.providers.http_adapters import OpenAIAdapter
from nexus.secrets import MemorySecretStore
from nexus.tools.sandbox import UnavailableSandbox
from sim_providers import ProviderSim

OBJ = "Brief the Larkspur council on a bike-share pilot"
SC = ["Research resident demand using workspace sources", "Write a one-page recommendation for the council"]


def owner_message(req) -> str:
    last = next(m.content for m in reversed(req.messages) if m.role == "user")
    return last.split("OWNER MESSAGE:\n", 1)[-1].strip()


def context(req) -> dict:
    last = next(m.content for m in reversed(req.messages) if m.role == "user")
    m = re.search(r"CONTEXT \(data\):\n(.*?)\n\nOWNER MESSAGE:", last, re.S)
    return json.loads(m.group(1)) if m else {}


class Concierge:
    """Deterministic stand-in for the concierge model; everything else uses the demo script."""

    def __init__(self):
        self.plan: dict | None = None

    def __call__(self, req, packet):
        if req.metadata.get("role") != "NEXUS":
            return demo_script(req, packet)
        text = owner_message(req)
        if text.startswith("mission"):
            proj = next(p["id"] for p in context(req)["PROJECTS"] if p["name"] == "Larkspur")
            cap = 5.0 if "expensive" in text else 1.0
            return {"reply": "On it.", "action": "mission", "conversation_title": "Bike-share brief",
                    "mission": {"title": "Larkspur bike-share brief", "objective": OBJ, "success_conditions": SC,
                                "constraints": ["Use only workspace sources"], "project": proj,
                                "estimate_usd": cap / 2, "cap_usd": cap}}
        if text.startswith("autopilot"):
            proj = next(p["id"] for p in context(req)["PROJECTS"] if p["name"] == "Larkspur")
            big = "big" in text
            return {"reply": "I'll do that every morning.", "action": "autopilot", "conversation_title": "Morning brief",
                    "mission": {"title": "Morning brief", "objective": OBJ, "success_conditions": SC, "project": proj,
                                "trigger": {"type": "daily", "at": "07:30"}, "run_cap_usd": 4.0 if big else 0.5,
                                "daily_cap_usd": 8.0 if big else 1.0}}
        if text.startswith("pause"):
            a = context(req)["AUTOPILOTS"][0]
            return {"reply": "Paused.", "action": "autopilot_control", "autopilot": {"id": a["id"], "op": "pause"}}
        if text.startswith("folder"):
            return {"reply": "Using that folder.", "action": "mission",
                    "mission": {"title": "x", "objective": "y", "success_conditions": ["z"],
                                "project": {"path": "C:\\Windows"}, "estimate_usd": 0.1, "cap_usd": 0.2}}
        return {"reply": "4", "action": "answer", "conversation_title": "Arithmetic"}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_PERSONAL_DIR", str(tmp_path / "personal"))
    c = build_context(tmp_path / "d", secrets=MemorySecretStore(), sandbox=UnavailableSandbox("test"))
    c.heartbeat_s, c.lease_ttl_ms = 0.2, 5000
    ensure_fake_provider(c)
    sup = Supervisor(c)
    await sup.start()
    sup._loop_task.cancel()
    brain = Concierge()
    sim = ProviderSim("deepseek", brain=brain)
    pid = c.registry.add(kind="openai", label="DeepSeek (sim)", base_url="https://api.deepseek.com", api_key="sk-sim-123456")
    c.registry.register_override(pid, OpenAIAdapter("sk-ds", base_url="https://api.deepseek.com", transport=sim))
    c.registry.set_price(pid, "deepseek-chat", 0.28, 0.42, source="test price")
    chat = ChatService(c, sup)
    yield c, sup, chat, pid, sim
    await sup.stop()
    c.close()


def configure(c, pid, ack=True):
    with c.db.tx() as cur:
        cur.execute("UPDATE providers SET status = 'ok' WHERE id = ?", (pid,))  # as after Test connection
        cur.execute("INSERT OR REPLACE INTO settings VALUES ('default_model_policy', ?, '')",
                    (dumps({"provider_id": pid, "model": "deepseek-chat", "max_output_tokens": 1024}),))
        if ack:
            cur.execute("INSERT OR REPLACE INTO settings VALUES ('chat.data_ack', 'true', '')")


def larkspur(c, tmp_path):
    root = tmp_path / "larkspur"
    shutil.copytree(fixtures_dir() / "demo-research", root)
    with c.db.tx() as cur:
        return create_project(cur, name="Larkspur", workspace_root=str(root))


def kinds(chat, cid):
    return [m["kind"] for m in chat.messages(cid) if m["role"] == "nexus"]


async def test_setup_is_requested_before_anything_is_sent(env):
    c, sup, chat, pid, sim = env
    cid = chat.create_conversation()["id"]
    r = await chat.send(cid, "hello")
    assert r["setup"]["need"] == "model" and sim.requests == 0
    configure(c, pid, ack=False)
    r = await chat.send(cid, "hello again")
    assert r["setup"]["need"] == "data_ack" and sim.requests == 0, "nothing leaves the machine before the owner agrees"


async def test_question_is_answered_directly(env):
    c, sup, chat, pid, sim = env
    configure(c, pid)
    cid = chat.create_conversation()["id"]
    r = await chat.send(cid, "what is 2+2?")
    assert r["action"] == "answer"
    msgs = chat.messages(cid)
    assert [m["content"] for m in msgs] == ["what is 2+2?", "4"]
    assert c.db.one("SELECT title FROM conversations WHERE id = ?", (cid,))["title"] == "Arithmetic"
    assert c.db.one("SELECT purpose FROM model_calls ORDER BY rowid DESC LIMIT 1")["purpose"] == "chat"
    assert sim.violations == []


async def test_request_becomes_an_auto_started_mission_and_the_result_comes_back_to_the_thread(env, tmp_path):
    c, sup, chat, pid, sim = env
    configure(c, pid)
    larkspur(c, tmp_path)
    narrator = Narrator(c, chat)
    cid = chat.create_conversation()["id"]
    r = await chat.send(cid, "mission: write the council brief")
    assert r.get("action") == "mission" and r["started"], (r, [m["content"][:200] for m in chat.messages(cid)])
    mid = r["mission_id"]
    assert await sup.wait_idle(mid, timeout=90) == "completed", sim.violations[:3]
    while narrator.process():
        pass
    msgs = [m for m in chat.messages(cid) if m["role"] == "nexus"]
    ks = [m["kind"] for m in msgs]
    assert ks[:2] == ["text", "mission"] and "progress" in ks and ks[-1] == "result"
    card = next(m for m in msgs if m["kind"] == "mission")
    assert card["data"]["auto_started"] and card["data"]["cap_usd"] == 1.0
    plan = next(m for m in msgs if m["kind"] == "progress" and m["content"].startswith("Plan:"))
    assert "Research resident demand" in plan["content"]
    assert msgs[-1]["data"]["artifact_id"] and len(msgs[-1]["content"]) > 100
    n = len(chat.messages(cid))
    narrator2 = Narrator(c, chat)  # restart: no duplicates
    narrator2.cursor = 0
    while narrator2.process():
        pass
    assert len(chat.messages(cid)) == n


async def test_expensive_request_waits_for_a_start_tap(env, tmp_path):
    c, sup, chat, pid, sim = env
    configure(c, pid)
    larkspur(c, tmp_path)
    cid = chat.create_conversation()["id"]
    r = await chat.send(cid, "mission expensive: write the council brief")
    assert r.get("action") == "mission" and not r["started"], (r, [m["content"][:200] for m in chat.messages(cid)])
    mid = r["mission_id"]
    assert c.db.one("SELECT status FROM missions WHERE id = ?", (mid,))["status"] == "draft"
    card = next(m for m in chat.messages(cid) if m["kind"] == "mission")
    assert "auto-start limit" in card["data"]["why_not_started"]
    await chat.start_proposed(mid)
    assert c.db.one("SELECT status FROM missions WHERE id = ?", (mid,))["status"] == "running"


async def test_model_cannot_pick_a_folder_the_owner_did_not_name(env):
    c, sup, chat, pid, sim = env
    configure(c, pid)
    cid = chat.create_conversation()["id"]
    r = await chat.send(cid, "folder please, do some work")
    assert "error" in r and "only use a folder you name" in r["error"]
    assert c.db.one("SELECT 1 FROM projects WHERE lower(workspace_root) LIKE '%windows%'") is None
    assert c.db.one("SELECT COUNT(*) AS n FROM missions")["n"] == 0


async def test_daily_chat_budget_stops_chat_calls(env):
    c, sup, chat, pid, sim = env
    configure(c, pid)
    with c.db.tx() as cur:
        cur.execute("INSERT OR REPLACE INTO settings VALUES ('chat.limits', ?, '')", (dumps({"daily_chat_usd": 0.0}),))
    cid = chat.create_conversation()["id"]
    r = await chat.send(cid, "what is 2+2?")
    assert r == {"error": "chat budget"} and sim.requests == 0, (r, sim.requests, chat._chat_spend_today())


async def test_recurring_request_becomes_an_autopilot_within_limits(env, tmp_path):
    c, sup, chat, pid, sim = env
    configure(c, pid)
    larkspur(c, tmp_path)
    cid = chat.create_conversation()["id"]
    r = await chat.send(cid, "autopilot: brief me every morning")
    assert r == {"action": "autopilot", "standing_id": r["standing_id"], "enabled": True}
    card = next(m for m in chat.messages(cid) if m["kind"] == "autopilot")
    assert card["data"]["schedule"] == "every day at 07:30" and card["data"]["enabled"]
    r2 = await chat.send(cid, "autopilot big: brief me every morning, thoroughly")
    assert r2["enabled"] is False, "above the owner's limits: created paused, waits for Enable"
    r3 = await chat.send(cid, "pause the morning brief")
    assert r3["op"] == "pause"
    assert not c.db.one("SELECT enabled FROM standing_missions WHERE id = ?", (r3["standing_id"],))["enabled"]


class BuildableSandbox:
    """Docker stand-in: the image is missing until build() is called."""

    def __init__(self):
        self.images, self.builds = set(), []

    async def available(self, refresh=False, image=None):
        return (image in self.images, "ok" if image in self.images else "missing")

    async def build(self, dockerfile, context, tag, timeout_s=1800):
        self.builds.append((str(dockerfile), str(context), tag))
        self.images.add(tag)
        return True, "built"

    async def run(self, worktree, command, timeout_s=None, profile=None):
        raise RuntimeError("not used")


async def test_repo_sandbox_is_set_up_automatically_from_nexus_sandbox_json(env, tmp_path):
    c, sup, chat, pid, sim = env
    configure(c, pid)
    repo = tmp_path / "myrepo"
    shutil.copytree(fixtures_dir() / "demo-research", repo)
    (repo / ".nexus").mkdir()
    (repo / "docker").mkdir()
    (repo / "docker" / "test.Dockerfile").write_text("FROM python:3.12-slim\n")
    (repo / ".nexus" / "sandbox.json").write_text(json.dumps({"image": "myrepo-tests:1", "dockerfile": "docker/test.Dockerfile",
                                                              "memory_mb": 1024, "timeout_s": 300}))
    sb = BuildableSandbox()
    c.sandbox = sb
    with c.db.tx() as cur:
        rp = create_project(cur, name="myrepo", workspace_root=str(repo.resolve()))
    cid = chat.create_conversation()["id"]
    await chat.ensure_repo_sandbox(rp, cid)
    assert sb.builds and sb.builds[0][2] == "myrepo-tests:1" and sb.builds[0][0].endswith("test.Dockerfile")
    from nexus.tools.sandbox import project_profile

    prof = project_profile(c.db, rp)
    assert prof["image"] == "myrepo-tests:1" and prof["memory_mb"] == 1024 and prof["timeout_s"] == 300
    assert sup.sandbox_ok_by_project[rp] is True
    assert any("Sandbox ready" in m["content"] for m in chat.messages(cid))
    # a spec whose Dockerfile points outside the repository is refused
    (repo / ".nexus" / "sandbox.json").write_text(json.dumps({"image": "x:1", "dockerfile": "../../evil.Dockerfile"}))
    with c.db.tx() as cur:
        rp2 = create_project(cur, name="other", workspace_root=str(repo.resolve()) + "")
        cur.execute("UPDATE projects SET settings_json = '{}' WHERE id = ?", (rp2,))
    await chat.ensure_repo_sandbox(rp2, cid)
    assert len(sb.builds) == 1 and any("escapes the repository" in m["content"] for m in chat.messages(cid))
