"""Mission spend caps with atomic reservations.

Committed spend = reconciled actuals + outstanding reservations + unresolved reservations.
A reservation is admitted only if committed + estimate <= cap, checked and inserted inside one
``BEGIN IMMEDIATE`` transaction, so concurrent workers cannot jointly exceed the cap.
These are estimates from owner-configured prices, not invoice-level figures.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..persistence.db import new_id, now_iso
from ..persistence.events import emit


class BudgetExceeded(Exception):
    pass


def committed_usd(cur: sqlite3.Cursor, mission_id: str) -> float:
    r = cur.execute(
        "SELECT COALESCE(SUM(CASE WHEN status IN ('reserved','unresolved') THEN reserved_usd"
        " WHEN status = 'reconciled' THEN actual_usd ELSE 0 END), 0) AS c FROM budget_reservations WHERE mission_id = ?",
        (mission_id,),
    ).fetchone()
    return float(r["c"])


def reserve(cur: sqlite3.Cursor, mission_id: str, *, category: str, amount_usd: float, basis: str,
            ref: str | None = None) -> str:
    m = cur.execute("SELECT project_id, spend_cap_usd, execution_mode FROM missions WHERE id = ?", (mission_id,)).fetchone()
    cap = m["spend_cap_usd"]
    used = committed_usd(cur, mission_id)
    if cap is None:
        if m["execution_mode"] == "live":
            raise BudgetExceeded("live mission has no spend cap")
        cap = float("inf")
    if used + amount_usd > cap + 1e-12:
        emit(cur, "budget.denied", "nexus:budget",
             {"category": category, "requested_usd": round(amount_usd, 6), "committed_usd": round(used, 6), "cap_usd": cap},
             project_id=m["project_id"], mission_id=mission_id)
        raise BudgetExceeded(f"reservation ${amount_usd:.4f} would exceed cap ${cap:.4f} (committed ${used:.4f})")
    rid = new_id("bud")
    ts = now_iso()
    cur.execute(
        "INSERT INTO budget_reservations (id, mission_id, category, reserved_usd, status, basis, ref, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, mission_id, category, amount_usd, "reserved", basis, ref, ts, ts),
    )
    return rid


def reconcile(cur: sqlite3.Cursor, reservation_id: str, *, actual_usd: float | None, status: str) -> None:
    """status: reconciled (actual known) | released (provably not billed) | unresolved (unknown)."""
    cur.execute("UPDATE budget_reservations SET status = ?, actual_usd = ?, updated_at = ? WHERE id = ?",
                (status, actual_usd, now_iso(), reservation_id))


def summary(cur: sqlite3.Cursor, mission_id: str) -> dict[str, Any]:
    m = cur.execute("SELECT spend_cap_usd FROM missions WHERE id = ?", (mission_id,)).fetchone()
    rows = cur.execute(
        "SELECT category, status, COALESCE(SUM(reserved_usd),0) AS reserved, COALESCE(SUM(actual_usd),0) AS actual,"
        " COUNT(*) AS n FROM budget_reservations WHERE mission_id = ? GROUP BY category, status", (mission_id,)).fetchall()
    out: dict[str, Any] = {"cap_usd": m["spend_cap_usd"], "actual_usd": 0.0, "reserved_usd": 0.0, "unresolved_usd": 0.0,
                           "by_category": {}}
    for r in rows:
        cat = out["by_category"].setdefault(r["category"], {"actual_usd": 0.0, "reserved_usd": 0.0, "unresolved_usd": 0.0})
        if r["status"] == "reconciled":
            out["actual_usd"] += r["actual"]
            cat["actual_usd"] += r["actual"]
        elif r["status"] == "reserved":
            out["reserved_usd"] += r["reserved"]
            cat["reserved_usd"] += r["reserved"]
        elif r["status"] == "unresolved":
            out["unresolved_usd"] += r["reserved"]
            cat["unresolved_usd"] += r["reserved"]
    out["committed_usd"] = committed_usd(cur, mission_id)
    out["remaining_usd"] = None if m["spend_cap_usd"] is None else max(0.0, m["spend_cap_usd"] - out["committed_usd"])
    out["note"] = "Estimates from owner-configured prices and provider-reported usage; not invoice figures."
    return out
