"""Backup and restore of application data.

A backup contains the SQLite database (online backup API), immutable artifacts and framework
checkpoints, plus a manifest with every artifact hash. Credentials are never included: the
database only holds opaque secret references, and the OS credential store is not exported.
After a restore on another machine, provider keys must be re-entered.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .persistence.db import now_iso

FORMAT = "nexus-backup-v1"


def create_backup(ctx, dest: Path) -> dict[str, Any]:
    dest = Path(dest)
    if dest.suffix != ".zip":
        dest = dest.with_suffix(".zip")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "nexus.db"
        ctx.db.backup_to(snap)
        hashes = {r["sha256"]: r["id"] for r in ctx.db.query("SELECT id, sha256 FROM artifacts")}
        manifest = {"format": FORMAT, "created_at": now_iso(),
                    "schema": [r["version"] for r in ctx.db.query("SELECT version FROM schema_migrations")],
                    "artifact_count": len(hashes), "artifact_hashes": sorted(hashes),
                    "credentials": "not included (OS credential store is never exported; re-enter keys after restore)",
                    "excluded": ["worktrees (machine-specific git worktrees)", "service.lock"]}
        tmp = dest.with_suffix(".zip.partial")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "nexus.db")
            for sub in ("artifacts", "checkpoints", "demo-workspaces"):
                root = ctx.data_dir / sub
                if not root.exists():
                    continue
                for p in root.rglob("*"):
                    if p.is_file() and ".git" not in p.relative_to(root).parts[:1]:
                        z.write(p, f"{sub}/{p.relative_to(root).as_posix()}")
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        os.replace(tmp, dest)
    return {"path": str(dest), "artifacts": len(hashes), "bytes": dest.stat().st_size,
            "credentials_included": False}


def verify_backup(path: Path) -> dict[str, Any]:
    problems = []
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            problems.append(f"unknown format {manifest.get('format')}")
        names = set(z.namelist())
        for sha in manifest["artifact_hashes"]:
            name = f"artifacts/{sha[:2]}/{sha}"
            if name not in names:
                problems.append(f"missing artifact {sha[:12]}")
                continue
            if hashlib.sha256(z.read(name)).hexdigest() != sha:
                problems.append(f"corrupt artifact {sha[:12]}")
        with tempfile.TemporaryDirectory() as td:
            z.extract("nexus.db", td)
            conn = sqlite3.connect(Path(td) / "nexus.db")
            try:
                ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
            if ok != "ok":
                problems.append(f"database integrity: {ok}")
    return {"ok": not problems, "problems": problems, "manifest": {k: v for k, v in manifest.items()
                                                                     if k != "artifact_hashes"}}


def restore_backup(path: Path, data_dir: Path) -> dict[str, Any]:
    """Restore into an EMPTY (or absent) data directory while the service is stopped."""
    data_dir = Path(data_dir)
    check = verify_backup(path)
    if not check["ok"]:
        raise ValueError(f"backup failed verification: {check['problems'][:5]}")
    if data_dir.exists() and any(p.name != "service.lock" for p in data_dir.iterdir()):
        raise ValueError(f"target data directory {data_dir} is not empty; restore into a fresh directory")
    data_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name == "manifest.json":
                continue
            target = (data_dir / name).resolve()
            if not target.is_relative_to(data_dir.resolve()):
                raise ValueError(f"unsafe path in backup: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    conn = sqlite3.connect(data_dir / "nexus.db")
    try:
        conn.execute("UPDATE providers SET status = 'unvalidated', status_detail = 'restored from backup: re-enter the"
                     " API key' WHERE secret_ref IS NOT NULL")
        conn.commit()
    finally:
        conn.close()
    return {"restored_to": str(data_dir), **check}
