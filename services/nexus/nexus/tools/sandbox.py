"""Host-controlled sandbox manager. Docker is an optional capability.

Workers never receive the Docker socket or host mounts: the *host* launches a container with
only the task worktree mounted read-only, no network, bounded CPU/memory/pids and a timeout.
If prerequisites are missing the capability is reported as unavailable and code is not run.

Each project may choose a sandbox *profile* (project settings "sandbox"): a different image (e.g. one with the
project's dependencies preinstalled) and larger limits. The isolation flags (no network, read-only worktree,
no new privileges, pid limit) are not configurable.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

DEFAULT_PROFILE: dict[str, Any] = {"image": "python:3.12-slim", "memory_mb": 512, "cpus": 1.0, "tmpfs_mb": 64,
                                   "timeout_s": 180}
LIMITS = {"memory_mb": (128, 8192), "cpus": (0.25, 8.0), "tmpfs_mb": (16, 4096), "timeout_s": (10, 1800)}
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$")


def clean_profile(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validated profile: unknown keys dropped, numbers clamped, image names checked (never a docker flag)."""
    p = dict(DEFAULT_PROFILE)
    raw = raw or {}
    img = str(raw.get("image") or "").strip()
    if img:
        if not IMAGE_RE.match(img):
            raise ValueError(f"invalid image name {img!r}")
        p["image"] = img
    for k, (lo, hi) in LIMITS.items():
        if raw.get(k) not in (None, ""):
            p[k] = min(max(float(raw[k]), lo), hi)
            if k != "cpus":
                p[k] = int(p[k])
    return p


def project_profile(db, project_id: str | None) -> dict[str, Any]:
    from ..persistence.db import loads

    row = db.one("SELECT settings_json FROM projects WHERE id = ?", (project_id,)) if project_id else None
    try:
        return clean_profile((loads(row["settings_json"], {}) if row else {}).get("sandbox"))
    except ValueError:
        return dict(DEFAULT_PROFILE)


def repo_sandbox_spec(root: Path) -> dict[str, Any] | None:
    """A repository can describe its own test sandbox in .nexus/sandbox.json:
    {"image": "name:tag", "dockerfile": "path/in/repo", "context": "path/in/repo", "memory_mb": …, "cpus": …,
     "tmpfs_mb": …, "timeout_s": …}. Paths must stay inside the repository."""
    import json

    f = Path(root) / ".nexus" / "sandbox.json"
    if not f.is_file():
        return None
    try:
        spec = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(spec, dict):
        return None
    prof = clean_profile(spec)  # validates the image name
    root = Path(root).resolve()
    out: dict[str, Any] = {"profile": prof}
    for k in ("dockerfile", "context"):
        if spec.get(k):
            p = (root / str(spec[k])).resolve()
            if root not in p.parents and p != root:
                raise ValueError(f"{k} escapes the repository")
            out[k] = p
    return out


class DockerSandbox:
    async def build(self, dockerfile: Path, context: Path, tag: str, *, timeout_s: float = 1800) -> tuple[bool, str]:
        """Host-side image build from a repository's own Dockerfile (has network; runs in Docker's build sandbox)."""
        if not self.docker:
            return False, "docker CLI not found on PATH"
        if not IMAGE_RE.match(tag):
            return False, f"invalid image name {tag!r}"
        rc, out, err = await self._run("build", "-q", "-f", str(dockerfile), "-t", tag, str(context), timeout=timeout_s)
        self._cached.pop(tag, None)
        return rc == 0, (out.strip() or err.strip())[-500:]

    def __init__(self, image: str = DEFAULT_PROFILE["image"], docker_bin: str | None = None):
        self.image = image
        self.docker = docker_bin or shutil.which("docker")
        self._cached: dict[str, tuple[bool, str]] = {}

    async def _run(self, *args: str, timeout: float) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(self.docker, *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", "timeout"
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def available(self, refresh: bool = False, image: str | None = None) -> tuple[bool, str]:
        image = image or self.image
        if image in self._cached and not refresh:
            return self._cached[image]
        if not self.docker:
            return self._cached.setdefault(image, (False, "docker CLI not found on PATH"))
        rc, out, err = await self._run("info", "--format", "{{.ServerVersion}}", timeout=15)
        if rc != 0:  # not cached: Docker Desktop may simply not be started yet
            return False, "docker daemon not reachable (is Docker Desktop running?)"
        rc, _, _ = await self._run("image", "inspect", image, timeout=15)
        if rc != 0:
            return False, f"sandbox image '{image}' not present locally; pull or build it explicitly"
        self._cached[image] = (True, f"docker {out.strip()} image {image}")
        return self._cached[image]

    async def run(self, worktree: Path, command: list[str], *, timeout_s: float | None = None,
                  profile: dict[str, Any] | None = None) -> dict[str, Any]:
        p = clean_profile(profile) if profile else {**DEFAULT_PROFILE, "image": self.image}
        timeout = float(timeout_s if timeout_s is not None else p["timeout_s"])
        ok, why = await self.available(image=p["image"])
        if not ok:
            raise RuntimeError(f"sandbox unavailable: {why}")
        if not all(isinstance(c, str) for c in command) or not command:
            raise ValueError("command must be a non-empty list of strings")
        name = f"nexus-sbx-{uuid.uuid4().hex[:12]}"
        args = ["run", "--rm", "--name", name, "--network", "none", "--memory", f"{p['memory_mb']}m",
                "--cpus", str(p["cpus"]), "--pids-limit", "512", "--read-only",
                "--tmpfs", f"/tmp:rw,exec,size={p['tmpfs_mb']}m", "--security-opt", "no-new-privileges",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-v", f"{Path(worktree).resolve()}:/work:ro", "-w", "/work",
                p["image"], *command]
        rc, out, err = await self._run(*args, timeout=timeout)
        if rc == 124 and err == "timeout":
            await self._run("kill", name, timeout=15)  # killing the CLI does not stop the container
        return {"exit_code": rc, "stdout": out[-20000:], "stderr": err[-20000:], "timed_out": rc == 124,
                "passed": rc == 0, "sandbox": f"docker:{p['image']}"}


class UnavailableSandbox:
    def __init__(self, reason: str):
        self.reason = reason

    async def available(self, refresh: bool = False, image: str | None = None) -> tuple[bool, str]:
        return False, self.reason

    async def run(self, worktree: Path, command: list[str], *, timeout_s: float | None = None,
                  profile: dict[str, Any] | None = None) -> dict[str, Any]:
        raise RuntimeError(f"sandbox unavailable: {self.reason}")
