"""Local utility tools for every agent (capability bundle ``utilities``). Pure computation or
project-scoped reads; nothing here executes code supplied by an agent."""

from __future__ import annotations

import ast
import csv
import io
import json
import math
import operator
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..persistence.db import loads
from .broker import READ_ONLY, ToolContext, ToolDef, ToolDenied, _obj

S, I = {"type": "string"}, {"type": "integer"}

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp, "sin": math.sin, "cos": math.cos,
          "tan": math.tan, "abs": abs, "round": round, "min": min, "max": max, "floor": math.floor, "ceil": math.ceil}
_CONST = {"pi": math.pi, "e": math.e}


def safe_eval(expr: str) -> float:
    """Arithmetic only: numbers, + - * / // % **, parentheses, a few math functions. No names,
    attributes, calls to anything else, or comprehensions."""
    if len(expr) > 500:
        raise ToolDenied("expression too long")

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 1000 or abs(a) > 1e100):
                raise ToolDenied("exponent too large")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Name) and n.id in _CONST:
            return _CONST[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ToolDenied(f"unsupported expression element: {type(n).__name__}")

    return ev(ast.parse(expr, mode="eval"))


async def calculator(broker, ctx: ToolContext, args: dict[str, Any]) -> Any:
    try:
        return {"expression": args["expression"], "result": safe_eval(str(args["expression"]))}
    except (ZeroDivisionError, OverflowError, ValueError, SyntaxError) as exc:
        raise ToolDenied(f"cannot evaluate: {type(exc).__name__}: {exc}") from None


async def date_time(broker, ctx: ToolContext, args: dict[str, Any]) -> Any:
    tz = args.get("timezone") or "UTC"
    try:
        z = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        raise ToolDenied(f"unknown timezone {tz}") from None
    now = datetime.now(timezone.utc).astimezone(z)
    out: dict[str, Any] = {"now": now.isoformat(timespec="seconds"), "timezone": tz, "weekday": now.strftime("%A")}
    if args.get("add_days") is not None:
        base = date.fromisoformat(args["from_date"]) if args.get("from_date") else now.date()
        out["result_date"] = (base + timedelta(days=int(args["add_days"]))).isoformat()
    if args.get("from_date") and args.get("to_date"):
        out["days_between"] = (date.fromisoformat(args["to_date"]) - date.fromisoformat(args["from_date"])).days
    return out


async def regex_extract(broker, ctx: ToolContext, args: dict[str, Any]) -> Any:
    text, pattern = str(args["text"])[:500_000], str(args["pattern"])
    if len(pattern) > 300:
        raise ToolDenied("pattern too long")
    try:
        rx = re.compile(pattern, re.M | (re.I if args.get("ignore_case") else 0))
    except re.error as exc:
        raise ToolDenied(f"bad pattern: {exc}") from None
    matches = []
    for m in rx.finditer(text):
        matches.append(m.groupdict() or (list(m.groups()) if m.groups() else m.group(0)))
        if len(matches) >= int(args.get("max_matches", 200)):
            break
    return {"count": len(matches), "matches": matches}


def _path(data: Any, path: str) -> Any:
    cur = data
    for part in [p for p in re.split(r"\.|\[(\d+)\]", path) if p]:
        if isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        elif isinstance(cur, list) and part == "*":
            continue
        elif isinstance(cur, list):
            cur = [c.get(part) if isinstance(c, dict) else None for c in cur]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


async def json_query(broker, ctx: ToolContext, args: dict[str, Any]) -> Any:
    src = args.get("json")
    if args.get("artifact_id"):
        src = _artifact_text(broker, ctx, args["artifact_id"])
    try:
        data = json.loads(src) if isinstance(src, str) else src
    except json.JSONDecodeError as exc:
        raise ToolDenied(f"invalid JSON: {exc}") from None
    return {"path": args.get("path", ""), "value": _path(data, str(args.get("path", "")))}


async def csv_preview(broker, ctx: ToolContext, args: dict[str, Any]) -> Any:
    text = _artifact_text(broker, ctx, args["artifact_id"]) if args.get("artifact_id") else str(args.get("text", ""))
    rows = list(csv.reader(io.StringIO(text[:2_000_000])))
    if not rows:
        return {"columns": [], "rows": 0}
    head, body = rows[0], rows[1:]
    stats = {}
    for i, col in enumerate(head):
        vals = [r[i] for r in body if i < len(r) and r[i] != ""]
        nums = []
        for v in vals:
            try:
                nums.append(float(v.replace(",", "")))
            except ValueError:
                pass
        stats[col] = ({"numeric": True, "min": min(nums), "max": max(nums), "mean": sum(nums) / len(nums), "count": len(nums)}
                      if nums and len(nums) == len(vals) else {"numeric": False, "distinct": len(set(vals)), "count": len(vals)})
    return {"columns": head, "rows": len(body), "sample": body[: int(args.get("sample_rows", 10))], "column_stats": stats}


async def text_stats(broker, ctx: ToolContext, args: dict[str, Any]) -> Any:
    t = str(args["text"])
    words = re.findall(r"\w+", t)
    return {"characters": len(t), "words": len(words), "sentences": len(re.findall(r"[.!?]+(\s|$)", t)),
            "lines": t.count("\n") + 1, "reading_minutes": round(len(words) / 230, 1)}


def _artifact_text(broker, ctx: ToolContext, artifact_id: str) -> str:
    a = broker.db.one("SELECT * FROM artifacts WHERE id = ? AND project_id = ?", (artifact_id, ctx.project_id))
    if a is None:
        raise ToolDenied("artifact not found in this project")
    if a["kind"] == "context_packet":
        raise ToolDenied("context packets are owner-inspectable records, not agent inputs")
    return broker.artifacts.read(a["id"]).decode("utf-8", errors="replace")


async def read_artifact(broker, ctx: ToolContext, args: dict[str, Any]) -> Any:
    text = _artifact_text(broker, ctx, args["artifact_id"])
    mc = int(args.get("max_chars", 30000))
    return {"artifact_id": args["artifact_id"], "text": text[:mc], "truncated": len(text) > mc, "untrusted": True}


async def list_evidence(broker, ctx: ToolContext, args: dict[str, Any]) -> Any:
    rows = broker.db.query("SELECT id, title, locator, lineage_root, status, kind FROM evidence WHERE mission_id IS ?"
                           " ORDER BY created_at", (ctx.mission_id,))
    return {"evidence": [dict(r) for r in rows]}


async def remember(broker, ctx: ToolContext, args: dict[str, Any]) -> Any:
    if broker.memory is None:
        raise ToolDenied("memory unavailable")
    note = str(args["note"])[:2000]
    r = broker.memory.propose(proposer=f"agent:{ctx.agent_id}", project_id=ctx.project_id, layer="working",
                              access="private", owner_agent_id=ctx.agent_id, content=note, source_refs=[],
                              mission_id=ctx.mission_id, circumstances=f"task {ctx.task_id}")
    return {"status": r["status"], "memory_id": r.get("item_id"), "problems": r.get("problems"),
            "note": "private working memory; it is not evidence and cannot be cited"}


def register(broker) -> None:
    tools = [
        ToolDef("calculator", READ_ONLY, "Evaluate arithmetic (+ - * / // % **, sqrt, log, sin, round, min, max, pi, e).",
                _obj({"expression": S}, ["expression"]), calculator),
        ToolDef("date_time", READ_ONLY, "Current date/time in a timezone; add days to a date; days between dates.",
                _obj({"timezone": S, "from_date": S, "to_date": S, "add_days": I}, []), date_time),
        ToolDef("regex_extract", READ_ONLY, "Extract regex matches from text.",
                _obj({"text": S, "pattern": S, "ignore_case": {"type": "boolean"}, "max_matches": I}, ["text", "pattern"]),
                regex_extract),
        ToolDef("json_query", READ_ONLY, "Read a value from JSON (inline or artifact) by path like a.b[0].c or items.name.",
                _obj({"json": S, "artifact_id": S, "path": S}, ["path"]), json_query),
        ToolDef("csv_preview", READ_ONLY, "Columns, row count, sample rows and per-column stats of CSV (inline or artifact).",
                _obj({"text": S, "artifact_id": S, "sample_rows": I}, []), csv_preview),
        ToolDef("text_stats", READ_ONLY, "Word, sentence and character counts of text.", _obj({"text": S}, ["text"]), text_stats),
        ToolDef("read_artifact", READ_ONLY, "Read a project artifact (e.g. a downloaded file) as text.",
                _obj({"artifact_id": S, "max_chars": I}, ["artifact_id"]), read_artifact),
        ToolDef("list_evidence", READ_ONLY, "List evidence recorded in this mission (ids to cite as [E:id]).", _obj({}, []),
                list_evidence),
        ToolDef("remember", READ_ONLY, "Save a private working note for yourself (persists across your tasks).",
                _obj({"note": S}, ["note"]), remember),
    ]
    for t in tools:
        broker.register(t)
    broker.capability_bundles["utilities"] = {t.name for t in tools}
