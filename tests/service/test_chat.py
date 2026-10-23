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
