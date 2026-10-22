"""Connector adapters: how NEXUS talks to external services.

Each adapter declares actions (name, schema, effect class) and executes them. Writes carry an
idempotency key; adapters whose destination can be queried implement ``reconcile`` so an
ambiguous outcome (timeout after send) is resolved by asking the destination, never by retrying.
Credentials are passed in-process and only placed in request headers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

READ_ONLY, EXTERNAL_WRITE, DESTRUCTIVE = "read_only", "external_write", "destructive"


@dataclass
class ActionSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    effect_class: str
    hints: dict[str, Any] = field(default_factory=dict)


class ConnectorError(Exception):
    pass


class AmbiguousOutcome(Exception):
    """The request may have taken effect remotely; the outcome must be reconciled."""


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    """Minimal JSON-schema check: required keys, no unknown keys when closed, primitive types."""
    errs = []
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    props = schema.get("properties", {})
    for r in schema.get("required", []):
        if r not in args:
            errs.append(f"missing required argument '{r}'")
    if schema.get("additionalProperties") is False:
        for k in args:
            if k not in props:
                errs.append(f"unknown argument '{k}'")
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}
    for k, v in args.items():
        t = props.get(k, {}).get("type")
        if t in types and not isinstance(v, types[t]):
            errs.append(f"argument '{k}' must be {t}")
    return errs


class HttpConnector:
    """Generic HTTP API. The OWNER declares every action (method + path template); agents can only
    fill in arguments - they cannot reach arbitrary URLs.

    config = {"base_url": "https://api.example.com", "auth_header": "Authorization",
              "auth_prefix": "Bearer ", "actions": [{"name", "method", "path", "description",
              "params": {name: type}, "body_params": [names], "effect_class", "status_path"?}]}"""

    kind = "http"

    def __init__(self, config: dict[str, Any], secret: str | None, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = config
        self.secret = secret
        self.transport = transport
        base = config.get("base_url", "")
        if not re.match(r"^https?://", base):
            raise ConnectorError("base_url must be http(s)")

    def _headers(self, idem: str | None = None) -> dict[str, str]:
        h = {"accept": "application/json", "user-agent": "NEXUS-local/0.1"}
        if self.secret:
            h[self.cfg.get("auth_header", "Authorization")] = f"{self.cfg.get('auth_prefix', 'Bearer ')}{self.secret}"
        if idem:
            h["idempotency-key"] = idem
        return h

    async def discover(self) -> list[ActionSpec]:
        out = []
        for a in self.cfg.get("actions", []):
            params = a.get("params", {})
            out.append(ActionSpec(a["name"], a.get("description", a["name"]),
                                  _obj({k: {"type": v} for k, v in params.items()}, list(params)),
                                  a.get("effect_class") or (READ_ONLY if a.get("method", "GET").upper() == "GET" else EXTERNAL_WRITE),
                                  {"method": a.get("method", "GET").upper(), "declared_by": "owner"}))
        return out

    def _action(self, name: str) -> dict[str, Any]:
        for a in self.cfg.get("actions", []):
            if a["name"] == name:
                return a
        raise ConnectorError(f"unknown action {name}")

    def _url(self, a: dict[str, Any], args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        path = a["path"]
        used = set()
        for k in re.findall(r"\{(\w+)\}", path):
            if k not in args:
                raise ConnectorError(f"missing path argument {k}")
            path = path.replace("{" + k + "}", quote(str(args[k]), safe=""))
            used.add(k)
        return self.cfg["base_url"].rstrip("/") + path, {k: v for k, v in args.items() if k not in used}

    async def execute(self, name: str, args: dict[str, Any], *, idempotency_key: str, timeout_s: float = 20) -> Any:
        a = self._action(name)
        method = a.get("method", "GET").upper()
        url, rest = self._url(a, args)
        body = {k: rest[k] for k in a.get("body_params", list(rest))} if method != "GET" else None
        params = rest if method == "GET" else None
        try:
            async with httpx.AsyncClient(timeout=timeout_s, transport=self.transport, follow_redirects=False) as c:
                r = await c.request(method, url, json=body, params=params,
                                    headers=self._headers(idempotency_key if method != "GET" else None))
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            if method == "GET":
                raise ConnectorError(f"read failed: {type(exc).__name__}") from None
            raise AmbiguousOutcome(f"{method} sent but no response ({type(exc).__name__})") from None
        except httpx.HTTPError as exc:
            raise ConnectorError(f"connection failed before sending: {type(exc).__name__}") from None
        if r.status_code >= 400:
            raise ConnectorError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return {"status": r.status_code, "text": r.text[:4000]}

    async def reconcile(self, name: str, args: dict[str, Any], *, idempotency_key: str) -> str:
        a = self._action(name)
        sp = a.get("status_path")
        if not sp:
            return "unknown"
        url = self.cfg["base_url"].rstrip("/") + sp
        try:
            async with httpx.AsyncClient(timeout=15, transport=self.transport) as c:
                r = await c.get(url, params={"idempotency_key": idempotency_key}, headers=self._headers())
        except httpx.HTTPError:
            return "unknown"
        if r.status_code == 200:
            applied = r.json().get("applied")
            return "succeeded" if applied is True else "failed" if applied is False else "unknown"
        return "unknown"

    async def validate(self) -> str:
        return f"{len(self.cfg.get('actions', []))} owner-declared action(s)"


class GitHubConnector:
    """GitHub REST for one repository. Writes embed a hidden marker with the idempotency key, so
    an ambiguous create can be reconciled by searching for the marker instead of re-posting."""

    kind = "github"

    def __init__(self, config: dict[str, Any], secret: str | None, transport: httpx.AsyncBaseTransport | None = None):
        repo = config.get("repo", "")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ConnectorError("repo must look like owner/name")
        self.repo = repo
        self.base = config.get("api_url", "https://api.github.com").rstrip("/")
        self.secret = secret
        self.transport = transport

    def _h(self) -> dict[str, str]:
        h = {"accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28", "user-agent": "NEXUS-local/0.1"}
        if self.secret:
            h["authorization"] = f"Bearer {self.secret}"
        return h

    @staticmethod
    def marker(key: str) -> str:
        return f"<!-- nexus-op:{key} -->"

    async def discover(self) -> list[ActionSpec]:
        S, I = {"type": "string"}, {"type": "integer"}
        return [
            ActionSpec("list_issues", "List open issues (title, number, url).", _obj({"state": S}, []), READ_ONLY),
            ActionSpec("get_issue", "Read one issue with its body.", _obj({"number": I}, ["number"]), READ_ONLY),
            ActionSpec("create_issue", "Open a new issue in the repository.", _obj({"title": S, "body": S}, ["title", "body"]),
                       EXTERNAL_WRITE),
            ActionSpec("comment_issue", "Add a comment to an issue.", _obj({"number": I, "body": S}, ["number", "body"]),
                       EXTERNAL_WRITE),
            ActionSpec("close_issue", "Close an issue.", _obj({"number": I}, ["number"]), DESTRUCTIVE,
                       {"note": "reversible in GitHub, but visible to others; treated as destructive"}),
        ]

    async def _req(self, method: str, path: str, *, json_body: Any = None, params: Any = None, timeout_s: float = 20):
        try:
            async with httpx.AsyncClient(timeout=timeout_s, transport=self.transport) as c:
                r = await c.request(method, f"{self.base}{path}", json=json_body, params=params, headers=self._h())
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            if method == "GET":
                raise ConnectorError(f"read failed: {type(exc).__name__}") from None
            raise AmbiguousOutcome(f"{method} {path} sent but no response") from None
        except httpx.HTTPError as exc:
            raise ConnectorError(f"connection failed: {type(exc).__name__}") from None
        if r.status_code in (401, 403):
            raise ConnectorError(f"GitHub rejected credentials (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise ConnectorError(f"GitHub HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def execute(self, name: str, args: dict[str, Any], *, idempotency_key: str, timeout_s: float = 20) -> Any:
        r = f"/repos/{self.repo}"
        if name == "list_issues":
            data = await self._req("GET", f"{r}/issues", params={"state": args.get("state", "open"), "per_page": 50})
            return [{"number": i["number"], "title": i["title"], "url": i.get("html_url")} for i in data
                    if "pull_request" not in i]
        if name == "get_issue":
            i = await self._req("GET", f"{r}/issues/{int(args['number'])}")
            return {"number": i["number"], "title": i["title"], "body": i.get("body"), "state": i.get("state")}
        mk = self.marker(idempotency_key)
        if name == "create_issue":
            i = await self._req("POST", f"{r}/issues", json_body={"title": args["title"], "body": f"{args['body']}\n\n{mk}"})
            return {"number": i["number"], "url": i.get("html_url")}
        if name == "comment_issue":
            c = await self._req("POST", f"{r}/issues/{int(args['number'])}/comments", json_body={"body": f"{args['body']}\n\n{mk}"})
            return {"comment_id": c.get("id"), "url": c.get("html_url")}
        if name == "close_issue":
            i = await self._req("PATCH", f"{r}/issues/{int(args['number'])}", json_body={"state": "closed"})
            return {"number": i["number"], "state": i.get("state")}
        raise ConnectorError(f"unknown action {name}")

    async def reconcile(self, name: str, args: dict[str, Any], *, idempotency_key: str) -> str:
        mk = self.marker(idempotency_key)
        try:
            if name == "create_issue":
                items = await self._req("GET", f"/repos/{self.repo}/issues", params={"state": "all", "per_page": 100})
                return "succeeded" if any(mk in (i.get("body") or "") for i in items) else "failed"
            if name == "comment_issue":
                items = await self._req("GET", f"/repos/{self.repo}/issues/{int(args['number'])}/comments",
                                        params={"per_page": 100})
                return "succeeded" if any(mk in (c.get("body") or "") for c in items) else "failed"
            if name == "close_issue":
                i = await self._req("GET", f"/repos/{self.repo}/issues/{int(args['number'])}")
                return "succeeded" if i.get("state") == "closed" else "failed"
        except ConnectorError:
            return "unknown"
        return "unknown"

    async def validate(self) -> str:
        info = await self._req("GET", f"/repos/{self.repo}")
        return f"repository {info.get('full_name', self.repo)} reachable"


class McpConnector:
    """Model Context Protocol server over stdio (local command) or streamable HTTP (URL).

    Effect classes come from tool annotations: readOnlyHint -> read_only, destructiveHint ->
    destructive, anything else -> external_write (conservative: unknown side effects). MCP has no
    generic idempotency or status lookup, so ambiguous outcomes stay 'uncertain' until the owner
    resolves them."""

    kind = "mcp"

    def __init__(self, config: dict[str, Any], secret: str | None, server_override: Any = None):
        self.cfg = config
        self.secret = secret
        self.server_override = server_override  # in-process server (tests only)

    def _target(self):
        if self.server_override is not None:
            return self.server_override
        from mcp import StdioServerParameters

        if self.cfg.get("transport") == "stdio":
            env = dict(self.cfg.get("env") or {})
            if self.secret and self.cfg.get("secret_env"):
                env[self.cfg["secret_env"]] = self.secret
            return StdioServerParameters(command=self.cfg["command"], args=list(self.cfg.get("args", [])),
                                         env=env or None, cwd=self.cfg.get("cwd"))
        url = self.cfg.get("url", "")
        if not re.match(r"^https?://", url):
            raise ConnectorError("MCP url must be http(s) or use transport=stdio")
        return url

    async def discover(self) -> list[ActionSpec]:
        from mcp import Client

        out = []
        async with Client(self._target(), read_timeout_seconds=30) as c:
            res = await c.list_tools()
            for t in res.tools:
                a = t.annotations
                if a is not None and a.read_only_hint:
                    eff = READ_ONLY
                elif a is not None and a.destructive_hint:
                    eff = DESTRUCTIVE
                else:
                    eff = EXTERNAL_WRITE
                hints = {} if a is None else {k: v for k, v in a.model_dump().items() if v is not None}
                out.append(ActionSpec(t.name, t.description or t.name, t.input_schema or {"type": "object"}, eff, hints))
        return out

    async def execute(self, name: str, args: dict[str, Any], *, idempotency_key: str, timeout_s: float = 60) -> Any:
        from mcp import Client

        try:
            async with Client(self._target(), read_timeout_seconds=timeout_s) as c:
                r = await c.call_tool(name, args, meta={"io.nexus/idempotencyKey": idempotency_key})
        except TimeoutError as exc:
            raise AmbiguousOutcome(f"MCP tool {name} timed out") from exc
        except Exception as exc:  # transport errors before a call cannot have side effects we know of
            raise ConnectorError(f"MCP call failed: {type(exc).__name__}: {exc}"[:300]) from None
        texts = [getattr(x, "text", None) for x in r.content]
        if r.is_error:
            raise ConnectorError(f"MCP tool error: {' '.join(t for t in texts if t)[:300]}")
        return {"structured": r.structured_content, "text": "\n".join(t for t in texts if t)[:20000]}

    async def reconcile(self, name: str, args: dict[str, Any], *, idempotency_key: str) -> str:
        return "unknown"

    async def validate(self) -> str:
        return f"{len(await self.discover())} tool(s) available"


def json_short(v: Any, n: int = 4000) -> str:
    s = json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "...(truncated)"
