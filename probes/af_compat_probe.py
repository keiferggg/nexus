"""Stage 0 compatibility probe for Microsoft Agent Framework (agent-framework-core 1.22.0,
agent-framework-orchestrations 1.3.2).

Exercises, with deterministic fake workers and no network:
  A. plan -> execute -> review(reject) -> revise -> review(pass) -> suspend for NEXUS acceptance
     (request_info), persisted to a local FileCheckpointStorage.
  B. a *separate process* reconstructs the workflow from scratch and resumes the suspended
     workflow from the checkpoint by supplying the acceptance response.
  C. cancellation of a streaming run mid-execution.
  D. Magentic orchestration with a custom (deterministic) MagenticManagerBase subclass and
     executor participants, with checkpointing and orchestrator event streaming.

Usage:  python af_compat_probe.py <phase> <state_dir>
Phases: start | resume | cancel | magentic | magentic_resume | all  ("all" runs start/resume as subprocesses)
Writes a JSON result to <state_dir>/<phase>.json.  Exit code 0 means every assertion held.
"""

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agent_framework import (
    Executor,
    FileCheckpointStorage,
    Message,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowRunState,
    handler,
    register_checkpoint_type,
    response_handler,
)

WORKFLOW_NAME = "nexus-probe-pwr"


@dataclass
class MissionInput:
    goal: str
    must_contain: str


@dataclass
class WorkOrder:
    goal: str
    must_contain: str
    attempt: int = 1
    feedback: str = ""


@dataclass
class Submission:
    goal: str
    must_contain: str
    attempt: int
    content: str


@dataclass
class AcceptanceRequest:
    submission_content: str
    attempt: int
    checks: list = field(default_factory=list)


@dataclass
class AcceptanceDecision:
    accepted: bool
    reason: str


for _t in (MissionInput, WorkOrder, Submission, AcceptanceRequest, AcceptanceDecision):
    register_checkpoint_type(_t)


class Planner(Executor):
    @handler
    async def plan(self, msg: MissionInput, ctx: WorkflowContext[WorkOrder]) -> None:
        await ctx.send_message(WorkOrder(goal=msg.goal, must_contain=msg.must_contain))


class Worker(Executor):
    """Deterministic fake: first attempt omits the required token; revision includes it."""

    def __init__(self, id: str) -> None:
        super().__init__(id=id)
        self.attempts_made = 0

    @handler
    async def work(self, msg: WorkOrder, ctx: WorkflowContext[Submission]) -> None:
        self.attempts_made += 1
        if msg.attempt == 1:
            content = f"Draft for: {msg.goal}"
        else:
            content = f"Draft for: {msg.goal}. Includes {msg.must_contain}. (addressed: {msg.feedback})"
        await ctx.send_message(Submission(msg.goal, msg.must_contain, msg.attempt, content))

    async def on_checkpoint_save(self) -> dict:
        return {"attempts_made": self.attempts_made}

    async def on_checkpoint_restore(self, state: dict) -> None:
        self.attempts_made = state.get("attempts_made", 0)


class Reviewer(Executor):
    """Deterministic check; rejected work is routed back for revision. Passing work is NOT
    accepted here: the workflow suspends and asks the host (NEXUS) for the acceptance decision."""

    @handler
    async def review(self, sub: Submission, ctx: WorkflowContext[WorkOrder]) -> None:
        passed = sub.must_contain in sub.content
        if not passed:
            await ctx.send_message(
                WorkOrder(sub.goal, sub.must_contain, sub.attempt + 1, feedback=f"missing '{sub.must_contain}'")
            )
            return
        await ctx.request_info(
            AcceptanceRequest(sub.content, sub.attempt, checks=[{"name": "contains", "passed": True}]),
            AcceptanceDecision,
        )

    @response_handler
    async def on_decision(
        self, original: AcceptanceRequest, decision: AcceptanceDecision, ctx: WorkflowContext[WorkOrder, dict]
    ) -> None:
        await ctx.yield_output(
            {"accepted": decision.accepted, "attempt": original.attempt, "content": original.submission_content}
        )


def build(storage: FileCheckpointStorage):
    planner, worker, reviewer = Planner("planner"), Worker("worker"), Reviewer("reviewer")
    wf = (
        WorkflowBuilder(name=WORKFLOW_NAME, start_executor=planner, checkpoint_storage=storage)
        .add_edge(planner, worker)
        .add_edge(worker, reviewer)
        .add_edge(reviewer, worker)
        .build()
    )
    return wf, worker


