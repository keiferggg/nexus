"""Scripted fake-provider behaviors for fault tests (deterministic)."""
import asyncio
import json

from nexus.demo import script as demo


def one_task_plan(kind="write", acceptance=None, key="draft", constraint_ids=None):
    acceptance = acceptance or [{"id": "A1", "text": ">= 20 words", "check": {"kind": "min_words", "args": {"min": 20}}}]

    def _plan(packet):
        return {"rationale": "test plan", "tasks": [{"key": key, "title": f"{kind} task", "kind": kind,
                                                     "description": "test", "addresses": "SC1", "depends_on": [],
                                                     "constraint_ids": constraint_ids or [], "acceptance": acceptance}]}
    return _plan


def make_script(plan_fn=None, writer=None, delays=None):
    """delays: list of (role, seconds) consumed in order for the first matching calls."""
    delays = list(delays or [])
    calls = {"n": 0}

    def script(req, packet):
        role = req.metadata.get("role")
        if req.metadata.get("purpose") == "review" or role == "reviewer":
            return demo.review(req, packet)
        if role == "planner":
            return (plan_fn or demo.plan)(packet)
        if role == "integrator":
            return demo.integrate_doc(req, packet)
        if role == "researcher":
            return demo.research(req, packet)
        calls["n"] += 1
        if writer:
            return writer(req, packet, calls["n"])
        return {"content_markdown": "word " * 40, "summary": "ok"}
    return script


class SlowFirst:
    """Wraps a FakeProvider so the first call matching role sleeps (simulating a hung worker)."""

    def __init__(self, inner, role, seconds):
        self.inner, self.role, self.seconds, self.done = inner, role, seconds, False
        self.kind, self.sends_data_off_machine = inner.kind, False

    async def list_models(self):
        return await self.inner.list_models()

    async def complete(self, req, *, timeout_s):
        if not self.done and req.metadata.get("role") == self.role:
            self.done = True
            await asyncio.sleep(self.seconds)
        return await self.inner.complete(req, timeout_s=timeout_s)
