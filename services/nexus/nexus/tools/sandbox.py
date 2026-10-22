"""Host-controlled sandbox manager. Docker is an optional capability.

Workers never receive the Docker socket or host mounts: the *host* launches a container with
only the task worktree mounted read-only, no network, bounded CPU/memory/pids and a timeout.
If prerequisites are missing the capability is reported as unavailable and code is not run.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any


class DockerSandbox:
    def __init__(self, image: str = "python:3.12-slim", docker_bin: str | None = None):
        self.image = image
        self.docker = docker_bin or shutil.which("docker")
        self._cached: tuple[bool, str] | None = None

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

    async def available(self, refresh: bool = False) -> tuple[bool, str]:
        if self._cached and not refresh:
            return self._cached
        if not self.docker:
            self._cached = (False, "docker CLI not found on PATH")
            return self._cached
        rc, out, err = await self._run("info", "--format", "{{.ServerVersion}}", timeout=15)
        if rc != 0:
            self._cached = (False, "docker daemon not reachable (is Docker Desktop running?)")
            return self._cached
        rc, _, _ = await self._run("image", "inspect", self.image, timeout=15)
        if rc != 0:
            self._cached = (False, f"sandbox image '{self.image}' not present locally; pull it explicitly:"
                                   f" docker pull {self.image}")
            return self._cached
        self._cached = (True, f"docker {out.strip()} image {self.image}")
        return self._cached

    async def run(self, worktree: Path, command: list[str], *, timeout_s: float = 120) -> dict[str, Any]:
        ok, why = await self.available()
        if not ok:
            raise RuntimeError(f"sandbox unavailable: {why}")
        if not all(isinstance(c, str) for c in command) or not command:
            raise ValueError("command must be a non-empty list of strings")
        args = ["run", "--rm", "--network", "none", "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
                "--read-only", "--tmpfs", "/tmp:rw,size=64m", "--security-opt", "no-new-privileges",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-v", f"{Path(worktree).resolve()}:/work:ro", "-w", "/work",
                self.image, *command]
        rc, out, err = await self._run(*args, timeout=timeout_s)
        return {"exit_code": rc, "stdout": out[-20000:], "stderr": err[-20000:], "timed_out": rc == 124,
                "passed": rc == 0, "sandbox": f"docker:{self.image}"}


class UnavailableSandbox:
    def __init__(self, reason: str):
        self.reason = reason

    async def available(self, refresh: bool = False) -> tuple[bool, str]:
        return False, self.reason

    async def run(self, worktree: Path, command: list[str], *, timeout_s: float = 120) -> dict[str, Any]:
        raise RuntimeError(f"sandbox unavailable: {self.reason}")