async def phase_start(state_dir: Path) -> dict:
    storage = FileCheckpointStorage(state_dir / "ckpt")
    wf, worker = build(storage)
    events = []
    request = None
    async for ev in wf.run(MissionInput("summarize X", "SOURCE-1"), stream=True):
        events.append(ev.type)
        if ev.type == "request_info":
            request = {"request_id": ev.request_id, "attempt": ev.data.attempt, "content": ev.data.submission_content}
    latest = await storage.get_latest(workflow_name=WORKFLOW_NAME)
    assert request is not None, "workflow did not suspend for acceptance"
    assert request["attempt"] == 2, f"expected revision before acceptance, got attempt {request['attempt']}"
    assert worker.attempts_made == 2
    assert latest is not None and latest.pending_request_info_events, "no checkpoint with pending request"
    return {
        "ok": True,
        "event_types": sorted(set(events)),
        "request": request,
        "checkpoint_id": latest.checkpoint_id,
        "checkpoint_count": len(await storage.list_checkpoint_ids(workflow_name=WORKFLOW_NAME)),
        "final_state": str(wf.status) if hasattr(wf, "status") else None,
    }


async def phase_resume(state_dir: Path) -> dict:
    start = json.loads((state_dir / "start.json").read_text())
    storage = FileCheckpointStorage(state_dir / "ckpt")
    wf, worker = build(storage)  # brand-new instances in a new process
    outputs = []
    states = []
    async for ev in wf.run(
        checkpoint_id=start["checkpoint_id"],
        responses={start["request"]["request_id"]: AcceptanceDecision(True, "nexus checks passed")},
        stream=True,
    ):
        if ev.type == "output":
            outputs.append(ev.data)
        if ev.type == "status":
            states.append(str(ev.state))
    assert len(outputs) == 1 and outputs[0]["accepted"] and outputs[0]["attempt"] == 2, outputs
    assert worker.attempts_made == 2, f"executor state not restored: {worker.attempts_made}"
    return {"ok": True, "outputs": outputs, "restored_worker_attempts": worker.attempts_made, "states": states}


class SlowWorker(Executor):
    def __init__(self, id: str) -> None:
        super().__init__(id=id)

    @handler
    async def work(self, msg: WorkOrder, ctx: WorkflowContext[Submission]) -> None:
        await asyncio.sleep(30)
        await ctx.send_message(Submission(msg.goal, msg.must_contain, msg.attempt, "late"))


async def phase_cancel(state_dir: Path) -> dict:
    storage = FileCheckpointStorage(state_dir / "ckpt-cancel")
    planner, worker, reviewer = Planner("planner"), SlowWorker("worker"), Reviewer("reviewer")
    wf = (
        WorkflowBuilder(name="nexus-probe-cancel", start_executor=planner, checkpoint_storage=storage)
        .add_edge(planner, worker)
        .add_edge(worker, reviewer)
        .add_edge(reviewer, worker)
        .build()
    )
    seen = []

    async def consume():
        async for ev in wf.run(MissionInput("slow", "X"), stream=True):
            seen.append(ev.type)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.5)
    task.cancel()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        await task
    except asyncio.CancelledError:
        pass
    elapsed = loop.time() - t0
    assert elapsed < 5, f"cancellation took {elapsed:.1f}s"
    assert "output" not in seen
    ckpts = await storage.list_checkpoint_ids(workflow_name="nexus-probe-cancel")
    return {"ok": True, "events_before_cancel": seen, "cancel_latency_s": round(elapsed, 3), "checkpoints": len(ckpts)}


