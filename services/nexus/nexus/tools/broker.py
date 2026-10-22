"""Tool broker: the only way agents touch files, the network, repositories, or sandboxes.

Policy is enforced here, from application state (agent version permissions, owner grants,
workspace roots, domain allowlists). Text from prompts, retrieved documents, repository files or
tool results can never grant a permission: there is no tool that changes policy, budgets,
permissions or credentials, and unknown tool names are denied and recorded.
"""

from __future__ import annotations

import fnmatch
import json
import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..persistence.db import Database, dumps, loads, new_id, now_iso
from ..persistence.events import emit
from ..providers.base import ToolSpec

READ_ONLY, LOCAL_WRITE, EXTERNAL_WRITE, DESTRUCTIVE = "read_only", "local_write", "external_write", "destructive"
SECRET_LIKE = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.pfx", "credentials*", "*.kdbx"]
MAX_READ_BYTES = 200_000


class ToolDenied(Exception):
    pass


class UncertainOperation(Exception):
    """A previous attempt of this operation may have taken effect remotely; do not retry blindly."""


@dataclass
class ToolContext:
    project_id: str
    workspace_root: Path
    agent_id: str
    allowed_tools: set[str]
    mission_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    worktree: Path | None = None
    fetch_allowlist: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDef:
    name: str
    effect: str
    description: str
    schema: dict[str, Any]
    fn: Callable[["ToolBroker", ToolContext, dict[str, Any]], Awaitable[Any]]
    requires_grant: bool = False
    dynamic: bool = False  # connector actions / agent-created tools, resolved per project


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def resolve_within(root: Path, rel: str) -> Path:
    """Resolve a user/model-supplied path strictly inside root (symlinks resolved)."""
    if not isinstance(rel, str) or rel.strip() == "":
        raise ToolDenied("path must be a non-empty string")
    if "\x00" in rel:
        raise ToolDenied("invalid path")
    root_r = root.resolve()
    p = (root_r / rel).resolve()
    if not p.is_relative_to(root_r):
        raise ToolDenied(f"path escapes the allowed workspace root: {rel}")
    for part in p.relative_to(root_r).parts:
        if part == ".git":
            raise ToolDenied("access to .git internals is not permitted")
    if any(fnmatch.fnmatch(p.name.lower(), pat) for pat in SECRET_LIKE):
        raise ToolDenied(f"refusing to read credential-like file: {p.name}")
    return p


