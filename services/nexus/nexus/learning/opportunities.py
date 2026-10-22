"""Bounded initiative: an opportunity queue derived from recorded state.

Every opportunity references an existing owner goal (a mission), carries a cost estimate,
a benefit rationale, a scope and an expiry, and is deduplicated. Nothing runs in the background
unless the owner enabled the opportunity policy AND set a capped background budget; otherwise
opportunities wait for explicit approval. No new owner objectives are invented.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..persistence.db import Database, dumps, loads, new_id, now_iso
from ..persistence.events import emit

TTL_DAYS = 7


def _avg_task_cost(db: Database, mission_id: str) -> float:
    r = db.one("SELECT COALESCE(AVG(c), 0) AS a FROM (SELECT SUM(COALESCE(actual_usd, 0)) AS c FROM model_calls WHERE"
               " mission_id = ? GROUP BY task_id)", (mission_id,))
    return float(r["a"] or 0.0)


def _upsert(db: Database, *, project_id: str, mission_id: str, kind: str, rationale: str, cost: float, scope: str,
            dedup: str) -> str | None:
    expires = (datetime.now(timezone.utc) + timedelta(days=TTL_DAYS)).isoformat(timespec="seconds")
    with db.tx() as cur:
        if cur.execute("SELECT 1 FROM opportunities WHERE dedup_key = ?", (dedup,)).fetchone():
            return None
        oid = new_id("opp")
        cur.execute("INSERT INTO opportunities (id, project_id, mission_id, kind, goal_ref, rationale, cost_estimate_usd,"
                    " scope, status, dedup_key, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, project_id, mission_id, kind, f"mission:{mission_id}", rationale, cost, scope, "open", dedup,
                     expires, now_iso()))
        emit(cur, "opportunity.found", "nexus:initiative", {"opportunity_id": oid, "kind": kind, "rationale": rationale,
                                                            "cost_estimate_usd": cost}, project_id=project_id,
             mission_id=mission_id)
    return oid


def scan(db: Database, project_id: str) -> list[str]:
    found: list[str] = []
    with db.tx() as cur:
        cur.execute("UPDATE opportunities SET status = 'expired' WHERE status = 'open' AND expires_at < ?", (now_iso(),))
    for m in db.query("SELECT * FROM missions WHERE project_id = ? AND status NOT IN ('canceled','canceling')",
                      (project_id,)):
        cost = _avg_task_cost(db, m["id"])
        # Newly unblocked work in a paused mission.
        ready = db.query("SELECT key FROM tasks WHERE mission_id = ? AND status = 'ready'", (m["id"],))
        if m["status"] == "paused" and ready:
            oid = _upsert(db, project_id=project_id, mission_id=m["id"], kind="unblocked_work",
                          rationale=f"{len(ready)} task(s) became runnable while the mission is paused: "
                                    f"{[r['key'] for r in ready][:5]}",
                          cost=cost * len(ready), scope="resume mission", dedup=f"unblocked:{m['id']}:{len(ready)}")
            found += [oid] if oid else []
        # Stale decision evidence.
        for d in db.query("SELECT * FROM decisions WHERE mission_id = ? AND status = 'stale'", (m["id"],)):
            oid = _upsert(db, project_id=project_id, mission_id=m["id"], kind="stale_evidence",
                          rationale=f"decision '{d['title']}' depends on evidence that changed",
                          cost=cost, scope=f"recheck decision {d['id']}", dedup=f"stale-decision:{d['id']}")
            found += [oid] if oid else []
        stale = db.query("SELECT id, key FROM tasks WHERE mission_id = ? AND is_stale = 1 AND status = 'draft'", (m["id"],))
        if stale and m["status"] == "completed":
            oid = _upsert(db, project_id=project_id, mission_id=m["id"], kind="stale_evidence",
                          rationale=f"{len(stale)} accepted result(s) became stale after the mission completed",
                          cost=cost * len(stale), scope="re-run stale tasks", dedup=f"stale-tasks:{m['id']}:{len(stale)}")
            found += [oid] if oid else []
    # Recurring failures across tasks: same failing criterion kind, >= 3 occurrences.
    for r in db.query("SELECT r.check_kind, COUNT(*) AS n, MIN(t.mission_id) AS mid FROM reviews r JOIN tasks t ON"
                      " t.id = r.task_id WHERE t.project_id = ? AND r.verdict = 'fail' GROUP BY r.check_kind HAVING n >= 3",
                      (project_id,)):
        oid = _upsert(db, project_id=project_id, mission_id=r["mid"], kind="recurring_failure",
                      rationale=f"check '{r['check_kind']}' failed {r['n']} times across tasks; a procedure candidate"
                                f" may prevent it", cost=0.0, scope="propose a skill candidate for evaluation",
                      dedup=f"recurring:{project_id}:{r['check_kind']}:{r['n'] // 3}")
        found += [oid] if oid else []
    return found


def background_policy(db: Database) -> dict[str, Any]:
    r = db.one("SELECT value_json FROM settings WHERE key = 'initiative.policy'")
    return loads(r["value_json"], {"enabled": False, "budget_usd": 0.0}) if r else {"enabled": False, "budget_usd": 0.0}


def may_run_in_background(db: Database, opp) -> tuple[bool, str]:
    pol = background_policy(db)
    if not pol.get("enabled"):
        return False, "background initiative disabled; awaiting owner approval"
    if opp["cost_estimate_usd"] > float(pol.get("budget_usd") or 0):
        return False, f"estimate ${opp['cost_estimate_usd']:.4f} exceeds background budget ${pol.get('budget_usd')}"
    return True, "within enabled policy and capped budget"


def consolidate_memory(db: Database, project_id: str) -> dict[str, Any]:
    """Merge duplicate episodic entries (same content) into one with combined provenance.
    Never promotes to semantic/procedural memory and never discards source references."""
    merged = 0
    with db.tx() as cur:
        rows = cur.execute("SELECT * FROM memory_items WHERE project_id = ? AND layer = 'episodic' AND status = 'active'"
                           " ORDER BY created_at", (project_id,)).fetchall()
        by_content: dict[str, list] = {}
        for r in rows:
            by_content.setdefault(r["content"], []).append(r)
        for content, group in by_content.items():
            if len(group) < 2:
                continue
            keep, rest = group[0], group[1:]
            refs = []
            for g in group:
                refs += loads(g["source_refs_json"], [])
            cur.execute("UPDATE memory_items SET source_refs_json = ?, version = version + 1, updated_at = ? WHERE id = ?",
                        (dumps(sorted(set(refs))), now_iso(), keep["id"]))
            for g in rest:
                cur.execute("UPDATE memory_items SET status = 'superseded', updated_at = ? WHERE id = ?", (now_iso(), g["id"]))
                cur.execute("DELETE FROM memory_fts WHERE item_id = ?", (g["id"],))
                merged += 1
        emit(cur, "memory.consolidated", "nexus:memory", {"merged": merged}, project_id=project_id)
    return {"merged": merged}
