"""Narrator: posts the milestones of chat-started missions back into their conversation.

Follows the durable event log (like the activity log) with its own cursor, so a restart neither loses nor repeats
messages (each message also carries a dedup key). Only milestones are posted (the plan, finished steps, real actions,
things that need the owner, the result); the live step-by-step activity is shown by the app from the event stream.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..persistence.db import dumps, loads, now_iso

log = logging.getLogger("nexus.chat")
CURSOR_KEY = "chat.narrator_cursor"
RESULT_CHARS = 60000


class Narrator:
    def __init__(self, ctx, chat):
        self.ctx = ctx
        self.chat = chat
        self.db = ctx.db
        r = self.db.one("SELECT value_json FROM settings WHERE key = ?", (CURSOR_KEY,))
        if r is None:  # first run: start from now, do not narrate history
            top = self.db.one("SELECT COALESCE(MAX(seq), 0) AS s FROM events")["s"]
            self.cursor = int(top)
        else:
            self.cursor = int(loads(r["value_json"], 0))
        self._runner: asyncio.Task | None = None

    def _conv(self, mission_id: str | None) -> str | None:
        if not mission_id:
            return None
        r = self.db.one("SELECT conversation_id FROM conversation_missions WHERE mission_id = ?", (mission_id,))
        return r["conversation_id"] if r else None

    def process(self) -> int:
        from ..persistence.events import read_events

        evs = read_events(self.db, after=self.cursor, limit=500)
        if not evs:
            return 0
        for e in evs:
            try:
                cid = self._conv(e["mission_id"])
                if cid:
                    self._narrate(cid, e)
            except Exception:
                log.exception("narrator failed on event %s", e.get("seq"))
            self.cursor = e["seq"]
        with self.db.tx() as cur:
            cur.execute("INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?,?,?)",
                        (CURSOR_KEY, dumps(self.cursor), now_iso()))
        return len(evs)

    def _get_task(self, task_id: str | None) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,)) if task_id else None
        return dict(r) if r else None

    def _narrate(self, cid: str, e: dict[str, Any]) -> None:
        t, p, mid = e["type"], e["payload"], e["mission_id"]
        post = lambda kind, text, data=None, dedup=None: self._post(cid, kind, text, data, mid, dedup)
        if t == "plan.materialized":
            rows = self.db.query("SELECT key, title FROM tasks WHERE mission_id = ? AND key IN (%s)" %
                                 ",".join("?" * len(p.get("keys", []))), (mid, *p.get("keys", []))) if p.get("keys") else []
            titles = {r["key"]: r["title"] for r in rows}
            steps = [titles.get(k, k) for k in p.get("keys", [])]
            post("progress", "Plan:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps)),
                 {"steps": steps}, f"plan:{e['id']}")
        elif t == "task.accepted":
            task = self._get_task(e["task_id"])
            if task and task["kind"] not in ("plan", "integrate"):
                post("progress", f"Done: {task['title']}", {"task_id": task["id"]}, f"acc:{e['id']}")
        elif t == "action.succeeded":
            ar = self.db.one("SELECT action, args_json FROM action_requests WHERE id = ?", (p.get("action_request_id"),))
            target = ""
            if ar:
                a = loads(ar["args_json"], {})
                target = a.get("url") or ", ".join(a.get("to", []) if isinstance(a.get("to"), list) else []) or ""
            post("progress", f"Action completed: {p.get('action') or (ar['action'] if ar else 'action')}"
                             + (f" → {target}" if target else ""), {"action_request_id": p.get("action_request_id")},
                 f"act:{e['id']}")
        elif t.startswith("attention.") or (t == "task.transition" and p.get("to") in ("blocked", "failed")):
            self._attention(cid, mid)
        elif t == "mission.status":
            to = p.get("to")
            if to == "completed":
                self._result(cid, mid, e)
            elif to == "failed":
                post("error", f"The mission failed: {p.get('reason', '')}"[:600], {}, f"st:{e['id']}")
            elif to == "canceled":
                post("progress", "Mission canceled.", {}, f"st:{e['id']}")

    def _attention(self, cid: str, mid: str) -> None:
        from ..api.views import attention

        for a in attention(self.db, mid):
            key = a.get("action_request_id") or a.get("submission_id") or a.get("task_id") or a.get("proposal_id") or ""
            dedup = f"attn:{a['kind']}:{key}:{(a.get('detail') or '')[:80]}"
            text = {"action_approval": "Approve this action?", "owner_approval": "Needs your sign-off",
                    "task_blocked": "Stuck — needs you", "task_failed": "A step failed",
                    "task_awaiting_action": "Waiting on an action"}.get(a["kind"], a["kind"].replace("_", " "))
            self._post(cid, "attention", f"{text}: {a.get('title') or ''}".strip(": "), a, mid, dedup)

    def _result(self, cid: str, mid: str, e: dict[str, Any]) -> None:
        from ..api.views import mission_view

        v = mission_view(self.db, mid)
        content, art = "", v.get("final_artifact_id")
        if art:
            try:
                content = self.ctx.artifacts.read_text(art)[:RESULT_CHARS]
            except Exception:
                content = ""
        spent = self.db.one("SELECT COALESCE(SUM(actual_usd), 0) AS s FROM model_calls WHERE mission_id = ?", (mid,))["s"]
        actions = self.db.query("SELECT action, status FROM action_requests WHERE mission_id = ?", (mid,))
        self._post(cid, "result", content or "Mission completed.",
                   {"artifact_id": art, "spent_usd": round(float(spent or 0), 4),
                    "steps": len(v["tasks"]), "actions": [dict(a) for a in actions]}, mid, f"result:{mid}:{e['id']}")

    def _post(self, cid, kind, text, data, mid, dedup) -> None:
        with self.db.tx() as cur:
            self.chat.post(cur, cid, "nexus", kind, text, data or {}, mission_id=mid, dedup=dedup)

    async def run(self) -> None:
        while True:
            try:
                while self.process():
                    pass
            except Exception:
                log.exception("narrator loop failed")
            await self.ctx.notifier.wait(1.0)

    def start(self) -> None:
        self._runner = asyncio.create_task(self.run(), name="chat-narrator")

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass
