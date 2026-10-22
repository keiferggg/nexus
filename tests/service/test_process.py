"""Real service processes: auth/origin guard, SSE resume, hard kill mid-mission (scenario 1),
backup/restore without credentials (scenario 17)."""
import json
import os
import secrets
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

PY = sys.executable
FAKE_KEY = "sk-test-NEVER-EXPORT-" + "9" * 24


class Service:
    def __init__(self, data_dir: Path, *, fault: str | None = None):
        self.token = secrets.token_urlsafe(32)
        env = {**os.environ, "NEXUS_AUTH_TOKEN": self.token, "NEXUS_SECRET_BACKEND": "memory",
               "NEXUS_ALLOWED_ORIGINS": "app://nexus"}
        env.pop("NEXUS_FAULT_EXIT", None)
        if fault:
            env["NEXUS_FAULT_EXIT"] = fault
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = data_dir.parent / f"{data_dir.name}-{secrets.token_hex(3)}.log"
        self.log = open(self.log_path, "w")  # stderr must be drained or the service blocks on a full pipe
        self.proc = subprocess.Popen([PY, "-m", "nexus", "serve", "--data-dir", str(data_dir)], env=env,
                                     stdout=subprocess.PIPE, stderr=self.log, text=True)
        line = self.proc.stdout.readline()
        assert line.startswith("NEXUS_READY"), f"no ready line: {line!r} {self.log_path.read_text()[-2000:]}"
        self.port = json.loads(line.split(" ", 1)[1])["port"]
        self.base = f"http://127.0.0.1:{self.port}"
        self.h = {"authorization": f"Bearer {self.token}"}

    def get(self, path, **kw):
        return httpx.get(self.base + path, headers=self.h, timeout=30, **kw)

    def post(self, path, body=None):
        return httpx.post(self.base + path, headers=self.h, json=body or {}, timeout=30)

    def wait_mission(self, mid, statuses=("completed", "blocked", "failed", "canceled"), timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                return None
            try:
                m = self.get(f"/api/missions/{mid}").json()
            except httpx.HTTPError:
                return None
            if m["mission"]["status"] in statuses:
                return m
            time.sleep(0.2)
        raise TimeoutError(mid)

    def stop(self):
        self.log.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def start_demo_mission(svc):
    p = svc.post("/api/projects/demo/research").json()
    m = svc.post(f"/api/projects/{p['id']}/missions", {
        "title": "Larkspur brief", "objective": "Brief the Larkspur council on a bike-share pilot",
        "success_conditions": ["Research resident demand using workspace sources",
                               "Write a one-page recommendation for the council"],
        "constraints": ["Use only workspace sources"]}).json()
    return p, m["mission"]["id"]


def test_auth_origin_and_sse_resume(tmp_path):
    svc = Service(tmp_path / "d")
    try:
        assert httpx.get(svc.base + "/api/health").status_code == 401
        assert httpx.get(svc.base + "/api/health", headers={"authorization": "Bearer wrong"}).status_code == 401
        bad = httpx.get(svc.base + "/api/health", headers={**svc.h, "origin": "https://evil.example"})
        assert bad.status_code == 403
        assert httpx.get(svc.base + "/api/health", headers={**svc.h, "origin": "app://nexus"}).status_code == 200
        h = svc.get("/api/health").json()
        assert h["status"] == "ready" and h["framework"]["agent-framework-core"] == "1.22.0"
        assert h["secrets"]["backend"].startswith("memory")
        _, mid = start_demo_mission(svc)
        assert svc.wait_mission(mid)["mission"]["status"] == "completed"
        # SSE: read part of the stream, then resume from the last id; no gaps, no duplicates.
        seen = []
        with httpx.stream("GET", svc.base + "/api/events/stream", headers=svc.h, timeout=10) as r:
            for line in r.iter_lines():
                if line.startswith("id: "):
                    seen.append(int(line[4:]))
                if len(seen) >= 20:
                    break
        resumed = []
        with httpx.stream("GET", svc.base + "/api/events/stream", headers={**svc.h, "last-event-id": str(seen[-1])},
                          timeout=10) as r:
            for line in r.iter_lines():
                if line.startswith("id: "):
                    resumed.append(int(line[4:]))
                if len(resumed) >= 10:
                    break
        assert seen == sorted(seen) and resumed[0] == seen[-1] + 1
        all_ids = [e["seq"] for e in svc.get("/api/events?limit=2000").json()]
        assert all_ids[: len(seen) + len(resumed)] == seen + resumed
    finally:
        svc.stop()


@pytest.mark.parametrize("fault", ["during_model_call", "after_submission_before_verify"])
def test_hard_kill_mid_mission_recovers(tmp_path, fault):
    data = tmp_path / "d"
    svc = Service(data, fault=fault)
    _, mid = start_demo_mission(svc)
    rc = svc.proc.wait(30)
    assert rc == 77, "service should have died at the fault point"
    svc2 = Service(data)
    try:
        diag = svc2.get("/api/diagnostics").json()
        rec = diag["startup_reconciliation"]
        assert rec["abandoned_attempts"], rec
        if fault == "during_model_call":
            assert rec["uncertain_model_calls"] == 1
        m = svc2.wait_mission(mid)
        assert m["mission"]["status"] == "completed"
        if fault == "during_model_call":
            att = svc2.get("/api/attention").json()
            assert any(a["kind"] == "unresolved_charge" for a in att), "uncertain call must stay visible"
        assert diag["artifact_integrity"]["corrupt"] == [] and diag["artifact_integrity"]["missing"] == []
        keys = [t["key"] for t in m["tasks"]]
        accs = [t for t in m["tasks"] if t["latest_acceptance"]]
        assert len(accs) == len(keys)
    finally:
        svc2.stop()


def test_backup_restore_excludes_credentials(tmp_path):
    data = tmp_path / "d"
    svc = Service(data)
    try:
        prov = svc.post("/api/providers", {"kind": "openai", "label": "OpenAI (test)", "api_key": FAKE_KEY}).json()
        assert prov["has_key"] and "api_key" not in json.dumps(prov) and FAKE_KEY not in json.dumps(prov)
        _, mid = start_demo_mission(svc)
        assert svc.wait_mission(mid)["mission"]["status"] == "completed"
        before = svc.get(f"/api/missions/{mid}/artifacts").json()
    finally:
        svc.stop()
    zip_path = tmp_path / "backup.zip"
    env = {**os.environ, "NEXUS_SECRET_BACKEND": "memory"}
    out = subprocess.run([PY, "-m", "nexus", "backup", "--data-dir", str(data), "--to", str(zip_path)], env=env,
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["credentials_included"] is False
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            assert FAKE_KEY.encode() not in z.read(name), f"credential leaked into {name}"
    v = subprocess.run([PY, "-m", "nexus", "verify-backup", "--from", str(zip_path)], capture_output=True, text=True)
    assert v.returncode == 0 and json.loads(v.stdout)["ok"]
    restored = tmp_path / "restored"
    subprocess.run([PY, "-m", "nexus", "restore", "--data-dir", str(restored), "--from", str(zip_path)], env=env,
                   check=True, capture_output=True)
    svc2 = Service(restored)
    try:
        diag = svc2.get("/api/diagnostics").json()
        assert diag["artifact_integrity"]["corrupt"] == [] and diag["artifact_integrity"]["missing"] == []
        after = svc2.get(f"/api/missions/{mid}/artifacts").json()
        assert {a["sha256"] for a in after} == {a["sha256"] for a in before}
        provs = {p["label"]: p for p in svc2.get("/api/providers").json()}
        assert provs["OpenAI (test)"]["status"] == "unvalidated"
        assert "re-enter" in provs["OpenAI (test)"]["status_detail"]
        # Tampered backup is refused.
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(zip_path) as zin, zipfile.ZipFile(bad, "w") as zout:
            for item in zin.namelist():
                data_ = zin.read(item)
                if item.startswith("artifacts/") and not zout.namelist().__contains__("tampered"):
                    data_ = data_ + b"x"
                    zout.writestr("tampered", b"")
                zout.writestr(item, data_)
        v2 = subprocess.run([PY, "-m", "nexus", "verify-backup", "--from", str(bad)], capture_output=True, text=True)
        assert v2.returncode == 1 and "corrupt" in v2.stdout
    finally:
        svc2.stop()


# ---- real actions through the real service process ------------------------------------------
import http.server
import threading


class _Dest(http.server.BaseHTTPRequestHandler):
    writes: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/status"):
            key = self.path.split("idempotency_key=")[-1]
            body = json.dumps({"applied": any(w["key"] == key for w in _Dest.writes)}).encode()
        else:
            body = json.dumps({"notes": [w["body"] for w in _Dest.writes]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        _Dest.writes.append({"key": self.headers.get("idempotency-key"), "body": json.loads(self.rfile.read(n)),
                             "auth": self.headers.get("authorization")})
        self.send_response(201)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')


def test_real_action_via_http_and_mcp_connectors(tmp_path):
    _Dest.writes = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Dest)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    svc = Service(tmp_path / "d")
    try:
        p = svc.post("/api/projects/demo/research").json()
        cfg = {"base_url": f"http://127.0.0.1:{httpd.server_address[1]}", "actions": [
            {"name": "post_note", "method": "POST", "path": "/notes", "description": "Post to the team channel",
             "params": {"text": "string"}, "status_path": "/status"}]}
        con = svc.post(f"/api/projects/{p['id']}/connectors", {"kind": "http", "label": "Team", "config": cfg,
                                                               "secret": "local-token-123456"}).json()
        assert con["status"] == "ok" and con["actions"][0]["autonomy"] == "ask" and "local-token-123456" not in json.dumps(con)
        notes_file = tmp_path / "notes.json"
        repo = Path(__file__).resolve().parents[2]
        mcp = svc.post(f"/api/projects/{p['id']}/connectors", {"kind": "mcp", "label": "Notes", "config": {
            "transport": "stdio", "command": sys.executable,
            "args": [str(repo / "fixtures" / "mcp-demo" / "notes_server.py"), str(notes_file)]}}).json()
        assert mcp["status"] == "ok", mcp
        eff = {a["name"]: (a["effect_class"], a["autonomy"]) for a in mcp["actions"]}
        assert eff["list_notes"] == ("read_only", "auto") and eff["clear_notes"] == ("destructive", "deny")
        m = svc.post(f"/api/projects/{p['id']}/missions", {
            "title": "Tell the team", "objective": "Share resident-demand findings with the team",
            "success_conditions": ["Research resident demand using workspace sources",
                                   "Notify the team channel with the findings"],
            "constraints": ["Use only workspace sources"]}).json()
        mid = m["mission"]["id"]
        blocked = svc.wait_mission(mid, statuses=("blocked", "completed", "failed"))
        assert blocked["mission"]["status"] == "blocked"
        attn = [a for a in svc.get(f"/api/attention?mission_id={mid}").json() if a["kind"] == "action_approval"]
        assert len(attn) == 1 and _Dest.writes == []
        assert svc.post(f"/api/actions/{attn[0]['action_request_id']}/approve", {"note": "go"}).json()["status"] == "succeeded"
        done = svc.wait_mission(mid)
        assert done["mission"]["status"] == "completed", [(t["key"], t["status"], t["status_reason"]) for t in done["tasks"]]
        assert len(_Dest.writes) == 1 and _Dest.writes[0]["auth"] == "Bearer local-token-123456"
        assert _Dest.writes[0]["key"], "idempotency key sent to destination"
        acts = svc.get(f"/api/projects/{p['id']}/actions").json()
        assert [a["status"] for a in acts] == ["succeeded"]
    finally:
        svc.stop()
        httpd.shutdown()


def test_unattended_mission_via_api_needs_no_approval(tmp_path):
    _Dest.writes = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Dest)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    svc = Service(tmp_path / "d")
    try:
        p = svc.post("/api/projects/demo/research").json()
        pid = p["id"]
        # The local test server is on loopback, which the internet guard blocks unless allowlisted.
        r = httpx.put(svc.base + f"/api/projects/{pid}/internet", headers=svc.h, timeout=30,
                      json={"mode": "read_write", "write_autonomy": "ask", "allowed_private_hosts": ["127.0.0.1"]})
        assert r.status_code == 200, r.text
        a = httpx.put(svc.base + f"/api/projects/{pid}/autonomy", headers=svc.h, timeout=30, json={"mode": "unattended"}).json()
        assert a["mode"] == "unattended" and a["monitor"] is True
        m = svc.post(f"/api/projects/{pid}/missions", {
            "title": "Unattended brief", "objective": f"Post the resident-demand findings to http://127.0.0.1:{port}/notes",
            "success_conditions": ["Research resident demand using workspace sources",
                                   f"Post the findings to http://127.0.0.1:{port}/notes"],
            "constraints": ["Use only workspace sources"]}).json()
        mid = m["mission"]["id"]
        done = svc.wait_mission(mid)
        assert done["mission"]["status"] == "completed", [(t["key"], t["status"], t["status_reason"]) for t in done["tasks"]]
        assert len(_Dest.writes) == 1, "posted exactly once, with no human approval"
        d = svc.get(f"/api/projects/{pid}/digest").json()
        assert len(d["actions_done"]) == 1 and d["actions_done"][0]["approval"] == "auto:guarded"
        assert d["actions_waiting"] == [] and d["actions_blocked"] == []
    finally:
        svc.stop()
        httpd.shutdown()
