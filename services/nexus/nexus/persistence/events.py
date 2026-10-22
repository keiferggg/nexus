"""Durable event log + transactional outbox.

``emit`` must be called with the cursor of the transaction that performs the state change, so
the event row and the change commit (or roll back) together. Streaming consumers read by
``seq`` cursor, which makes the stream resumable after disconnects or service restarts.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from .db import Database, dumps, loads, new_id, now_iso


def emit(
    cur: sqlite3.Cursor,
    type: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    *,
    project_id: str | None = None,
    mission_id: str | None = None,
    task_id: str | None = None,
    causal_parent: str | None = None,
) -> str:
    eid = new_id("ev")
    cur.execute(
        "INSERT INTO events (id, project_id, mission_id, task_id, type, actor, payload_json, causal_parent, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (eid, project_id, mission_id, task_id, type, actor, dumps(payload or {}), causal_parent, now_iso()),
    )
    return eid


def row_to_event(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": r["seq"],
        "id": r["id"],
        "project_id": r["project_id"],
        "mission_id": r["mission_id"],
        "task_id": r["task_id"],
        "type": r["type"],
        "actor": r["actor"],
        "payload": loads(r["payload_json"], {}),
        "causal_parent": r["causal_parent"],
        "created_at": r["created_at"],
    }


def read_events(
    db: Database, *, after: int = 0, mission_id: str | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    if mission_id:
        rows = db.query(
            "SELECT * FROM events WHERE seq > ? AND mission_id = ? ORDER BY seq LIMIT ?", (after, mission_id, limit)
        )
    else:
        rows = db.query("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit))
    return [row_to_event(r) for r in rows]


class EventNotifier:
    """Wakes streaming consumers after any committed write. Consumers then read from the DB by
    cursor, so a missed wake-up never loses events (the DB is the source of truth)."""

    def __init__(self, db: Database):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: set[asyncio.Event] = set()
        db.add_commit_hook(self._on_commit)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _on_commit(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for w in list(self._waiters):
            try:
                loop.call_soon_threadsafe(w.set)
            except RuntimeError:
                pass

    async def wait(self, timeout: float) -> None:
        ev = asyncio.Event()
        self._waiters.add(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._waiters.discard(ev)
