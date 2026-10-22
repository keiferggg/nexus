"""LIVE internet smoke test (read-only GETs to public services). Opt-in: set NEXUS_LIVE_WEB=1.
Results depend on third-party services; failures here are reported, not hidden."""
import os

import pytest

from nexus.appctx import build_context
from nexus.domain.missions import create_project
from nexus.secrets import MemorySecretStore
from nexus.tools.broker import ToolContext
from nexus.tools.builtin import expand_capabilities
from nexus.tools.sandbox import UnavailableSandbox

pytestmark = pytest.mark.skipif(os.environ.get("NEXUS_LIVE_WEB") != "1", reason="live internet test (set NEXUS_LIVE_WEB=1)")

CASES = [
    ("fetch_url", {"url": "https://example.com/"}, lambda r: "Example Domain" in r["title"]),
    ("web_search", {"query": "python httpx library"}, lambda r: len(r["results"]) > 0),
    pytest.param("wikipedia", {"query": "Bicycle-sharing system"}, lambda r: "bicycle" in (r["summary"] or "").lower(),
                 marks=pytest.mark.skipif(not os.environ.get("NEXUS_CONTACT"),
                                          reason="Wikimedia requires a real contact in the User-Agent: set NEXUS_CONTACT")),
    ("arxiv_search", {"query": "bike sharing demand"}, lambda r: len(r["results"]) > 0),
    ("package_info", {"ecosystem": "pypi", "name": "httpx"}, lambda r: r["name"].lower() == "httpx"),
    ("package_info", {"ecosystem": "npm", "name": "react"}, lambda r: r["name"] == "react"),
    ("github_search", {"query": "agent framework language:python"}, lambda r: len(r["results"]) > 0),
    ("stackexchange_search", {"query": "python asyncio timeout"}, lambda r: len(r["results"]) > 0),
    ("weather", {"place": "Berlin"}, lambda r: r["current"] is not None),
    ("exchange_rates", {"base": "USD", "symbols": ["EUR"]}, lambda r: "EUR" in r["rates"]),
    ("wayback_lookup", {"url": "example.com"}, lambda r: "snapshot" in r),
    ("read_feed", {"url": "https://hnrss.org/frontpage"}, lambda r: len(r["items"]) > 0),
    ("extract_links", {"url": "https://www.python.org/"}, lambda r: r["total"] > 10),
]


@pytest.fixture
def live(tmp_path):
    c = build_context(tmp_path / "d", secrets=MemorySecretStore(), sandbox=UnavailableSandbox("test"))
    with c.db.tx() as cur:
        pid = create_project(cur, name="live", workspace_root=str(tmp_path))
    agent = c.db.one("SELECT id FROM agent_identities WHERE project_id = ? LIMIT 1", (pid,))["id"]
    t = ToolContext(project_id=pid, workspace_root=tmp_path, agent_id=agent,
                    allowed_tools=expand_capabilities(c.broker, ["internet", "utilities"]),
                    extra={"internet": {"contact": os.environ.get("NEXUS_CONTACT", "")}})
    yield c, t
    c.close()


def _id(c):
    v = c.values if hasattr(c, "values") else c
    return f"{v[0]}:{list(v[1].values())[0]}"


@pytest.mark.parametrize("tool,args,check", CASES, ids=[_id(c) for c in CASES])
async def test_live_tool(live, tool, args, check):
    c, t = live
    r = await c.broker.invoke(t, tool, args, operation_id=f"live-{tool}-{list(args.values())[0]}")
    assert r["ok"], r.get("error")
    assert check(r["result"]), str(r["result"])[:500]


async def test_live_ssrf_guards(live):
    c, t = live
    for url in ["http://169.254.169.254/latest/meta-data/", "http://localhost:80/", "http://127.0.0.1:22/"]:
        r = await c.broker.invoke(t, "fetch_url", {"url": url}, operation_id=f"live-ssrf-{url}")
        assert not r["ok"] and "non-public" in r["error"]


async def test_live_web_research_mission(tmp_path):
    """Full pipeline on the real internet: a (scripted) researcher searches and fetches two
    independent public pages; NEXUS verifies citations, lineage independence and quotations
    against what was actually fetched."""
    import json as _json

    from helpers import new_mission, tasks
    from nexus.bootstrap import ensure_fake_provider
    from nexus.demo import script as demo
    from nexus.orchestration.supervisor import Supervisor

    urls = ["https://example.com/", "https://www.iana.org/help/example-domains"]

    def script(req, packet):
        role = req.metadata.get("role")
        if req.metadata.get("purpose") == "review" or role == "reviewer":
            return demo.review(req, packet)
        if role == "planner":
            return {"rationale": "live", "tasks": [{"key": "research", "title": "What are example domains for?",
                    "kind": "research", "description": "Use the web", "addresses": "SC1", "depends_on": [],
                    "acceptance": [{"id": "A1", "text": "two independent sources",
                                    "check": {"kind": "cites_evidence", "args": {"min_independent": 2}}},
                                   {"id": "A2", "text": "quotes verified",
                                    "check": {"kind": "quotes_verified", "args": {"require_quotes": True}}}]}]}
        if role == "integrator":
            return demo.integrate_doc(req, packet)
        done = [_json.loads(m.content) for m in req.messages if m.role == "tool"]
        if not done:
            return {"__tool_calls__": [{"id": "s", "name": "web_search", "arguments": {"query": "IANA example domains"}}]
                    + [{"id": f"f{i}", "name": "fetch_url", "arguments": {"url": u}} for i, u in enumerate(urls)]}
        lines = []
        for d in done:
            r = d.get("result") or {}
            if "evidence_id" in r:
                sentence = next((s.strip() + "." for s in r["text"].replace("\n", " ").split(".") if len(s.strip()) > 30), "")
                lines.append(f'- {r["title"]}: "{sentence}" [E:{r["evidence_id"]}]')
        return {"content_markdown": "\n".join(lines), "summary": f"{len(lines)} live sources"}

    from nexus.appctx import build_context

    c = build_context(tmp_path / "d", secrets=MemorySecretStore(), sandbox=UnavailableSandbox("test"))
    ensure_fake_provider(c, script=script)
    with c.db.tx() as cur:
        pid = create_project(cur, name="live", workspace_root=str(tmp_path))
    sup = Supervisor(c)
    await sup.start()
    sup._loop_task.cancel()
    try:
        mid = new_mission(c, pid, objective="Explain example domains", success=["Research what example domains are for"])
        status = await sup.wait_idle(mid, timeout=120)
        t = tasks(c, mid)["research"]
        reviews = c.db.query("SELECT check_kind, verdict, json_extract(findings_json,'$.summary') s FROM reviews WHERE task_id = ?",
                             (t["id"],))
        assert status == "completed", (t["status_reason"], [dict(r) for r in reviews])
        ev = c.db.query("SELECT lineage_root FROM evidence WHERE mission_id = ?", (mid,))
        assert {"https://example.com/", "https://www.iana.org/help/example-domains"} <= {e["lineage_root"] for e in ev}
    finally:
        await sup.stop()
        c.close()
