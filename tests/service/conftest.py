import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("NEXUS_SECRET_BACKEND", "memory")

from nexus.appctx import build_context  # noqa: E402
from nexus.bootstrap import create_demo_project, ensure_fake_provider  # noqa: E402
from nexus.orchestration.supervisor import Supervisor  # noqa: E402
from nexus.secrets import MemorySecretStore  # noqa: E402
from nexus.tools.sandbox import UnavailableSandbox  # noqa: E402


@pytest.fixture
def ctx(tmp_path):
    c = build_context(tmp_path / "data", secrets=MemorySecretStore(),
                      sandbox=UnavailableSandbox("test environment: no sandbox"))
    c.heartbeat_s = 0.2
    c.lease_ttl_ms = 5_000
    yield c
    c.close()


@pytest.fixture
async def sup(ctx):
    s = Supervisor(ctx)
    await s.start()
    s._loop_task.cancel()  # tests drive ticks explicitly via wait_idle for determinism
    yield s
    await s.stop()
