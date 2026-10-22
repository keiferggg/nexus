"""Deterministic verification checks and plan validation.

Model review complements these checks (see orchestration.verifier); it is not a truth oracle.
Each check returns {"verdict": pass|fail|inconclusive, "summary": str, "details": {...}}.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .understanding import lineage_summary

CITE_RE = re.compile(r"\[E:(ev_[0-9a-f]+)\]")
QUOTE_RE = re.compile(r"[\"“]([^\"”]{12,400})[\"”]\s*\[E:(ev_[0-9a-f]+)\]")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,40}$")
TASK_KINDS = {"research", "write", "code", "check", "integrate", "act", "discover"}
CHECK_KINDS = {"min_words", "contains_text", "regex", "cites_evidence", "quotes_verified", "json_valid", "file_exists",
               "python_syntax", "sandbox_command", "includes_inputs", "no_conflicts", "reviewer", "plan_schema",
               "owner_approval", "actions_settled", "objectives_proposed", "no_injected_instructions"}
ROLE_FOR_KIND = {"research": "researcher", "write": "writer", "code": "engineer", "check": "reviewer",
                 "integrate": "integrator", "act": "operator", "discover": "strategist"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def validate_plan(plan: Any, *, success_condition_ids: list[str], constraint_ids: list[str],
                  max_tasks: int = 16) -> list[str]:
    errs: list[str] = []
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        return ["plan must be an object with a 'tasks' list"]
    tasks = plan["tasks"]
    if not tasks:
        errs.append("plan has no tasks")
    if len(tasks) > max_tasks:
        errs.append(f"plan has {len(tasks)} tasks; maximum is {max_tasks}")
    keys = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            errs.append(f"task[{i}] is not an object")
            continue
        k = t.get("key")
        if not isinstance(k, str) or not KEY_RE.match(k) or k in ("plan",) or k.startswith("replan"):
            errs.append(f"task[{i}] key invalid: {k!r}")
        elif k in keys:
            errs.append(f"duplicate task key {k}")
        keys.add(k)
        if t.get("kind") not in TASK_KINDS:
            errs.append(f"task {k}: kind must be one of {sorted(TASK_KINDS)}")
        for f in ("title", "description"):
            if not isinstance(t.get(f), str) or not t[f].strip():
                errs.append(f"task {k}: missing {f}")
        acc = t.get("acceptance")
        if not isinstance(acc, list) or not acc:
            errs.append(f"task {k}: needs at least one acceptance criterion")
        else:
            for a in acc:
                ck = (a.get("check") or {}).get("kind") if isinstance(a, dict) else None
                if ck not in CHECK_KINDS - {"plan_schema"}:
                    errs.append(f"task {k}: acceptance check kind {ck!r} unknown")
        for c in t.get("constraint_ids", []) or []:
            if c not in constraint_ids:
                errs.append(f"task {k}: unknown constraint id {c}")
        addr = t.get("addresses")
        if addr is not None and addr not in success_condition_ids and addr != "integration":
            errs.append(f"task {k}: addresses unknown success condition {addr}")
    for t in tasks:
        if isinstance(t, dict):
            for d in t.get("depends_on", []) or []:
                if d not in keys:
                    errs.append(f"task {t.get('key')}: depends on unknown task {d}")
    # cycle check
    graph = {t["key"]: list(t.get("depends_on") or []) for t in tasks if isinstance(t, dict) and isinstance(t.get("key"), str)}
    state: dict[str, int] = {}

    def visit(n: str, stack: list[str]) -> None:
        if state.get(n) == 1:
            errs.append(f"dependency cycle: {' -> '.join(stack + [n])}")
            return
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in graph.get(n, []):
            if d in graph:
                visit(d, stack + [n])
        state[n] = 2

    for n in graph:
        visit(n, [])
    covered = {t.get("addresses") for t in tasks if isinstance(t, dict)}
    for sc in success_condition_ids:
        if sc not in covered:
            errs.append(f"success condition {sc} is not addressed by any task")
    return errs


def run_check(cur: sqlite3.Cursor, check: dict[str, Any], *, content: str, mission_id: str,
              submission_meta: dict[str, Any], worktree: Path | None, plan_context: dict[str, Any] | None = None
              ) -> dict[str, Any]:
    kind = check.get("kind")
    args = check.get("args") or {}
    if kind == "plan_schema":
        try:
            plan = json.loads(content)
        except json.JSONDecodeError as exc:
            return {"verdict": "fail", "summary": f"plan is not valid JSON: {exc}", "details": {}}
        errs = validate_plan(plan, **(plan_context or {"success_condition_ids": [], "constraint_ids": []}))
        return {"verdict": "fail" if errs else "pass", "summary": "; ".join(errs[:6]) or "plan valid",
                "details": {"errors": errs}}
    if kind == "min_words":
        n = len(re.findall(r"\w+", content))
        need = int(args.get("min", 50))
        return {"verdict": "pass" if n >= need else "fail", "summary": f"{n} words (minimum {need})",
                "details": {"words": n}}
    if kind == "contains_text":
        text = args.get("text", "")
        ok = _norm(text) in _norm(content)
        return {"verdict": "pass" if ok else "fail", "summary": f"{'contains' if ok else 'missing'} required text {text!r}",
                "details": {}}
    if kind == "regex":
        ok = re.search(args.get("pattern", "$^"), content, re.M) is not None
        return {"verdict": "pass" if ok else "fail", "summary": f"pattern {'found' if ok else 'not found'}", "details": {}}
    if kind == "json_valid":
        try:
            json.loads(content)
            return {"verdict": "pass", "summary": "valid JSON", "details": {}}
        except json.JSONDecodeError as exc:
            return {"verdict": "fail", "summary": f"invalid JSON: {exc}", "details": {}}
    if kind == "cites_evidence":
        ids = sorted(set(CITE_RE.findall(content)))
        lin = lineage_summary(cur, ids)
        foreign = [r["id"] for r in cur.execute(
            f"SELECT id FROM evidence WHERE id IN ({','.join('?' * len(ids))}) AND mission_id IS NOT ?",
            (*ids, mission_id)).fetchall()] if ids else []
        need = int(args.get("min_independent", 1))
        problems = []
        if lin["unknown"]:
            problems.append(f"cites unknown evidence {lin['unknown']}")
        if lin["inactive"]:
            problems.append(f"cites withdrawn/changed evidence {lin['inactive']}")
        if foreign:
            problems.append(f"cites evidence from another mission {foreign}")
        if lin["independent_lineages"] < need:
            problems.append(f"{lin['independent_lineages']} independent source lineage(s) cited; {need} required"
                            f" ({lin['active']} evidence item(s) share lineages {lin['lineages']})")
        return {"verdict": "fail" if problems else "pass",
                "summary": "; ".join(problems) or f"{lin['independent_lineages']} independent lineage(s) cited",
                "details": {**lin, "cited": ids}}
    if kind == "quotes_verified":
        quotes = QUOTE_RE.findall(content)
        if not quotes:
            need = bool(args.get("require_quotes", False))
            return {"verdict": "fail" if need else "pass",
                    "summary": "no attributed quotations" + (" (at least one required)" if need else ""), "details": {}}
        bad = []
        for q, eid in quotes:
            r = cur.execute("SELECT e.artifact_id, e.excerpt FROM evidence e WHERE e.id = ?", (eid,)).fetchone()
            src = ""
            if r is not None and r["artifact_id"]:
                a = cur.execute("SELECT sha256 FROM artifacts WHERE id = ?", (r["artifact_id"],)).fetchone()
                src = submission_meta.get("_artifact_reader")(a["sha256"]) if submission_meta.get("_artifact_reader") else (r["excerpt"] or "")
            elif r is not None:
                src = r["excerpt"] or ""
            if _norm(q) not in _norm(src):
                bad.append({"quote": q[:120], "evidence": eid})
        return {"verdict": "fail" if bad else "pass",
                "summary": f"{len(quotes) - len(bad)}/{len(quotes)} quotations found verbatim in cited sources",
                "details": {"unverified": bad}}
    if kind in ("file_exists", "python_syntax"):
        if worktree is None:
            return {"verdict": "inconclusive", "summary": "no worktree for this submission", "details": {}}
        paths = args.get("paths") or submission_meta.get("changed_files") or []
        if kind == "file_exists":
            missing = [p for p in paths if not (worktree / p).exists()]
            return {"verdict": "fail" if missing else "pass", "summary": f"missing {missing}" if missing else
                    f"{len(paths)} file(s) present", "details": {"missing": missing}}
        from ..tools.worktree import python_syntax_check

        py = [p for p in paths if p.endswith(".py")]
        res = python_syntax_check(worktree, py)
        return {"verdict": "pass" if res["passed"] else "fail",
                "summary": f"{len(py)} python file(s) parse" if res["passed"] else f"syntax errors: {res['errors']}",
                "details": res}
    if kind == "actions_settled":
        # Real actions requested by this task in the submission's epoch, as executed by NEXUS.
        rows = cur.execute(
            "SELECT ar.status, ar.action, ar.id FROM action_requests ar JOIN task_attempts a ON a.id = ar.attempt_id"
            " WHERE a.task_id = ? AND a.epoch = ?", (submission_meta.get("_task_id"), submission_meta.get("_epoch"))).fetchall()
        need = int(args.get("min_actions", 1))
        by: dict[str, list[str]] = {}
        for r in rows:
            by.setdefault(r["status"], []).append(r["action"])
        waiting = sum(len(by.get(s, [])) for s in ("pending", "approved", "executing", "uncertain"))
        bad = by.get("failed", []) + by.get("denied", [])
        ok = len(by.get("succeeded", []))
        details = {"by_status": by}
        if bad:
            return {"verdict": "fail", "summary": f"action(s) failed or were denied: {bad}", "details": details}
        if waiting:
            return {"verdict": "inconclusive", "summary": f"{waiting} action(s) awaiting owner approval or reconciliation",
                    "details": details}
        if ok < need:
            return {"verdict": "fail", "summary": f"{ok} action(s) completed; {need} required", "details": details}
        return {"verdict": "pass", "summary": f"{ok} real action(s) completed and recorded as evidence", "details": details}
    if kind == "no_injected_instructions":
        from ..guard.injection import scan

        s = scan(content)
        return {"verdict": "fail" if s["flagged"] else "pass",
                "summary": ("deliverable contains text addressed to AI agents (possibly copied from an injected source): "
                            + "; ".join(m["excerpt"][:80] for m in s["matches"][:2])) if s["flagged"]
                else "no instructions-to-agents found in the deliverable", "details": s}
    if kind == "objectives_proposed":
        n = cur.execute("SELECT COUNT(*) AS n FROM objective_proposals WHERE source_task_id = ? AND status IN"
                        " ('proposed','adopted')", (submission_meta.get("_task_id"),)).fetchone()["n"]
        need = int(args.get("min", 1))
        return {"verdict": "pass" if n >= need else "fail",
                "summary": f"{n} valid objective proposal(s) linked to standing goals (minimum {need})", "details": {"count": n}}
    if kind == "includes_inputs" and submission_meta.get("kind") == "integrate_code":
        merged = set(submission_meta.get("merged_branches", []))
        missing = [i["task_key"] for i in submission_meta.get("code_inputs", []) if i["branch"] not in merged]
        return {"verdict": "fail" if missing else "pass",
                "summary": f"branches not integrated: {missing}" if missing else
                f"all {len(submission_meta.get('code_inputs', []))} input branch(es) are in the integrated history",
                "details": {"missing": missing}}
    if kind == "includes_inputs":
        inputs = submission_meta.get("inputs", [])
        missing = [i["task_key"] for i in inputs if i["artifact_id"] not in content and
                   _norm(i.get("title", "")) not in _norm(content)]
        return {"verdict": "fail" if missing else "pass",
                "summary": f"missing inputs from {missing}" if missing else f"includes all {len(inputs)} accepted input(s)",
                "details": {"missing": missing}}
    if kind == "no_conflicts":
        conflicts = submission_meta.get("conflicts", [])
        return {"verdict": "fail" if conflicts else "pass",
                "summary": f"{len(conflicts)} merge conflict(s): {[c['files'] for c in conflicts]}" if conflicts
                else "branches merged without conflicts", "details": {"conflicts": conflicts}}
    return {"verdict": "inconclusive", "summary": f"check kind {kind!r} is not deterministic here", "details": {}}


def build_verification_plan(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Map each acceptance criterion to its check, verifier and independence level."""
    plan = []
    for a in task.get("acceptance", []):
        check = a.get("check") or {"kind": "reviewer"}
        is_review = check.get("kind") == "reviewer"
        is_owner = check.get("kind") == "owner_approval"
        plan.append({
            "criterion": a["id"],
            "text": a.get("text", ""),
            "check": check,
            "evidence_required": a.get("evidence_required") or ("reviewer assessment artifact" if is_review
                                                                 else "raw check result artifact"),
            "verifier": "role:reviewer" if is_review else ("owner" if is_owner else "nexus:deterministic"),
            "independence": "independent_agent" if is_review else ("owner" if is_owner else "deterministic"),
            "required": a.get("required", True),
            "limitations": ("model judgment; complements deterministic checks, not a truth oracle" if is_review
                            else LIMITATIONS.get(check.get("kind"), "")),
        })
    return plan


LIMITATIONS = {
    "min_words": "length only; says nothing about quality",
    "cites_evidence": "checks citation existence and lineage independence, not that citations support the claims",
    "quotes_verified": "verifies quoted text appears in the cited source; paraphrase is not checked",
    "python_syntax": "parses code without executing it; behavior is not validated",
    "file_exists": "existence only",
    "includes_inputs": "checks inclusion of accepted inputs by id/title",
    "no_conflicts": "git-level textual conflicts only",
    "sandbox_command": "result depends on test quality; requires the Docker sandbox",
    "owner_approval": "human judgment recorded by the owner; used where automated checks cannot run",
    "actions_settled": "confirms the destination accepted the request; not that the action achieved its purpose",
    "objectives_proposed": "checks that proposals are structurally valid and goal-linked, not that they are worthwhile",
    "no_injected_instructions": "heuristic; catches copied injection text, not subtle bias",
}
