"""Live activity log: follows the durable event log and writes, as things happen,
  activity/activity-YYYYMMDD.log    one readable line per step
  activity/activity-YYYYMMDD.jsonl  the same events with full detail (model output excerpts, tool args/results)
Default folder: <data dir>/../activity (installed app: %APPDATA%/NEXUS/activity); NEXUS_ACTIVITY_DIR overrides.
Contains mission content (sources, outputs); never credentials (they are not in NEXUS records at all).
A cursor file makes restarts resume without duplicating lines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .persistence.db import loads

log = logging.getLogger("nexus.activity")
EXCERPT = 1500


def _short(v: Any, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class ActivityLog:
    def __init__(self, ctx, folder: Path | None = None):
        self.ctx = ctx
        self.folder = Path(os.environ.get("NEXUS_ACTIVITY_DIR") or folder or (Path(ctx.data_dir).parent / "activity"))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.cursor_file = self.folder / ".cursor"
        try:
            self.cursor = int(self.cursor_file.read_text().strip() or 0)
        except (OSError, ValueError):
            self.cursor = 0
        self._names: dict[str, str] = {}
        self._tasks: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    # ---- lookups ------------------------------------------------------------------------
    def agent(self, aid: str | None) -> str:
        if not aid:
            return "?"
        aid = aid.removeprefix("agent:")
        if aid not in self._names:
            r = self.ctx.db.one("SELECT a.name, v.role_default FROM agent_identities a JOIN agent_versions v ON"
                                " v.id = a.current_version_id WHERE a.id = ?", (aid,))
            self._names[aid] = f"{r['name']} ({r['role_default']})" if r else aid
        return self._names[aid]

    def task(self, tid: str | None) -> str:
        if not tid:
            return "-"
        if tid not in self._tasks:
            r = self.ctx.db.one("SELECT key FROM tasks WHERE id = ?", (tid,))
            self._tasks[tid] = r["key"] if r else tid
        return self._tasks[tid]

    def _output_excerpt(self, call_id: str) -> dict[str, Any]:
        c = self.ctx.db.one("SELECT * FROM model_calls WHERE id = ?", (call_id,))
        if c is None:
            return {}
        out: dict[str, Any] = {"model": c["model"], "provider": c["provider_kind"], "purpose": c["purpose"],
                               "input_tokens": c["input_tokens"], "output_tokens": c["output_tokens"],
                               "actual_usd": c["actual_usd"], "status": c["status"], "error": c["error_detail"]}
        if c["output_artifact_id"]:
            try:
                rec = json.loads(self.ctx.artifacts.read_text(c["output_artifact_id"]))
                out["text"] = (rec.get("text") or "")[:EXCERPT]
                out["tool_calls"] = [{"name": t["name"], "arguments": _short(t.get("arguments"), 400)}
                                     for t in rec.get("tool_calls", [])]
                if rec.get("reasoning"):
                    out["reasoning_excerpt"] = rec["reasoning"][:600]
            except Exception:
                pass
        return out

    # ---- formatting ---------------------------------------------------------------------
    def describe(self, e: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        p, t, extra = e["payload"], e["type"], {}
        who = self.agent(e["actor"]) if e["actor"].startswith(("agent:", "agt_")) else e["actor"]
        task = self.task(e["task_id"])
        if t == "task.transition":
            line = f"[{task}] {p.get('from')} -> {p.get('to')}: {_short(p.get('reason'), 200)}"
        elif t == "attempt.started":
            line = f"[{task}] attempt #{p.get('attempt_no')} started by {self.agent(p.get('agent_id'))}" + \
                   (" (resumed from checkpoint)" if p.get("resumed_from_checkpoint") else "")
        elif t == "model_call.started":
            line = f"[{task}] {who} -> {p.get('provider')}:{p.get('model')} {p.get('purpose')} (reserved ${p.get('reserved_usd')})"
        elif t == "model_call.finished":
            extra = self._output_excerpt(p.get("model_call_id"))
            tools = ", ".join(tc["name"] for tc in extra.get("tool_calls", []))
            line = (f"[{task}] {who} <- {p.get('status')} {extra.get('input_tokens')}/{extra.get('output_tokens')} tok"
                    f" ${(p.get('actual_usd') or 0):.5f}" + (f" calls tools: {tools}" if tools else "")
                    + (f" ERROR {p.get('error_kind')}: {_short(p.get('detail'), 200)}" if p.get("error_kind") else "")
                    + (f" | {_short(extra.get('text'), 200)}" if extra.get("text") and not tools else ""))
        elif t.startswith("tool."):
            extra = {"args": p.get("args"), "result": p.get("result")}
            line = f"[{task}] {who} tool {p.get('tool')} {t.split('.')[1]}" + \
                   (f" args={_short(p.get('args'), 160)}" if p.get("args") else "") + \
                   (f" -> {_short(p.get('result'), 200)}" if p.get("result") else "") + \
                   (f" reason: {_short(p.get('reason'), 200)}" if p.get("reason") else "")
        elif t == "review.recorded":
            line = f"[{task}] review {p.get('criterion')} {p.get('check')}: {p.get('verdict').upper()} ({p.get('independence')}) {_short(p.get('summary'), 200)}"
        elif t == "submission.received":
            line = f"[{task}] submission round {p.get('round')} artifact {p.get('artifact_id')}" + \
                   (f" STALE: {p.get('stale_reason')}" if p.get("stale_reason") else "")
        elif t == "task.accepted":
            line = f"[{task}] ACCEPTED sha256={str(p.get('sha256'))[:12]}"
        elif t == "message.posted":
            line = f"[{task}] {who} {p.get('type')} -> {', '.join(p.get('recipients', []))}: {_short(p.get('body'), 220)}"
        elif t.startswith("guard."):
            failed = [c for c in p.get("checks", []) if not c.get("passed")]
            line = f"[{task}] GUARD {t.split('.')[1].upper()}: " + (
                _short(p.get("summary"), 240) if t != "guard.injection_suspected" else
                f"suspicious content in {_short(p.get('source'), 120)} ({[m.get('kind') for m in p.get('matches', [])]})")
            extra = {"failed_checks": failed}
        elif t.startswith("action."):
            line = f"[{task}] ACTION {p.get('action', '')} {t.split('.')[1]}" + \
                   (f": {_short(p.get('rationale') or p.get('note') or p.get('error'), 200)}" if (p.get('rationale') or p.get('note') or p.get('error')) else "")
        elif t == "mission.status":
            line = f"MISSION {p.get('from')} -> {p.get('to')}: {_short(p.get('reason'), 200)}"
        elif t == "plan.materialized":
            line = f"PLAN created {p.get('created')} task(s): {', '.join(p.get('keys', []))}"
        elif t.startswith("attention."):
            line = f"[{task}] NEEDS ATTENTION ({t.split('.')[1]}): {_short(p.get('reason') or p.get('detail') or p.get('title') or p, 220)}"
        else:
            line = f"[{task}] {t} {_short(p, 220)}"
        return line, extra

    # ---- loop ---------------------------------------------------------------------------
    def process(self) -> int:
        from .persistence.events import read_events

        evs = read_events(self.ctx.db, after=self.cursor, limit=500)
        if not evs:
            return 0
        day = datetime.now().strftime("%Y%m%d")
        with open(self.folder / f"activity-{day}.log", "a", encoding="utf-8") as human, \
                open(self.folder / f"activity-{day}.jsonl", "a", encoding="utf-8") as full:
            for e in evs:
                try:
                    line, extra = self.describe(e)
                except Exception as exc:  # never let logging break the service
                    line, extra = f"{e['type']} (unformatted: {type(exc).__name__})", {}
                ts = e["created_at"][11:19]
                mission = (e["mission_id"] or "")[-6:]
                human.write(f"{ts} {mission:>6} {line}\n")
                full.write(json.dumps({**e, "summary": line, "detail": extra}, default=str, ensure_ascii=False) + "\n")
                self.cursor = e["seq"]
        self.cursor_file.write_text(str(self.cursor))
        return len(evs)

    async def run(self) -> None:
        while True:
            try:
                while self.process():
                    pass
            except Exception:
                log.exception("activity log failed")
            await self.ctx.notifier.wait(1.0)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="activity-log")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self.process()
        except Exception:
            pass
