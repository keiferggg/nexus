"""First-run setup helpers: the offline fake provider and demo projects (visibly flagged)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .appctx import AppContext
from .demo.script import demo_script
from .domain.missions import create_project
from .providers.fake import FakeProvider
from .tools.worktree import GIT_ENV


def fixtures_dir() -> Path:
    env = os.environ.get("NEXUS_FIXTURES_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return Path(getattr(sys, "_MEIPASS")) / "fixtures"
    return Path(__file__).resolve().parents[3] / "fixtures"


def ensure_fake_provider(ctx: AppContext, *, script=None, faults=None) -> str:
    row = ctx.db.one("SELECT id FROM providers WHERE kind = 'fake' ORDER BY created_at LIMIT 1")
    pid = row["id"] if row else ctx.registry.add(kind="fake", label="Deterministic fake (offline demo)")
    ctx.registry.register_override(pid, FakeProvider(script or demo_script, faults=list(faults or [])))
    if not row:
        with ctx.db.tx() as cur:
            cur.execute("UPDATE providers SET status = 'ok', status_detail = 'in-process deterministic script',"
                        " models_json = '[\"fake-deterministic-1\"]' WHERE id = ?", (pid,))
    return pid


def create_demo_project(ctx: AppContext, kind: str = "research", *, name: str | None = None) -> str:
    src = fixtures_dir() / f"demo-{kind}"
    if not src.is_dir():
        raise FileNotFoundError(f"demo fixtures not found at {src}")
    dest_root = ctx.data_dir / "demo-workspaces"
    dest_root.mkdir(parents=True, exist_ok=True)
    n = 1
    while (dest_root / f"{kind}-{n}").exists():
        n += 1
    dest = dest_root / f"{kind}-{n}"
    shutil.copytree(src, dest)
    if kind == "code":
        env = {**os.environ, **GIT_ENV}
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m",
                                                                    "demo fixture baseline"]):
            subprocess.run(["git", *args], cwd=dest, check=True, capture_output=True, env=env)
    with ctx.db.tx() as cur:
        pid = create_project(cur, name=name or f"DEMO {kind} {n}", workspace_root=str(dest), is_demo=True)
    return pid
