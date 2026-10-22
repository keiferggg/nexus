"""Durable structured messages for mission rooms.

At-least-once delivery with deduplication by operation_id; explicit recipients (no global
broadcast into agent contexts); pending deliveries survive restart; expired requests surface
to the supervisor as owner-attention events.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..persistence.db import dumps, loads, new_id, now_iso
from ..persistence.events import emit

MESSAGE_TYPES = {"question", "proposal", "evidence", "objection", "task_offer", "task_claim", "handoff",
                 "review_request", "decision", "blocker", "status"}


def post(cur: sqlite3.Cursor, *, operation_id: str, project_id: str, mission_id: str | None, thread_id: str,
         sender: str, recipients: list[str], type: str, body: str, task_id: str | None = None,
         causal_parent: str | None = None, refs: dict[str, Any] | None = None, expects_response: str | None = None,
         deadline: str | None = None) -> tuple[str, bool]:
    """Returns (message_id, created). A redelivered post with the same operation_id is a no-op."""
    if type not in MESSAGE_TYPES:
        raise ValueError(f"unknown message type {type}")
    existing = cur.execute("SELECT id FROM messages WHERE operation_id = ?", (operation_id,)).fetchone()
    if existing:
        return existing["id"], False
    mid = new_id("msg")
    cur.execute(
        "INSERT INTO messages (id, operation_id, project_id, mission_id, task_id, thread_id, sender, recipients_json,"
        " causal_parent, type, body, refs_json, expects_response, deadline, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, operation_id, project_id, mission_id, task_id, thread_id, sender, dumps(recipients), causal_parent, type,
         body, dumps(refs or {}), expects_response, deadline, now_iso()),
    )
    for r in recipients:
        cur.execute("INSERT OR IGNORE INTO message_deliveries (message_id, recipient, status) VALUES (?,?,?)",
                    (mid, r, "pending"))
    emit(cur, "message.posted", sender, {"message_id": mid, "type": type, "thread_id": thread_id, "body": body[:400],
                                         "recipients": recipients, "refs": refs or {}},
         project_id=project_id, mission_id=mission_id, task_id=task_id, causal_parent=causal_parent)
    return mid, True


def pending_for(cur: sqlite3.Cursor, *, recipient_keys: list[str], mission_id: str) -> list[dict[str, Any]]:
    if not recipient_keys:
        return []
    rows = cur.execute(
        f"SELECT m.*, d.recipient, d.status AS dstatus FROM message_deliveries d JOIN messages m ON m.id = d.message_id"
        f" WHERE d.recipient IN ({','.join('?' * len(recipient_keys))}) AND d.status IN ('pending','delivered')"
        f" AND m.mission_id = ? ORDER BY m.created_at", (*recipient_keys, mission_id)).fetchall()
    return [{"id": r["id"], "type": r["type"], "from": r["sender"], "body": r["body"], "refs": loads(r["refs_json"], {}),
             "expects_response": r["expects_response"], "recipient": r["recipient"]} for r in rows]


def mark_delivered(cur: sqlite3.Cursor, message_ids: list[str], recipient_keys: list[str]) -> None:
    for mid in message_ids:
        for rk in recipient_keys:
            cur.execute("UPDATE message_deliveries SET status = 'delivered', attempts = attempts + 1, delivered_at = ?"
                        " WHERE message_id = ? AND recipient = ? AND status = 'pending'", (now_iso(), mid, rk))
            # Messages that need no response are complete once delivered.
            cur.execute("UPDATE message_deliveries SET status = 'acked', acked_at = ? WHERE message_id = ? AND recipient = ?"
                        " AND status = 'delivered' AND (SELECT expects_response FROM messages WHERE id = ?) IS NULL",
                        (now_iso(), mid, rk, mid))


def acknowledge(cur: sqlite3.Cursor, message_id: str, recipient: str) -> bool:
    cur.execute("UPDATE message_deliveries SET status = 'acked', acked_at = ? WHERE message_id = ? AND recipient = ?"
                " AND status IN ('pending','delivered')", (now_iso(), message_id, recipient))
    return cur.rowcount == 1


def expire_overdue(cur: sqlite3.Cursor) -> list[str]:
    rows = cur.execute(
        "SELECT d.message_id, d.recipient, m.mission_id, m.project_id, m.body FROM message_deliveries d"
        " JOIN messages m ON m.id = d.message_id WHERE d.status IN ('pending','delivered') AND m.deadline IS NOT NULL"
        " AND m.deadline < ?", (now_iso(),)).fetchall()
    out = []
    for r in rows:
        cur.execute("UPDATE message_deliveries SET status = 'expired' WHERE message_id = ? AND recipient = ?",
                    (r["message_id"], r["recipient"]))
        emit(cur, "attention.message_expired", "nexus:supervisor",
             {"message_id": r["message_id"], "recipient": r["recipient"], "body": r["body"][:200]},
             project_id=r["project_id"], mission_id=r["mission_id"])
        out.append(r["message_id"])
    return out


def thread_for_task(task_id: str) -> str:
    return f"task:{task_id}"


def thread_for_mission(mission_id: str) -> str:
    return f"mission:{mission_id}"
