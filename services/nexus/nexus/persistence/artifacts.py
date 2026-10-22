"""Immutable, content-addressed artifact files with metadata and lineage in SQLite."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

from .db import Database, dumps, loads, new_id, now_iso


class ArtifactIntegrityError(Exception):
    pass


class ArtifactStore:
    def __init__(self, db: Database, root: Path):
        self.db = db
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put_bytes(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        p = self._path_for(sha)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".tmp{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, p)
            try:
                os.chmod(p, 0o444)
            except OSError:
                pass
        return sha

    def create(
        self,
        cur: sqlite3.Cursor,
        *,
        project_id: str,
        kind: str,
        title: str,
        content: bytes | str,
        media_type: str = "text/markdown",
        created_by: str,
        mission_id: str | None = None,
        task_id: str | None = None,
        mission_revision: int | None = None,
        input_versions: dict[str, Any] | None = None,
        lineage: dict[str, Any] | None = None,
        supersedes_id: str | None = None,
        access: str = "project",
    ) -> dict[str, Any]:
        data = content.encode("utf-8") if isinstance(content, str) else content
        sha = self.put_bytes(data)  # file write precedes metadata; orphans are harmless + GC-able
        aid = new_id("art")
        cur.execute(
            "INSERT INTO artifacts (id, project_id, mission_id, task_id, kind, title, media_type, sha256, size_bytes,"
            " created_by, mission_revision, input_versions_json, lineage_json, supersedes_id, access, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                aid, project_id, mission_id, task_id, kind, title, media_type, sha, len(data), created_by,
                mission_revision, dumps(input_versions or {}), dumps(lineage or {}), supersedes_id, access, now_iso(),
            ),
        )
        return {"id": aid, "sha256": sha, "size_bytes": len(data)}

    def read(self, artifact_id: str, *, verify: bool = True) -> bytes:
        row = self.db.one("SELECT sha256 FROM artifacts WHERE id = ?", (artifact_id,))
        if row is None:
            raise KeyError(artifact_id)
        return self.read_sha(row["sha256"], verify=verify)

    def read_sha(self, sha: str, *, verify: bool = True) -> bytes:
        data = self._path_for(sha).read_bytes()
        if verify and hashlib.sha256(data).hexdigest() != sha:
            raise ArtifactIntegrityError(f"artifact content hash mismatch for {sha}")
        return data

    def read_text(self, artifact_id: str) -> str:
        return self.read(artifact_id).decode("utf-8", errors="replace")

    def meta(self, artifact_id: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if r is None:
            return None
        d = dict(r)
        d["input_versions"] = loads(d.pop("input_versions_json"), {})
        d["lineage"] = loads(d.pop("lineage_json"), {})
        return d

    def verify_all(self) -> dict[str, Any]:
        bad, missing, ok = [], [], 0
        for r in self.db.query("SELECT id, sha256 FROM artifacts"):
            p = self._path_for(r["sha256"])
            if not p.exists():
                missing.append(r["id"])
                continue
            if hashlib.sha256(p.read_bytes()).hexdigest() != r["sha256"]:
                bad.append(r["id"])
            else:
                ok += 1
        return {"ok": ok, "corrupt": bad, "missing": missing}