class ToolBroker:
    def __init__(self, db: Database, artifacts, *, sandbox=None, http_transport=None, memory=None, web_resolver=None):
        from ..web.client import SafeWebClient

        self.web = SafeWebClient(transport=http_transport, resolver=web_resolver)
        self.db = db
        self.artifacts = artifacts
        self.sandbox = sandbox
        self.http_transport = http_transport
        self.memory = memory
        self.services = None  # AppServices (connectors, objectives, toolsmith, collaboration)
        self.tools: dict[str, ToolDef] = {}
        from . import builtin, util_tools, web_tools  # registers tools

        builtin.register(self)
        web_tools.register(self)  # replaces the allowlist-only fetch_url with the policy-based one
        util_tools.register(self)

    def register(self, tool: ToolDef) -> None:
        self.tools[tool.name] = tool

    def specs_for(self, ctx: ToolContext) -> list[ToolSpec]:
        static = [ToolSpec(t.name, t.description, t.schema) for n, t in sorted(self.tools.items())
                  if n in ctx.allowed_tools and self._granted(ctx, t)]
        return static + [ToolSpec(t.name, t.description, t.schema) for t in self.dynamic_tools(ctx)]

    def dynamic_tools(self, ctx: ToolContext) -> list[ToolDef]:
        """Connector actions (capability 'svc:*') and active agent-created tools ('custom:*')."""
        if self.services is None:
            return []
        out: list[ToolDef] = []
        if "svc:*" in ctx.allowed_tools:
            for a in self.services.connectors.actions_for_project(ctx.project_id):
                out.append(self.services.connector_tooldef(a))
        if "custom:*" in ctx.allowed_tools:
            out += self.services.toolsmith.tooldefs(ctx.project_id)
        return out

    def _dynamic(self, ctx: ToolContext, name: str) -> ToolDef | None:
        if self.services is None or not (name.startswith("svc__") or name.startswith("tool__")):
            return None
        return next((t for t in self.dynamic_tools(ctx) if t.name == name), None)

    def _granted(self, ctx: ToolContext, tool: ToolDef) -> bool:
        if tool.dynamic:
            return True  # gated by per-action autonomy policy (connectors) or by each step (composites)
        if not tool.requires_grant and tool.effect in (READ_ONLY, LOCAL_WRITE):
            return True
        r = self.db.one("SELECT 1 FROM permission_grants WHERE project_id = ? AND capability = ? AND revoked_at IS NULL"
                        " AND granted_by = 'owner' AND (subject = ? OR subject = '*')",
                        (ctx.project_id, tool.name, ctx.agent_id))
        return r is not None

    def _record(self, cur: sqlite3.Cursor, ctx: ToolContext, op_id: str, tool: str, effect: str, args: dict,
                status: str, reason: str | None, idem: str | None = None) -> str:
        oid = new_id("top")
        ts = now_iso()
        cur.execute(
            "INSERT INTO tool_operations (id, operation_id, project_id, mission_id, task_id, attempt_id, actor, tool,"
            " effect_class, args_json, status, decision_reason, idempotency_key, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, op_id, ctx.project_id, ctx.mission_id, ctx.task_id, ctx.attempt_id, ctx.agent_id, tool, effect,
             dumps(_redact_args(args)), status, reason, idem, ts, ts),
        )
        return oid

    async def invoke(self, ctx: ToolContext, name: str, args: dict[str, Any], *, operation_id: str) -> dict[str, Any]:
        """Returns {"ok": bool, "result"|"error": ...}. Never raises for policy denials."""
        prior = self.db.one("SELECT * FROM tool_operations WHERE operation_id = ?", (operation_id,))
        if prior is not None:
            if prior["status"] == "succeeded":
                return {"ok": True, "result": loads(prior["result_json"]), "replayed": True}
            if prior["status"] == "uncertain":
                return {"ok": False, "error": "previous attempt of this operation has an uncertain outcome; it must be"
                                              " reconciled before any retry", "uncertain": True}
            return {"ok": False, "error": f"operation already {prior['status']}: {prior['decision_reason']}"}
        tool = self.tools.get(name) or self._dynamic(ctx, name)
        effect = tool.effect if tool else "destructive"
        reason = None
        if tool is None:
            reason = f"unknown tool '{name}' (no tool can change policy, budgets, permissions or credentials)"
        elif not tool.dynamic and name not in ctx.allowed_tools:
            reason = f"agent lacks capability '{name}'"
        elif not self._granted(ctx, tool):
            reason = f"'{name}' ({tool.effect}) requires an explicit owner grant for this project"
        if reason:
            with self.db.tx() as cur:
                self._record(cur, ctx, operation_id, name, effect, args, "denied", reason)
                emit(cur, "tool.denied", ctx.agent_id, {"tool": name, "reason": reason, "operation_id": operation_id},
                     project_id=ctx.project_id, mission_id=ctx.mission_id, task_id=ctx.task_id)
            return {"ok": False, "error": f"denied: {reason}"}
        idem = hashlib.sha256(operation_id.encode()).hexdigest()[:32] if effect == EXTERNAL_WRITE else None
        with self.db.tx() as cur:
            oid = self._record(cur, ctx, operation_id, name, effect, args, "running", "policy allowed", idem)
        ctx.extra["operation_id"] = operation_id
        try:
            result = await tool.fn(self, ctx, {**args, "_idempotency_key": idem} if idem else args)
        except ToolDenied as exc:
            self._finish(oid, "denied", None, str(exc), ctx, name)
            return {"ok": False, "error": f"denied: {exc}"}
        except UncertainOperation as exc:
            self._finish(oid, "uncertain", None, str(exc), ctx, name)
            return {"ok": False, "error": str(exc), "uncertain": True}
        except Exception as exc:
            self._finish(oid, "failed", None, f"{type(exc).__name__}: {exc}"[:500], ctx, name)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
        self._finish(oid, "succeeded", result, None, ctx, name, args)
        return {"ok": True, "result": result}

    def _finish(self, oid: str, status: str, result: Any, reason: str | None, ctx: ToolContext, name: str,
                args: dict[str, Any] | None = None) -> None:
        with self.db.tx() as cur:
            cur.execute("UPDATE tool_operations SET status=?, result_json=?, decision_reason=COALESCE(?, decision_reason),"
                        " updated_at=? WHERE id=?", (status, dumps(result) if result is not None else None, reason,
                                                      now_iso(), oid))
            if status in ("denied", "uncertain", "failed"):
                emit(cur, f"tool.{status}", ctx.agent_id, {"tool": name, "reason": reason, "tool_operation_id": oid},
                     project_id=ctx.project_id, mission_id=ctx.mission_id, task_id=ctx.task_id)
            elif status == "succeeded":
                emit(cur, "tool.succeeded", ctx.agent_id,
                     {"tool": name, "tool_operation_id": oid, "args": _redact_args(args or {}), "result": _result_summary(result)},
                     project_id=ctx.project_id, mission_id=ctx.mission_id, task_id=ctx.task_id)


_SECRETISH = re.compile(r"(key|token|secret|password|authorization)", re.I)


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in args.items():
        if k.startswith("_"):
            continue
        if _SECRETISH.search(k):
            out[k] = "[REDACTED]"
        elif isinstance(v, str) and len(v) > 2000:
            out[k] = v[:2000] + f"...[{len(v)} chars]"
        else:
            out[k] = v
    return out


def _result_summary(result: Any) -> Any:
    """Compact view of a tool result for the event log: large text fields are truncated."""
    if isinstance(result, dict):
        out = {}
        for k, v in result.items():
            if isinstance(v, str) and len(v) > 300:
                out[k] = v[:300] + f"...[{len(v)} chars]"
            elif isinstance(v, (list, dict)) and len(json.dumps(v, default=str)) > 600:
                out[k] = f"[{type(v).__name__} with {len(v)} items]"
            else:
                out[k] = v
        return out
    s = json.dumps(result, default=str)
    return s[:300] + ("..." if len(s) > 300 else "")
