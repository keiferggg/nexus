"""Scoped memory with versioned write proposals and full-text retrieval.

Access filtering happens in SQL *before* ranking, so out-of-scope items never enter the
candidate set:  project scope (or owner-wide items with access='owner'), and private items only
for their owning agent. The FTS index is a rebuildable projection of memory_items.
"""

from __future__ import annotations

import re
from typing import Any

from ..persistence.db import ConflictError, Database, dumps, loads, new_id, now_iso
from ..persistence.events import emit

LAYERS = {"working", "episodic", "semantic", "procedural", "mission", "identity"}
ACCESS = {"private", "team", "project", "owner"}


class MemoryRejected(Exception):
    pass


def _fts_query(q: str) -> str:
    terms = [t for t in re.findall(r"[A-Za-z0-9_]{2,}", q.lower())][:12]
    return " OR ".join(f'"{t}"' for t in terms) if terms else '""'


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db

    # ---- write path: proposals validated before commit --------------------------------------
    def propose(self, *, proposer: str, project_id: str | None, layer: str, access: str, content: str,
                source_refs: list[str], owner_agent_id: str | None = None, mission_id: str | None = None,
                circumstances: str | None = None, target_id: str | None = None, base_version: int | None = None,
                speculative: bool = False) -> dict[str, Any]:
        """Validate and commit a memory write. Returns {"status": committed|rejected, ...}."""
        problems = []
        if layer not in LAYERS:
            problems.append(f"unknown layer {layer}")
        if access not in ACCESS:
            problems.append(f"unknown access {access}")
        if access == "private" and not owner_agent_id:
            problems.append("private memory requires an owning agent")
        if access == "owner" and project_id is not None:
            problems.append("owner-wide memory must not be project-scoped")
        if layer in ("semantic", "procedural") and not source_refs:
            problems.append("semantic/procedural memory requires source references")
        if speculative and layer in ("semantic", "procedural"):
            problems.append("speculation is never promoted to semantic/procedural memory automatically")
        if not content.strip():
            problems.append("empty content")
        with self.db.tx() as cur:
            for ref in source_refs:
                kind, _, rid = ref.partition(":")
                table = {"evidence": "evidence", "artifact": "artifacts", "acceptance": "acceptances",
                         "claim": "claims"}.get(kind)
                if table is None or cur.execute(f"SELECT 1 FROM {table} WHERE id = ?", (rid,)).fetchone() is None:
                    problems.append(f"unresolvable source reference {ref}")
            if target_id:
                cur_item = cur.execute("SELECT * FROM memory_items WHERE id = ?", (target_id,)).fetchone()
                if cur_item is None:
                    problems.append("target memory item not found")
                elif base_version is not None and cur_item["version"] != base_version:
                    problems.append(f"version conflict: base {base_version}, current {cur_item['version']}")
                elif cur_item["project_id"] != project_id:
                    problems.append("cannot move memory across scopes")
            pid = new_id("mpt")
            status = "rejected" if problems else "committed"
            cur.execute("INSERT INTO memory_patches (id, target_id, base_version, project_id, proposal_json, status,"
                        " validation_json, proposer, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (pid, target_id, base_version, project_id,
                         dumps({"layer": layer, "access": access, "content": content, "source_refs": source_refs}),
                         status, dumps({"problems": problems}), proposer, now_iso()))
            if problems:
                emit(cur, "memory.rejected", proposer, {"patch_id": pid, "problems": problems}, project_id=project_id,
                     mission_id=mission_id)
                return {"status": "rejected", "patch_id": pid, "problems": problems}
            ts = now_iso()
            if target_id:
                cur.execute("UPDATE memory_items SET content = ?, source_refs_json = ?, version = version + 1,"
                            " updated_at = ? WHERE id = ?", (content, dumps(source_refs), ts, target_id))
                cur.execute("DELETE FROM memory_fts WHERE item_id = ?", (target_id,))
                mid = target_id
            else:
                mid = new_id("mem")
                cur.execute("INSERT INTO memory_items (id, project_id, layer, access, owner_agent_id, mission_id, content,"
                            " source_refs_json, circumstances, status, created_by, created_at, updated_at)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (mid, project_id, layer, access, owner_agent_id, mission_id, content, dumps(source_refs),
                             circumstances, "active", proposer, ts, ts))
            cur.execute("INSERT INTO memory_fts (content, item_id) VALUES (?, ?)", (content, mid))
            emit(cur, "memory.committed", proposer, {"patch_id": pid, "item_id": mid, "layer": layer, "access": access},
                 project_id=project_id, mission_id=mission_id)
        return {"status": "committed", "patch_id": pid, "item_id": mid}

    # ---- read path ---------------------------------------------------------------------------
    def search(self, query: str, *, project_id: str, agent_id: str | None, limit: int = 8,
               layers: set[str] | None = None) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT m.*, bm25(memory_fts) AS rank FROM memory_fts JOIN memory_items m ON m.id = memory_fts.item_id"
            " WHERE memory_fts MATCH ? AND m.status = 'active'"
            "   AND ((m.project_id = ? AND (m.access != 'private' OR m.owner_agent_id = ?))"
            "        OR (m.project_id IS NULL AND m.access = 'owner'))"
            " ORDER BY rank LIMIT ?",
            (_fts_query(query), project_id, agent_id or "", limit * 3),
        )
        out = []
        for r in rows:
            if layers and r["layer"] not in layers:
                continue
            out.append({"id": r["id"], "layer": r["layer"], "access": r["access"], "content": r["content"],
                        "source_refs": loads(r["source_refs_json"], []), "circumstances": r["circumstances"],
                        "version": r["version"]})
            if len(out) >= limit:
                break
        return out

    def rebuild_index(self) -> int:
        with self.db.tx() as cur:
            cur.execute("DELETE FROM memory_fts")
            rows = cur.execute("SELECT id, content FROM memory_items").fetchall()
            for r in rows:
                cur.execute("INSERT INTO memory_fts (content, item_id) VALUES (?, ?)", (r["content"], r["id"]))
        return len(rows)
