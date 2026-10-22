"""Isolated git worktrees for concurrent coding workers and an explicit integration step."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "NEXUS", "GIT_AUTHOR_EMAIL": "nexus@localhost",
           "GIT_COMMITTER_NAME": "NEXUS", "GIT_COMMITTER_EMAIL": "nexus@localhost"}


class GitError(Exception):
    pass


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV}
    p = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args], cwd=str(cwd),
                       capture_output=True, text=True, env=env, timeout=120)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {p.stderr.strip()[:500]}")
    return p


async def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(_git, cwd, *args, check=check)


class WorktreeManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def is_repo(self, repo: Path) -> bool:
        p = await git(repo, "rev-parse", "--is-inside-work-tree", check=False)
        return p.returncode == 0 and p.stdout.strip() == "true"

    async def head(self, repo: Path) -> str:
        return (await git(repo, "rev-parse", "HEAD")).stdout.strip()

    async def create(self, repo: Path, name: str, base_ref: str) -> dict[str, Any]:
        path = self.root / name
        branch = f"nexus/{name}"
        if path.exists():
            # resume an existing worktree (e.g. after restart)
            return {"path": path, "branch": branch, "base": base_ref, "resumed": True}
        await git(repo, "worktree", "add", "-B", branch, str(path), base_ref)
        return {"path": path, "branch": branch, "base": base_ref, "resumed": False}

    async def commit_all(self, wt: Path, message: str) -> str | None:
        await git(wt, "add", "-A")
        st = await git(wt, "status", "--porcelain")
        if not st.stdout.strip():
            return None
        await git(wt, "commit", "-m", message)
        return (await git(wt, "rev-parse", "HEAD")).stdout.strip()

    async def diff(self, wt: Path, base: str) -> str:
        return (await git(wt, "diff", f"{base}..HEAD")).stdout

    async def changed_files(self, wt: Path, base: str) -> list[str]:
        out = (await git(wt, "diff", "--name-only", f"{base}..HEAD")).stdout
        return [l for l in out.splitlines() if l.strip()]

    async def integrate(self, repo: Path, name: str, base: str, branches: list[str]) -> dict[str, Any]:
        """Merge branches into a fresh integration worktree. A conflict is captured explicitly
        (files + both sides) and the merge is aborted - nothing is resolved silently."""
        info = await self.create(repo, name, base)
        wt = info["path"]
        merged, conflicts = [], []
        for b in branches:
            p = await git(wt, "merge", "--no-ff", "--no-edit", b, check=False)
            if p.returncode != 0:
                files = (await git(wt, "diff", "--name-only", "--diff-filter=U", check=False)).stdout.split()
                sides = {}
                for f in files[:10]:
                    ours = await git(wt, "show", f":2:{f}", check=False)
                    theirs = await git(wt, "show", f":3:{f}", check=False)
                    sides[f] = {"ours": ours.stdout[:4000], "theirs": theirs.stdout[:4000]}
                await git(wt, "merge", "--abort", check=False)
                conflicts.append({"branch": b, "files": files, "sides": sides})
            else:
                merged.append(b)
        return {"path": wt, "branch": info["branch"], "merged": merged, "conflicts": conflicts,
                "head": (await git(wt, "rev-parse", "HEAD")).stdout.strip()}

    async def remove(self, repo: Path, name: str) -> None:
        path = self.root / name
        await git(repo, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def python_syntax_check(root: Path, paths: list[str]) -> dict[str, Any]:
    """Parse-only validation (compile() does not execute module code)."""
    errors = {}
    for rel in paths:
        p = (root / rel)
        if not p.exists():
            errors[rel] = "missing"
            continue
        try:
            compile(p.read_text(encoding="utf-8"), rel, "exec", dont_inherit=True)
        except SyntaxError as exc:
            errors[rel] = f"SyntaxError line {exc.lineno}: {exc.msg}"
    return {"checked": paths, "errors": errors, "passed": not errors}
