"""Scenario 16 + Stage 4 exit: candidates are rejected on held-out failure, promoted when the
policy passes, and can be rolled back. Uses the real pipeline via MissionEvalHarness."""
import re

import pytest

from nexus.bootstrap import create_demo_project, ensure_fake_provider
from nexus.demo import script as demo
from nexus.learning.opportunities import consolidate_memory, may_run_in_background, scan
from nexus.learning.skills import EvalCase, EvalSuite, MissionEvalHarness, SkillRegistry


def directive_writer(req, packet):
    """Deterministic 'model' that follows skill directives found in its context packet."""
    role = req.metadata.get("role")
    if req.metadata.get("purpose") == "review" or role == "reviewer":
        return demo.review(req, packet)
    directives = {}
    for s in packet.get("skills", []):
        directives.update(s["content"].get("directives", {}))
    words = int(packet["task"]["description"].split("words=")[1]) if "words=" in packet["task"]["description"] else 40
    if "max_words" in directives:
        words = min(words, directives["max_words"])
    body = "Finding " * words
    if directives.get("append_section"):
        body += f"\n\n## {directives['append_section']}\n- none recorded"
    return {"content_markdown": body, "summary": "eval output"}


def case(cid, split, *, words, require_section, min_words):
    acc = [{"id": "A1", "text": "length", "check": {"kind": "min_words", "args": {"min": min_words}}}]
    if require_section:
        acc.append({"id": "A2", "text": "has open questions", "check": {"kind": "contains_text",
                                                                        "args": {"text": "## Open questions"}}})
    return EvalCase(cid, split, {"key": "case", "title": f"eval {cid}", "kind": "write",
                                 "description": f"Write a note words={words}", "acceptance": acc})


SUITE = EvalSuite("write-procedures-v1", [
    case("t1", "train", words=40, require_section=True, min_words=20),
    case("t2", "train", words=50, require_section=True, min_words=20),
    case("h1", "heldout", words=120, require_section=True, min_words=100),
    case("h2", "heldout", words=150, require_section=False, min_words=100),
    case("p1", "past_failures", words=40, require_section=True, min_words=20),
])


@pytest.fixture
async def harness(ctx, sup):
    ensure_fake_provider(ctx, script=directive_writer)
    pid = create_demo_project(ctx, "research")
    return MissionEvalHarness(sup, pid)


async def test_overfit_candidate_cannot_auto_promote(ctx, sup, harness):
    reg = SkillRegistry(ctx.db)
    with ctx.db.tx() as cur:
        cur.execute("INSERT INTO settings VALUES ('learning.auto_promote', ?, '')",
                    ('{"enabled": true, "scopes": ["concise-notes"]}',))
    sid = reg.propose(skill_name="concise-notes", target="procedure",
                      hypothesis="Short notes with an open-questions section pass review more often",
                      provenance={"from": "train cases t1,t2"}, conditions="write tasks",
                      content={"applies_to_kinds": ["write"], "guidance": "Be brief; add open questions.",
                               "directives": {"append_section": "Open questions", "max_words": 60}},
                      proposer="nexus:learning")
    rep = await reg.evaluate(sid, SUITE, harness)
    print("OVERFIT", rep["rates"], rep["reasons"])
    assert rep["rates"]["train"]["candidate"] == 1.0 and rep["rates"]["train"]["baseline"] == 0.0
    assert rep["rates"]["heldout"]["candidate"] < rep["rates"]["heldout"]["baseline"] or \
        rep["rates"]["heldout"]["candidate"] < 0.8
    assert reg.get(sid)["status"] == "rejected" and not rep["eligible"]
    assert any("held-out" in r for r in rep["reasons"])
    with pytest.raises(ValueError):
        reg.promote(sid, actor="owner")


async def test_candidate_promoted_then_rolled_back(ctx, sup, harness):
    reg = SkillRegistry(ctx.db)
    sid = reg.propose(skill_name="open-questions", target="procedure",
                      hypothesis="Adding an open-questions section satisfies reviewers without shortening work",
                      provenance={"from": "recurring contains_text failures"}, conditions="write tasks",
                      content={"applies_to_kinds": ["write"], "directives": {"append_section": "Open questions"}},
                      proposer="nexus:learning")
    rep = await reg.evaluate(sid, SUITE, harness)
    assert rep["eligible"], rep["reasons"]
    assert reg.get(sid)["status"] == "evaluating", "auto-promotion is off: waits for the owner"
    with pytest.raises(PermissionError):
        reg.promote(sid, actor="nexus:learning")
    reg.promote(sid, actor="owner")
    assert reg.get(sid)["status"] == "promoted"
    sid2 = reg.propose(skill_name="open-questions", target="procedure", hypothesis="v2 variant",
                       provenance={"from": "owner"}, conditions="write tasks",
                       content={"applies_to_kinds": ["write"], "directives": {"append_section": "Open questions"}},
                       proposer="owner")
    assert reg.get(sid2)["parent_version_id"] == sid
    rep2 = await reg.evaluate(sid2, SUITE, harness)
    assert not rep2["eligible"] and "no measured improvement" in " ".join(rep2["reasons"])
    restored = reg.rollback(sid, actor="owner", reason="owner rollback drill")
    assert reg.get(sid)["status"] == "rolled_back" and restored is None


async def test_opportunities_need_policy_and_goal(ctx, sup, harness):
    from nexus.domain.missions import pause_mission
    from helpers import new_mission

    pid = harness.project_id
    ensure_fake_provider(ctx)
    mid = new_mission(ctx, pid, start=True)
    await sup.tick()
    with ctx.db.tx() as cur:
        pause_mission(cur, mid)
        cur.execute("UPDATE tasks SET status='ready' WHERE mission_id = ? AND status IN ('leased','running')", (mid,))
    found = scan(ctx.db, pid)
    opp = ctx.db.one("SELECT * FROM opportunities WHERE mission_id = ?", (mid,))
    assert found and opp["goal_ref"] == f"mission:{mid}" and opp["expires_at"]
    assert scan(ctx.db, pid) == [], "deduplicated"
    ok, why = may_run_in_background(ctx.db, opp)
    assert not ok and "disabled" in why
    assert consolidate_memory(ctx.db, pid)["merged"] >= 0