async def phase_magentic(state_dir: Path, resume_from=None) -> dict:
    from agent_framework_orchestrations import (
        MagenticBuilder,
        MagenticContext,
        MagenticManagerBase,
        MagenticProgressLedger,
        MagenticProgressLedgerItem,
    )
    from agent_framework import AgentResponse, BaseAgent

    class FakeAgent(BaseAgent):
        def __init__(self, name: str, reply: str):
            super().__init__(name=name, description=f"fake {name}")
            self._reply = reply

        def run(self, messages=None, *, stream=False, session=None, **kwargs):
            reply = Message(role="assistant", contents=[self._reply], author_name=self.name)

            if stream:
                from agent_framework import AgentResponseUpdate, ResponseStream

                async def gen():
                    yield AgentResponseUpdate(contents=reply.contents, role="assistant", author_name=self.name)

                return ResponseStream(gen(), finalizer=lambda ups: AgentResponse(messages=[reply]))

            async def _r():
                return AgentResponse(messages=[reply])

            return _r()

    class ScriptedManager(MagenticManagerBase):
        """Deterministic manager: researcher, then writer, then declares satisfied.
        NEXUS would treat 'satisfied' as a *submission*, never as acceptance."""

        def __init__(self):
            super().__init__(max_stall_count=2, max_round_count=6)
            self.calls = []

        async def plan(self, ctx: MagenticContext) -> Message:
            self.calls.append("plan")
            return Message(role="assistant", contents=["PLAN: research then write"], author_name="manager")

        async def replan(self, ctx: MagenticContext) -> Message:
            self.calls.append("replan")
            return Message(role="assistant", contents=["REPLAN"], author_name="manager")

        async def create_progress_ledger(self, ctx: MagenticContext) -> MagenticProgressLedger:
            spoke = [m.author_name for m in ctx.chat_history if m.author_name in ("researcher", "writer")]
            self.calls.append(f"ledger:{len(spoke)}")
            nxt = "researcher" if not spoke else "writer"
            done = len(spoke) >= 2
            item = MagenticProgressLedgerItem
            return MagenticProgressLedger(
                is_request_satisfied=item("scripted", done),
                is_in_loop=item("scripted", False),
                is_progress_being_made=item("scripted", True),
                next_speaker=item("scripted", nxt),
                instruction_or_question=item("scripted", f"{nxt}: do your part"),
            )

        async def prepare_final_answer(self, ctx: MagenticContext) -> Message:
            self.calls.append("final")
            return Message(role="assistant", contents=["FINAL"], author_name="manager")

    mgr = ScriptedManager()
    storage = FileCheckpointStorage(state_dir / "ckpt-magentic")
    wf = MagenticBuilder(
        participants=[FakeAgent("researcher", "fact: A"), FakeAgent("writer", "doc using fact A")],
        manager=mgr,
        checkpoint_storage=storage,
    ).build()
    if resume_from is not None:
        ids = await storage.list_checkpoint_ids(workflow_name=wf.name)
        cps = [await storage.load(i) for i in ids]
        cps.sort(key=lambda c: c.timestamp)
        mid = cps[len(cps) // 2]
        types, mag_events, outputs = [], [], []
        async for ev in wf.run(checkpoint_id=mid.checkpoint_id, stream=True):
            types.append(ev.type)
            if ev.type == "magentic_orchestrator":
                mag_events.append(str(ev.data.event_type))
            if ev.type == "output":
                outputs.append(ev.data)
        assert outputs, "resumed magentic run produced no output"
        assert "plan" not in mgr.calls, f"resume re-planned from scratch: {mgr.calls}"
        return {"ok": True, "resumed_from_iteration": mid.iteration_count, "of_checkpoints": len(cps),
                "manager_calls_after_resume": mgr.calls, "orchestrator_events": mag_events}
    types, mag_events, outputs = [], [], []
    async for ev in wf.run("write a doc", stream=True):
        types.append(ev.type)
        if ev.type == "magentic_orchestrator":
            mag_events.append(str(ev.data.event_type))
        if ev.type == "output":
            outputs.append(ev.data)
    ckpts = await storage.list_checkpoint_ids(workflow_name=wf.name)
    assert "plan" in mgr.calls and "final" in mgr.calls, mgr.calls
    assert outputs, "no final output"
    return {
        "ok": True,
        "manager_calls": mgr.calls,
        "orchestrator_events": mag_events,
        "event_types": sorted(set(types)),
        "checkpoints": len(ckpts),
        "output_repr": repr(outputs[-1])[:300],
    }


async def phase_magentic_resume(state_dir: Path) -> dict:
    return await phase_magentic(state_dir, resume_from=True)


PHASES = {
    "start": phase_start,
    "resume": phase_resume,
    "cancel": phase_cancel,
    "magentic": phase_magentic,
    "magentic_resume": phase_magentic_resume,
}


def main() -> int:
    phase, state_dir = sys.argv[1], Path(sys.argv[2])
    state_dir.mkdir(parents=True, exist_ok=True)
    if phase == "all":
        rc = 0
        for p in ("start", "resume", "cancel", "magentic", "magentic_resume"):
            r = subprocess.run([sys.executable, __file__, p, str(state_dir)])
            print(f"phase {p}: exit {r.returncode}")
            rc |= r.returncode
        return rc
    try:
        result = asyncio.run(PHASES[phase](state_dir))
    except Exception as exc:  # record the failing probe verbatim
        import traceback

        result = {"ok": False, "error": repr(exc), "traceback": traceback.format_exc()}
    (state_dir / f"{phase}.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str)[:3000])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
