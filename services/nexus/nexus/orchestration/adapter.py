"""NEXUS <-> Microsoft Agent Framework adapter. This is the ONLY module that imports the
framework (agent-framework-core 1.22.0 / agent-framework-orchestrations 1.3.2).

Per task attempt we build a small checkpointed workflow:

    Execute --WorkResult--> Gate --(request_info: SubmissionReady)--> [suspended]
       ^                      |
       +------Revision--------+   (when the NEXUS gate answers "revise")

The framework owns step execution and checkpoints. NEXUS owns acceptance: the Gate executor
never decides anything; it suspends and the host answers the request after running NEXUS
verification. A framework "completed" output is therefore never an acceptance.

Public framework APIs only: Executor, handler, response_handler, WorkflowBuilder,
WorkflowContext.request_info, FileCheckpointStorage, register_checkpoint_type, Workflow.run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent_framework import (
    Executor,
    FileCheckpointStorage,
    WorkflowBuilder,
    WorkflowContext,
    handler,
    register_checkpoint_type,
    response_handler,
)

FRAMEWORK_VERSIONS = {"agent-framework-core": "1.22.0", "agent-framework-orchestrations": "1.3.2"}


@dataclass
class AttemptStart:
    task_id: str
    attempt_id: str
    round: int = 1


@dataclass
class Revision:
    round: int
    feedback: list = field(default_factory=list)


@dataclass
class WorkResult:
    round: int
    artifact_id: str
    summary: str
    meta: dict = field(default_factory=dict)


@dataclass
class SubmissionReady:
    round: int
    artifact_id: str
    summary: str
    meta: dict = field(default_factory=dict)


@dataclass
class GateVerdict:
    action: str  # accept | revise | stop
    feedback: list = field(default_factory=list)
    reason: str = ""


@dataclass
class AttemptOutcome:
    action: str
    round: int
    reason: str = ""


for _t in (AttemptStart, Revision, WorkResult, SubmissionReady, GateVerdict, AttemptOutcome):
    register_checkpoint_type(_t)

ProduceFn = Callable[[int, list], Awaitable[WorkResult]]


class _ExecuteExecutor(Executor):
    def __init__(self, produce: ProduceFn):
        super().__init__(id="execute")
        self._produce = produce

    @handler
    async def on_start(self, msg: AttemptStart, ctx: WorkflowContext[WorkResult]) -> None:
        await ctx.send_message(await self._produce(msg.round, []))

    @handler
    async def on_revision(self, msg: Revision, ctx: WorkflowContext[WorkResult]) -> None:
        await ctx.send_message(await self._produce(msg.round, list(msg.feedback)))


class _GateExecutor(Executor):
    def __init__(self):
        super().__init__(id="gate")

    @handler
    async def on_work(self, msg: WorkResult, ctx: WorkflowContext[Revision]) -> None:
        await ctx.request_info(SubmissionReady(msg.round, msg.artifact_id, msg.summary, dict(msg.meta)), GateVerdict)

    @response_handler
    async def on_verdict(self, original: SubmissionReady, verdict: GateVerdict,
                         ctx: WorkflowContext[Revision, AttemptOutcome]) -> None:
        if verdict.action == "revise":
            await ctx.send_message(Revision(original.round + 1, list(verdict.feedback)))
        else:
            await ctx.yield_output(AttemptOutcome(verdict.action, original.round, verdict.reason))


class FrameworkRuntime:
    def __init__(self, checkpoint_dir: Path):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.storage = FileCheckpointStorage(self.checkpoint_dir)

    def build_attempt_workflow(self, task_id: str, produce: ProduceFn):
        execute, gate = _ExecuteExecutor(produce), _GateExecutor()
        return (
            WorkflowBuilder(name=f"nexus-task-{task_id}", start_executor=execute, checkpoint_storage=self.storage,
                            max_iterations=60)
            .add_edge(execute, gate)
            .add_edge(gate, execute)
            .build()
        )

    async def latest_checkpoint(self, workflow_name: str) -> dict[str, Any] | None:
        cp = await self.storage.get_latest(workflow_name=workflow_name)
        if cp is None:
            return None
        return {"checkpoint_id": cp.checkpoint_id, "iteration": cp.iteration_count,
                "pending_request_ids": list(cp.pending_request_info_events.keys()), "timestamp": cp.timestamp}

    async def drive(
        self,
        workflow,
        *,
        start: AttemptStart | None,
        resume_checkpoint_id: str | None,
        on_submission: Callable[[str, SubmissionReady], Awaitable[GateVerdict]],
        on_segment: Callable[[], Awaitable[None]] | None = None,
        on_event: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> AttemptOutcome | None:
        """Run the workflow until it yields an outcome. Each time it suspends on a
        SubmissionReady request we call ``on_submission`` (NEXUS verification) and resume with
        the verdict. Returns None if the workflow went idle without an outcome."""
        if resume_checkpoint_id:
            stream = workflow.run(checkpoint_id=resume_checkpoint_id, stream=True)
        else:
            stream = workflow.run(start, stream=True)
        for _ in range(100):
            pending: list[tuple[str, SubmissionReady]] = []
            outcome: AttemptOutcome | None = None
            async for ev in stream:
                if ev.type == "request_info" and isinstance(ev.data, SubmissionReady):
                    pending.append((ev.request_id, ev.data))
                elif ev.type == "output" and isinstance(ev.data, AttemptOutcome):
                    outcome = ev.data
                elif ev.type in ("executor_failed", "failed") and on_event is not None:
                    await on_event(ev.type, _safe_details(ev))
            if on_segment is not None:
                await on_segment()
            if outcome is not None:
                return outcome
            if not pending:
                return None
            responses = {}
            for rid, req in pending:
                responses[rid] = await on_submission(rid, req)
            stream = workflow.run(responses=responses, stream=True)
        raise RuntimeError("attempt workflow exceeded 100 suspend/resume segments")


def _safe_details(ev: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for attr in ("executor_id", "details"):
        try:
            v = getattr(ev, attr)
        except Exception:
            continue
        out[attr] = getattr(v, "message", v) if attr == "details" else v
    return out
