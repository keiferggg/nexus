"""Application container and fault-injection points."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory.store import MemoryStore
from .orchestration.adapter import FrameworkRuntime
from .persistence.artifacts import ArtifactStore
from .persistence.db import Database
from .persistence.events import EventNotifier
from .providers.gateway import ModelGateway, ProviderRegistry
from .secrets import SecretStore, make_secret_store
from .tools.broker import ToolBroker
from .tools.sandbox import DockerSandbox
from .tools.worktree import WorktreeManager


class SimulatedCrash(BaseException):
    """Raised at an armed fault point. BaseException so ordinary handlers do not 'clean up'
    - it models abrupt process death for in-process tests."""


@dataclass
class FaultPoints:
    armed: set[str] = field(default_factory=set)
    hits: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        env = os.environ.get("NEXUS_FAULT_EXIT")
        self._exit_point = env or None

    def hit(self, name: str) -> None:
        self.hits.append(name)
        if self._exit_point == name:
            os._exit(77)  # real process death for subprocess fault tests
        if name in self.armed:
            self.armed.discard(name)
            raise SimulatedCrash(name)


@dataclass
class AppContext:
    data_dir: Path
    db: Database
    artifacts: ArtifactStore
    secrets: SecretStore
    registry: ProviderRegistry
    gateway: ModelGateway
    broker: ToolBroker
    memory: MemoryStore
    worktrees: WorktreeManager
    sandbox: Any
    runtime: FrameworkRuntime
    notifier: EventNotifier
    faults: FaultPoints
    services: Any = None  # AppServices (connectors, objectives, toolsmith, collaboration)
    lease_ttl_ms: int = 30_000
    heartbeat_s: float = 5.0
    tick_s: float = 0.5

    def close(self) -> None:
        self.db.close()


def build_context(data_dir: Path, *, secrets: SecretStore | None = None, sandbox: Any = None,
                  http_transport: Any = None, web_resolver: Any = None) -> AppContext:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "nexus.db")
    artifacts = ArtifactStore(db, data_dir / "artifacts")
    secrets = secrets or make_secret_store()
    registry = ProviderRegistry(db, secrets)
    memory = MemoryStore(db)
    sandbox = sandbox if sandbox is not None else DockerSandbox()
    broker = ToolBroker(db, artifacts, sandbox=sandbox, http_transport=http_transport, memory=memory,
                        web_resolver=web_resolver)
    faults = FaultPoints()
    gateway = ModelGateway(db, registry, artifacts)
    gateway.fault_hook = faults.hit
    ctx = AppContext(
        data_dir=data_dir,
        db=db,
        artifacts=artifacts,
        secrets=secrets,
        registry=registry,
        gateway=gateway,
        broker=broker,
        memory=memory,
        worktrees=WorktreeManager(data_dir / "worktrees"),
        sandbox=sandbox,
        runtime=FrameworkRuntime(data_dir / "checkpoints"),
        notifier=EventNotifier(db),
        faults=faults,
    )
    from .autonomy.services import AppServices

    ctx.services = AppServices(ctx, connector_transport=http_transport)
    return ctx
