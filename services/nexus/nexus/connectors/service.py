"""Connected services and real actions.

Lifecycle of an action request (agents ask; NEXUS decides and executes):

    pending --owner approves--> approved --> executing --> succeeded | failed | uncertain
       |  \\--owner denies--> denied                              uncertain --reconcile--> succeeded|failed
       \\-- autonomy 'auto' (within daily quota) --> approved(auto:policy) --> executing ...

Default autonomy is conservative: read-only actions 'auto', external writes 'ask', destructive
'deny'. The owner may loosen per action, except that destructive actions can never be 'auto'.
Every executed action is recorded as observation evidence with its own lineage and posted to
the task's room thread.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..domain import rooms
from ..domain.understanding import register_evidence
from ..persistence.db import Database, dumps, loads, new_id, now_iso
from ..persistence.events import emit
from .adapters import (
    DESTRUCTIVE,
    EXTERNAL_WRITE,
    READ_ONLY,
    AmbiguousOutcome,
    ConnectorError,
    GitHubConnector,
    HttpConnector,
    McpConnector,
    validate_args,
)

DEFAULT_AUTONOMY = {READ_ONLY: "auto", "local_write": "ask", EXTERNAL_WRITE: "ask", DESTRUCTIVE: "deny"}
TERMINAL = {"succeeded", "failed", "denied", "expired", "canceled"}


class ActionPolicyError(Exception):
    pass


def tool_name(connector_label: str, action: str) -> str:
    slug = "".join(ch if ch.isalnum() else "_" for ch in connector_label.lower()).strip("_")[:20] or "svc"
    return f"svc__{slug}__{action}"[:64]


class ConnectorService:
    def __init__(self, db: Database, secrets, artifacts, *, transport=None, web=None):
        self.web = web  # SafeWebClient for the built-in web connector
        self.db = db
        self.secrets = secrets
        self.artifacts = artifacts
        self.transport = transport  # test injection for HTTP/GitHub
        self.mcp_overrides: dict[str, Any] = {}  # connector_id -> in-process MCP server (tests)
        self.on_settled = None  # callback(task_id) when an action reaches a terminal state
        self.on_violation = None  # callback(attempt_id, task_id) when the unattended guard denies an action
        self.monitor = None  # async callable(info) -> {"verdict", "reason"} (independent Sentinel agent)

    # ---- configuration ---------------------------------------------------------------------
    def add(self, *, project_id: str, kind: str, label: str, config: dict[str, Any], secret: str | None) -> str:
        self._build(kind, config, secret, None)  # validates config shape
        ref = self.secrets.put(secret) if secret else None
        cid = new_id("con")
        with self.db.tx() as cur:
            cur.execute("INSERT INTO connectors (id, project_id, kind, label, config_json, secret_ref, created_at)"
                        " VALUES (?,?,?,?,?,?,?)", (cid, project_id, kind, label, dumps(config), ref, now_iso()))
            emit(cur, "connector.added", "owner", {"connector_id": cid, "kind": kind, "label": label,
                                                   "has_secret": bool(secret)}, project_id=project_id)
        return cid

    def remove(self, cid: str) -> None:
        r = self.row(cid)
        if r["kind"] == "web":
            raise ActionPolicyError("the built-in web connector cannot be removed; set the internet mode instead")
        with self.db.tx() as cur:
            cur.execute("UPDATE action_requests SET status = 'canceled', decision_note = 'connector removed' WHERE"
                        " connector_id = ? AND status = 'pending'", (cid,))
            cur.execute("UPDATE connector_actions SET enabled = 0 WHERE connector_id = ?", (cid,))
            cur.execute("UPDATE connectors SET status = 'removed' WHERE id = ?", (cid,))
            emit(cur, "connector.removed", "owner", {"connector_id": cid}, project_id=r["project_id"])
        self.secrets.delete(r["secret_ref"])

    def row(self, cid: str):
        r = self.db.one("SELECT * FROM connectors WHERE id = ?", (cid,))
        if r is None:
            raise KeyError(cid)
        return r

    def _build(self, kind: str, config: dict[str, Any], secret: str | None, cid: str | None):
        if kind == "web":
            from ..web.client import WebPolicy
            from .web_email import WebConnector

            pid = self.row(cid)["project_id"] if cid else None
            p = self.db.one("SELECT settings_json FROM projects WHERE id = ?", (pid,)) if pid else None
            return WebConnector(config, secret, self.web, WebPolicy.from_project(loads(p["settings_json"], {}) if p else {}),
                                pid or "")
        if kind == "email":
            from .web_email import EmailConnector

            return EmailConnector(config, secret)
        if kind == "http":
            return HttpConnector(config, secret, self.transport)
        if kind == "github":
            return GitHubConnector(config, secret, self.transport)
        if kind == "mcp":
            return McpConnector(config, secret, self.mcp_overrides.get(cid) if cid else None)
        raise ConnectorError(f"unknown connector kind {kind}")

    def adapter(self, cid: str):
        r = self.row(cid)
        if r["status"] == "removed":
            raise ConnectorError("connector removed")
        return self._build(r["kind"], loads(r["config_json"], {}), self.secrets.get(r["secret_ref"]), cid)

    async def discover(self, cid: str) -> dict[str, Any]:
        """Validate the connection and upsert its actions. Existing owner autonomy choices are kept;
        an action whose effect class became riskier is reset to the conservative default."""
        r = self.row(cid)
        try:
            ad = self.adapter(cid)
            detail = await ad.validate()
            specs = await ad.discover()
            status = "ok"
        except (ConnectorError, Exception) as exc:  # surface any failure as a status, not a crash
            specs, status, detail = [], "error", f"{type(exc).__name__}: {exc}"[:400]
        rank = {READ_ONLY: 0, "local_write": 1, EXTERNAL_WRITE: 2, DESTRUCTIVE: 3}
        with self.db.tx() as cur:
            for s in specs:
                cur_row = cur.execute("SELECT * FROM connector_actions WHERE connector_id = ? AND name = ?",
                                      (cid, s.name)).fetchone()
                if cur_row is None:
                    cur.execute("INSERT INTO connector_actions (id, connector_id, name, description, input_schema_json,"
                                " effect_class, autonomy, hints_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                (new_id("cac"), cid, s.name, s.description[:1000], dumps(s.input_schema), s.effect_class,
                                 DEFAULT_AUTONOMY[s.effect_class], dumps(s.hints), now_iso()))
                else:
                    autonomy = cur_row["autonomy"]
                    if rank[s.effect_class] > rank[cur_row["effect_class"]]:
                        autonomy = DEFAULT_AUTONOMY[s.effect_class]
                    cur.execute("UPDATE connector_actions SET description = ?, input_schema_json = ?, effect_class = ?,"
                                " autonomy = ?, hints_json = ?, enabled = 1, updated_at = ? WHERE id = ?",
                                (s.description[:1000], dumps(s.input_schema), s.effect_class, autonomy, dumps(s.hints),
                                 now_iso(), cur_row["id"]))
            names = [s.name for s in specs]
            if status == "ok":
                cur.execute(f"UPDATE connector_actions SET enabled = 0 WHERE connector_id = ? AND name NOT IN"
                            f" ({','.join('?' * len(names)) or "''"})", (cid, *names))
            cur.execute("UPDATE connectors SET status = ?, status_detail = ?, validated_at = ? WHERE id = ?",
                        (status, detail, now_iso(), cid))
            emit(cur, "connector.validated", "nexus:connectors", {"connector_id": cid, "status": status,
                                                                   "detail": detail, "actions": names},
                 project_id=r["project_id"])
        return {"status": status, "detail": detail, "actions": names}

    def set_policy(self, action_id: str, *, autonomy: str | None = None, max_auto_per_day: int | None = None,
                   enabled: bool | None = None, actor: str = "owner") -> None:
        if actor != "owner":
            raise PermissionError("only the owner may change action autonomy")
        a = self.db.one("SELECT ca.*, c.project_id FROM connector_actions ca JOIN connectors c ON c.id = ca.connector_id"
                        " WHERE ca.id = ?", (action_id,))
        if a is None:
            raise KeyError(action_id)
        if autonomy is not None:
            if autonomy not in ("deny", "ask", "auto"):
                raise ValueError("autonomy must be deny, ask or auto")
            if autonomy == "auto" and a["effect_class"] == DESTRUCTIVE:
                raise ActionPolicyError("destructive actions always require explicit per-action approval")
        with self.db.tx() as cur:
            cur.execute("UPDATE connector_actions SET autonomy = COALESCE(?, autonomy), max_auto_per_day ="
                        " COALESCE(?, max_auto_per_day), enabled = COALESCE(?, enabled), updated_at = ? WHERE id = ?",
                        (autonomy, max_auto_per_day, None if enabled is None else int(enabled), now_iso(), action_id))
            emit(cur, "connector.policy_changed", "owner", {"action_id": action_id, "action": a["name"],
                                                            "autonomy": autonomy, "max_auto_per_day": max_auto_per_day},
                 project_id=a["project_id"])

    # ---- tools exposed to agents --------------------------------------------------------------
    def actions_for_project(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT ca.*, c.label, c.kind, c.id AS cid FROM connector_actions ca JOIN connectors c ON"
                             " c.id = ca.connector_id WHERE c.project_id = ? AND c.status = 'ok' AND ca.enabled = 1"
                             " AND ca.autonomy != 'deny'", (project_id,))
        return [{"tool": tool_name(r["label"], r["name"]), "connector_id": r["cid"], "action": r["name"],
                 "description": r["description"], "schema": loads(r["input_schema_json"], {}),
                 "effect_class": r["effect_class"], "autonomy": r["autonomy"], "label": r["label"]} for r in rows]

    def resolve_tool(self, project_id: str, name: str) -> dict[str, Any] | None:
        for a in self.actions_for_project(project_id):
            if a["tool"] == name:
                return a
        return None

    # ---- requests ------------------------------------------------------------------------------
    async def request(self, *, project_id: str, mission_id: str | None, task_id: str | None, attempt_id: str | None,
                      requested_by: str, connector_id: str, action: str, args: dict[str, Any], rationale: str,
                      operation_id: str) -> dict[str, Any]:
        existing = self.db.one("SELECT * FROM action_requests WHERE operation_id = ?", (operation_id,))
        if existing is not None:
            return self.view(existing["id"])
        a = self.db.one("SELECT ca.*, c.project_id, c.status AS cstatus FROM connector_actions ca JOIN connectors c ON"
                        " c.id = ca.connector_id WHERE ca.connector_id = ? AND ca.name = ?", (connector_id, action))
        if a is None or a["project_id"] != project_id or not a["enabled"] or a["cstatus"] != "ok":
            raise ActionPolicyError(f"action {action} is not available to this project")
        errs = validate_args(loads(a["input_schema_json"], {}), args)
        status, approval, note = "pending", None, None
        from ..guard.action_guard import autonomy_settings, evaluate

        unattended = autonomy_settings(self.db, project_id)["mode"] == "unattended"
        guard = None
        if errs:
            status, note = "denied", f"invalid arguments: {errs}"
        elif a["autonomy"] == "deny":
            status, note = "denied", "owner policy denies this action"
        elif unattended and a["effect_class"] != READ_ONLY:
            con = self.row(connector_id)
            guard = await evaluate(self.db, project_id=project_id, mission_id=mission_id, task_id=task_id,
                                   attempt_id=attempt_id, connector=con, action=a, args=args, rationale=rationale,
                                   monitor=self.monitor)
            if guard.decision == "allow":
                status, approval, note = "approved", "auto:guarded", "unattended: all guard checks passed"
            elif guard.decision == "deny":
                status, note = "denied", f"guard: {guard.summary}"
            else:
                note = f"guard escalated to owner: {guard.summary}"
        elif a["autonomy"] == "auto":
            used = self.db.one("SELECT COUNT(*) AS n FROM action_requests WHERE connector_id = ? AND action = ? AND"
                               " approval = 'auto:policy' AND created_at > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day')",
                               (connector_id, action))["n"]
            if used >= a["max_auto_per_day"]:
                note = f"daily auto quota ({a['max_auto_per_day']}) used; owner approval needed"
            else:
                status, approval = "approved", "auto:policy"
        rid = new_id("act")
        idem = hashlib.sha256(f"nexus-action:{operation_id}".encode()).hexdigest()[:32]
        with self.db.tx() as cur:
            cur.execute("INSERT INTO action_requests (id, operation_id, project_id, mission_id, task_id, attempt_id,"
                        " requested_by, connector_id, action, args_json, rationale, effect_class, status, approval,"
                        " decided_by, decision_note, idempotency_key, created_at, decided_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, operation_id, project_id, mission_id, task_id, attempt_id, requested_by, connector_id,
                         action, dumps(args), rationale[:2000], a["effect_class"], status, approval,
                         "nexus:policy" if status != "pending" else None, note, idem, now_iso(),
                         now_iso() if status != "pending" else None))
            emit(cur, "action.requested", requested_by,
                 {"action_request_id": rid, "action": action, "connector_id": connector_id, "effect_class": a["effect_class"],
                  "status": status, "rationale": rationale[:300], "note": note},
                 project_id=project_id, mission_id=mission_id, task_id=task_id)
            if status == "pending":
                emit(cur, "attention.action_approval", "nexus:connectors",
                     {"action_request_id": rid, "action": action, "effect_class": a["effect_class"],
                      "rationale": rationale[:300]}, project_id=project_id, mission_id=mission_id, task_id=task_id)
            if task_id:
                rooms.post(cur, operation_id=f"{operation_id}:request", project_id=project_id, mission_id=mission_id,
                           thread_id=rooms.thread_for_task(task_id), sender=requested_by,
                           recipients=["owner"], type="proposal", task_id=task_id,
                           body=f"Requests real action {action} ({a['effect_class']}): {rationale[:500]}",
                           refs={"action_request_id": rid}, expects_response="decision" if status == "pending" else None)
        if guard is not None:
            from ..guard.action_guard import record

            record(self.db, guard, action_request_id=rid, project_id=project_id, mission_id=mission_id, task_id=task_id,
                   attempt_id=attempt_id)
            if guard.decision == "deny" and self.on_violation and attempt_id:
                self.on_violation(attempt_id, task_id)
        if status == "approved":
            await self.execute(rid)
        return self.view(rid)

    async def decide(self, rid: str, *, approve: bool, note: str = "", actor: str = "owner") -> dict[str, Any]:
        if actor != "owner":
            raise PermissionError("only the owner approves actions")
        r = self.db.one("SELECT * FROM action_requests WHERE id = ?", (rid,))
        if r is None:
            raise KeyError(rid)
        if r["status"] != "pending":
            raise ActionPolicyError(f"request is {r['status']}, not pending")
        with self.db.tx() as cur:
            cur.execute("UPDATE action_requests SET status = ?, approval = ?, decided_by = 'owner', decision_note = ?,"
                        " decided_at = ? WHERE id = ? AND status = 'pending'",
                        ("approved" if approve else "denied", "owner" if approve else None, note, now_iso(), rid))
            emit(cur, f"action.{'approved' if approve else 'denied'}", "owner", {"action_request_id": rid, "note": note},
                 project_id=r["project_id"], mission_id=r["mission_id"], task_id=r["task_id"])
        if approve:
            await self.execute(rid)
        else:
            self._settled(rid)
        return self.view(rid)

    async def execute(self, rid: str) -> None:
        """Execute an approved request exactly once from NEXUS's point of view: the transition
        approved -> executing is the claim; a second caller finds it already claimed."""
        with self.db.tx() as cur:
            n = cur.execute("UPDATE action_requests SET status = 'executing', executed_at = ? WHERE id = ? AND"
                            " status = 'approved'", (now_iso(), rid)).rowcount
        if n != 1:
            return
        r = self.db.one("SELECT * FROM action_requests WHERE id = ?", (rid,))
        args = loads(r["args_json"], {})
        status, result, error = "failed", None, None
        try:
            result = await self.adapter(r["connector_id"]).execute(r["action"], args, idempotency_key=r["idempotency_key"])
            status = "succeeded"
        except AmbiguousOutcome as exc:
            status, error = "uncertain", f"{exc}; not retried - reconcile before any retry"
        except ConnectorError as exc:
            status, error = "failed", str(exc)
        except Exception as exc:
            status, error = "uncertain", f"unexpected {type(exc).__name__}: {exc}"[:400]
        self._finish(r, status, result, error)

    def _finish(self, r, status: str, result: Any, error: str | None) -> None:
        with self.db.tx() as cur:
            eid = None
            if status == "succeeded":
                con = cur.execute("SELECT label FROM connectors WHERE id = ?", (r["connector_id"],)).fetchone()
                text = json.dumps({"action": r["action"], "args": loads(r["args_json"], {}), "result": result},
                                  default=str, indent=1)[:200_000]
                art = self.artifacts.create(cur, project_id=r["project_id"], kind="action_result",
                                            title=f"{con['label']}: {r['action']}", content=text,
                                            media_type="application/json", created_by="nexus:connectors",
                                            mission_id=r["mission_id"], task_id=r["task_id"])
                eid, _ = register_evidence(cur, project_id=r["project_id"], mission_id=r["mission_id"], kind="observation",
                                           title=f"Result of {r['action']} via {con['label']}",
                                           locator=f"action:{r['id']}", artifact_id=art["id"], excerpt=text[:4000],
                                           lineage_root=f"action:{r['connector_id']}:{r['action']}:{r['id']}",
                                           created_by="nexus:connectors")
            cur.execute("UPDATE action_requests SET status = ?, result_json = ?, error = ?, evidence_id = ? WHERE id = ?",
                        (status, dumps(result) if result is not None else None, error, eid, r["id"]))
            emit(cur, f"action.{status}", "nexus:connectors",
                 {"action_request_id": r["id"], "action": r["action"], "error": error, "evidence_id": eid},
                 project_id=r["project_id"], mission_id=r["mission_id"], task_id=r["task_id"])
            if status == "uncertain":
                emit(cur, "attention.action_uncertain", "nexus:connectors",
                     {"action_request_id": r["id"], "action": r["action"], "detail": error},
                     project_id=r["project_id"], mission_id=r["mission_id"], task_id=r["task_id"])
            if r["task_id"]:
                rooms.post(cur, operation_id=f"{r['operation_id']}:outcome:{status}", project_id=r["project_id"],
                           mission_id=r["mission_id"], thread_id=rooms.thread_for_task(r["task_id"]),
                           sender="nexus:connectors", recipients=[r["requested_by"], "owner"],
                           type="evidence" if status == "succeeded" else "blocker" if status == "uncertain" else "status",
                           task_id=r["task_id"], body=f"Action {r['action']} {status}" + (f": {error}" if error else ""),
                           refs={"action_request_id": r["id"], **({"evidence_id": eid} if eid else {})})
        self._settled(r["id"])

    def _settled(self, rid: str) -> None:
        r = self.db.one("SELECT task_id, status FROM action_requests WHERE id = ?", (rid,))
        if r and r["task_id"] and r["status"] in TERMINAL and self.on_settled:
            self.on_settled(r["task_id"])

    async def reconcile(self, rid: str) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM action_requests WHERE id = ?", (rid,))
        if r is None:
            raise KeyError(rid)
        if r["status"] != "uncertain":
            return self.view(rid)
        outcome = await self.adapter(r["connector_id"]).reconcile(r["action"], loads(r["args_json"], {}),
                                                                  idempotency_key=r["idempotency_key"])
        if outcome == "unknown":
            with self.db.tx() as cur:
                emit(cur, "action.reconcile_inconclusive", "nexus:connectors", {"action_request_id": rid},
                     project_id=r["project_id"], mission_id=r["mission_id"], task_id=r["task_id"])
            return {**self.view(rid), "reconcile": "destination cannot confirm; resolve manually"}
        self._finish(r, outcome, {"reconciled": True} if outcome == "succeeded" else None,
                     None if outcome == "succeeded" else "reconciled: destination shows no effect")
        return {**self.view(rid), "reconcile": outcome}

    def resolve_manually(self, rid: str, *, outcome: str, note: str, actor: str = "owner") -> dict[str, Any]:
        if actor != "owner":
            raise PermissionError("only the owner resolves uncertain actions")
        if outcome not in ("succeeded", "failed"):
            raise ValueError("outcome must be succeeded or failed")
        r = self.db.one("SELECT * FROM action_requests WHERE id = ?", (rid,))
        if r["status"] != "uncertain":
            raise ActionPolicyError("only uncertain actions can be resolved manually")
        with self.db.tx() as cur:
            cur.execute("UPDATE action_requests SET decision_note = ? WHERE id = ?", (f"owner: {note}", rid))
        self._finish(r, outcome, {"owner_confirmed": True} if outcome == "succeeded" else None,
                     None if outcome == "succeeded" else f"owner marked failed: {note}")
        return self.view(rid)

    def view(self, rid: str) -> dict[str, Any]:
        r = self.db.one("SELECT ar.*, c.label FROM action_requests ar JOIN connectors c ON c.id = ar.connector_id"
                        " WHERE ar.id = ?", (rid,))
        d = dict(r)
        d["args"] = loads(d.pop("args_json"), {})
        d["result"] = loads(d.pop("result_json")) if d.get("result_json") else None
        return d

    def task_action_summary(self, task_id: str, since: str | None = None) -> dict[str, list[str]]:
        q = "SELECT id, status FROM action_requests WHERE task_id = ?" + (" AND created_at >= ?" if since else "")
        rows = self.db.query(q, (task_id, since) if since else (task_id,))
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["status"], []).append(r["id"])
        return out


def internet_policy(db: Database, project_id: str) -> dict[str, Any]:
    p = db.one("SELECT settings_json FROM projects WHERE id = ?", (project_id,))
    net = loads(p["settings_json"], {}).get("internet") or {}
    return {"mode": net.get("mode", "read_write"), "write_autonomy": net.get("write_autonomy", "ask"),
            "blocked_domains": net.get("blocked_domains", []), "allowed_private_hosts": net.get("allowed_private_hosts", []),
            "max_requests_per_minute": int(net.get("max_requests_per_minute", 120)),
            "max_auto_writes_per_day": int(net.get("max_auto_writes_per_day", 200)), "contact": net.get("contact", "")}


async def ensure_web_connector(svc: "ConnectorService", project_id: str) -> str:
    """Every project has exactly one built-in web connector; its write autonomy follows the
    project's internet policy."""
    r = svc.db.one("SELECT id FROM connectors WHERE project_id = ? AND kind = 'web'", (project_id,))
    if r is None:
        cid = new_id("con")
        with svc.db.tx() as cur:
            cur.execute("INSERT INTO connectors (id, project_id, kind, label, config_json, created_at) VALUES (?,?,?,?,?,?)",
                        (cid, project_id, "web", "Web", dumps({"builtin": True}), now_iso()))
        await svc.discover(cid)
    else:
        cid = r["id"]
    apply_internet_policy(svc, project_id)
    return cid


def apply_internet_policy(svc: "ConnectorService", project_id: str) -> None:
    pol = internet_policy(svc.db, project_id)
    writes = "deny" if pol["mode"] != "read_write" else ("auto" if pol["write_autonomy"] == "auto" else "ask")
    deletes = "deny" if pol["mode"] != "read_write" else "ask"  # deletes are never pre-approved
    with svc.db.tx() as cur:
        c = cur.execute("SELECT id FROM connectors WHERE project_id = ? AND kind = 'web'", (project_id,)).fetchone()
        if c is None:
            return
        cur.execute("UPDATE connector_actions SET autonomy = ?, max_auto_per_day = ?, updated_at = ? WHERE connector_id = ?"
                    " AND effect_class = 'external_write'", (writes, int(pol.get("max_auto_writes_per_day", 200)), now_iso(), c["id"]))
        cur.execute("UPDATE connector_actions SET autonomy = ?, updated_at = ? WHERE connector_id = ? AND effect_class ="
                    " 'destructive'", (deletes, now_iso(), c["id"]))
        cur.execute("UPDATE connectors SET status = 'ok', status_detail = ? WHERE id = ?",
                    (f"internet {pol['mode']}; writes: {writes}; deletes: {deletes}", c["id"]))
