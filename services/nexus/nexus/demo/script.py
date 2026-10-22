"""DEMO SCRIPT for the deterministic fake provider (offline mode only).

It plays each role mechanically from the context packet so the whole pipeline - planning,
tool use, evidence lineage, verification, repair, integration - can run repeatably without a
real model. Its outputs are scripted, not intelligent; nothing it does says anything about
real model quality.

Scripted behaviors worth knowing (they exist to exercise NEXUS, and are labeled in the UI):
  * researcher round 1 cites two sources that share one lineage (survey + syndicated news), so
    an independence check fails; round 2 adds an independent source.
  * engineer writes files from fixtures/.nexus-demo/solutions.json.
  * integrator resolves code conflicts with the fixture's scripted resolution.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..providers.base import CompletionRequest

ACT_WORDS = ("notify", "open an issue", "file an issue", "post ", "send ", "announce", "comment on")
CODE_WORDS = ("function", "implement", "code", "module", "test", "refactor", "bug")
RESEARCH_WORDS = ("source", "research", "evidence", "summar", "investigat", "find")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return "-".join(s.split("-")[:4]) or "task"


def _tool_results(req: CompletionRequest) -> list[dict[str, Any]]:
    out = []
    for m in req.messages:
        if m.role == "tool":
            try:
                out.append({"name": m.tool_name, **json.loads(m.content)})
            except json.JSONDecodeError:
                pass
    return out


def _first_sentence(text: str) -> str:
    body = "\n".join(l for l in text.splitlines() if l.strip() and not l.startswith("#")
                     and not l.lower().startswith("source-lineage"))
    m = re.search(r"([A-Z][^.]{20,240}\.)", body)
    return m.group(1).strip() if m else body[:120]


def plan(packet: dict[str, Any]) -> dict[str, Any]:
    scs = packet["goal"]["success_conditions"]
    cons = [c["id"] for c in packet.get("hard_constraints", [])]
    tasks: list[dict[str, Any]] = []
    research_keys: list[str] = []
    for sc in scs:
        text = sc["text"]
        low = text.lower()
        key = _slug(text)
        if key in {t["key"] for t in tasks}:
            key = f"{key}-{sc['id'].lower()}"
        if any(w in low for w in ACT_WORDS):
            prior = [t["key"] for t in tasks if t["kind"] in ("research", "write")]
            tasks.append({"key": key, "title": text, "kind": "act", "description": text, "addresses": sc["id"],
                          "constraint_ids": cons, "depends_on": prior,
                          "acceptance": [{"id": "A1", "text": "Requested real actions completed",
                                          "check": {"kind": "actions_settled", "args": {"min_actions": 1}}}]})
        elif any(w in low for w in CODE_WORDS):
            name = "slugify" if "slugify" in low else ("truncate" if "truncate" in low else key)
            tasks.append({"key": name, "title": text, "kind": "code", "description": text, "addresses": sc["id"],
                          "constraint_ids": cons, "depends_on": [],
                          "acceptance": [{"id": "A1", "text": "Changed files exist", "check": {"kind": "file_exists"}},
                                         {"id": "A2", "text": "Tests pass in the sandbox",
                                          "check": {"kind": "sandbox_command", "args": {"command": ["python", "-m", "pytest", "-q"]}}}],
                          "predictions": [{"statement": "Passes verification on the first round", "observable": "first_round_passes",
                                           "expected": True}]})
        elif any(w in low for w in RESEARCH_WORDS):
            research_keys.append(key)
            tasks.append({"key": key, "title": text, "kind": "research", "description": text, "addresses": sc["id"],
                          "constraint_ids": cons, "depends_on": [],
                          "acceptance": [{"id": "A1", "text": "Cites at least two independent source lineages",
                                          "check": {"kind": "cites_evidence", "args": {"min_independent": 2}}},
                                         {"id": "A2", "text": "Quotations appear verbatim in cited sources",
                                          "check": {"kind": "quotes_verified", "args": {"require_quotes": True}}},
                                         {"id": "A3", "text": "Independent review finds no blocking issue",
                                          "check": {"kind": "reviewer"}}],
                          "predictions": [{"statement": "Passes verification on the first round", "observable": "first_round_passes",
                                           "expected": True}]})
        else:
            tasks.append({"key": key, "title": text, "kind": "write", "description": text, "addresses": sc["id"],
                          "constraint_ids": cons, "depends_on": list(research_keys),
                          "acceptance": [{"id": "A1", "text": "At least 80 words", "check": {"kind": "min_words", "args": {"min": 80}}},
                                         {"id": "A2", "text": "Grounded in recorded evidence",
                                          "check": {"kind": "cites_evidence", "args": {"min_independent": 1}}},
                                         {"id": "A3", "text": "Independent review finds no blocking issue",
                                          "check": {"kind": "reviewer"}}]})
    uncertainties = []
    if research_keys:
        uncertainties.append({"kind": "missing_evidence", "question": "Do the workspace sources include independent evidence of demand?",
                              "would_change": "If not, the recommendation must be framed as provisional.",
                              "affects": research_keys, "cost_hint": "cheap"})
    return {"rationale": f"DEMO SCRIPT: one task per success condition ({len(scs)}), research before writing.",
            "tasks": tasks, "uncertainties": uncertainties,
            "assumptions": [{"statement": "Workspace sources are the only permitted evidence", "basis": "constraints"}]}


def research(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    attempted = [r for r in _tool_results(req) if r.get("name") == "read_source"]
    results = [r for r in attempted if r.get("ok") and r["result"].get("evidence_status", "active") == "active"]
    sources = packet.get("workspace", [{}])[0].get("sources", [])
    if not attempted:
        return {"__tool_calls__": [{"id": f"rs{i}", "name": "read_source", "arguments": {"path": p}}
                                   for i, p in enumerate(sources) if p.endswith(".md")]}
    retry = bool(packet.get("previous_failures"))
    picked = []
    seen_lineage: dict[str, int] = {}
    for r in results:
        res = r["result"]
        picked.append(res)
        seen_lineage[res["lineage_root"]] = seen_lineage.get(res["lineage_root"], 0) + 1
    if not retry:
        # Scripted mistake: cite only sources that share one lineage.
        shared = [lr for lr, n in seen_lineage.items() if n > 1]
        if shared:
            picked = [p for p in picked if p["lineage_root"] == shared[0]]
    lines = ["## Findings", ""]
    claims = []
    for p in picked:
        q = _first_sentence(p["content"])
        lines.append(f"- {p['title']}: \"{q}\" [E:{p['evidence_id']}]")
        claims.append({"proposition": q, "evidence_ids": [p["evidence_id"]]})
    lines += ["", f"Sources consulted: {len(results)}. Sources cited: {len(picked)}.",
              "Uncertainty: the sources are local documents; no external verification was performed."]
    return {"content_markdown": "\n".join(lines), "summary": f"Cited {len(picked)} source(s)"
            + (" after adding an independent source" if retry else ""), "claims": claims,
            "uncertainties": [] if retry else [{"kind": "explanation", "question": "Are the cited sources independent?",
                                                "would_change": "Whether demand is corroborated"}]}


def write(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    tool_names = {t.name for t in req.tools}
    consulted = [r for r in _tool_results(req) if r.get("name") == "consult"]
    if "consult" in tool_names and not consulted and req.metadata.get("purpose") == "execute":
        return {"__tool_calls__": [{"id": "c1", "name": "consult", "arguments": {
            "role": "researcher", "question": "What is the weakest part of the evidence for this recommendation?"}}]}
    advice = consulted[0]["result"]["answer"] if consulted and consulted[0].get("ok") else None
    inputs = packet.get("inputs", [])
    cites = sorted(set(re.findall(r"\[E:(ev_[0-9a-f]+)\]", " ".join(i.get("content", "") for i in inputs))))
    if not cites:
        cites = [e["id"] for e in packet.get("evidence", [])][:2]
    body = ["This recommendation is based only on the accepted research findings recorded in this mission.",
            "", "## Recommendation", "",
            "Proceed with a twelve-month pilot, conditional on a cost estimate, because recorded evidence shows "
            "organized merchant support and stated resident interest " + " ".join(f"[E:{c}]" for c in cites) + ".",
            "", "## What the evidence does and does not show", "",
            "Stated interest in a survey is not the same as observed use, and the main cited barrier is the lack of "
            "protected lanes, which a pilot alone would not fix. The council minutes record no funding decision.",
            *(["", "## Researcher's caution (consulted)", "", advice] if advice else []),
            "", "## Open questions", "",
            "- What will the pilot cost per station, and who funds it?",
            "- Should protected lanes on Main Street be a precondition?",
            "", "## Basis", "", f"Inputs: {', '.join(i['task_key'] for i in inputs) or 'none'}."]
    return {"content_markdown": "\n".join(body), "summary": "Recommendation drafted from accepted findings",
            "claims": [{"proposition": "A twelve-month pilot is supported by recorded evidence", "evidence_ids": cites}]}


def review(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    sub = next((i for i in packet.get("inputs", []) if i.get("title") == "submission under review"), {})
    content = sub.get("content", "")
    words = len(re.findall(r"\w+", content))
    cites = re.findall(r"\[E:(ev_[0-9a-f]+)\]", content)
    findings = []
    if words < 25:
        findings.append({"criterion": "substance", "issue": f"only {words} words", "severity": "blocking"})
    if packet["task"].get("key") and "plan" not in packet["task"]["key"] and not cites and "diff" not in content[:20]:
        findings.append({"criterion": "grounding", "issue": "no evidence citations", "severity": "blocking"})
    verdict = "fail" if any(f["severity"] == "blocking" for f in findings) else "pass"
    return {"verdict": verdict, "summary": f"DEMO reviewer heuristic: {words} words, {len(set(cites))} cited evidence id(s)",
            "findings": findings}


def integrate_doc(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    parts = [f"# {packet['goal']['objective']}", "", "_Integrated deliverable assembled from accepted task outputs._", ""]
    for i in packet.get("inputs", []):
        parts += [f"## {i['title']}", "", i.get("content", "").strip(), ""]
    return {"content_markdown": "\n".join(parts), "summary": f"Integrated {len(packet.get('inputs', []))} accepted input(s)"}


def engineer(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    results = _tool_results(req)
    sol = next((r for r in results if r.get("name") == "repo_read" and r.get("ok")), None)
    wrote = [r for r in results if r.get("name") == "repo_write" and r.get("ok")]
    if sol is None:
        return {"__tool_calls__": [{"id": "s0", "name": "repo_read", "arguments": {"path": ".nexus-demo/solutions.json"}}]}
    solutions = json.loads(sol["result"]["content"])
    key = packet["task"]["key"]
    files = solutions.get(key) or next((v for k, v in solutions.items() if not k.startswith("_") and k in key), {})
    if not wrote and files:
        return {"__tool_calls__": [{"id": f"w{i}", "name": "repo_write", "arguments": {"path": p, "content": c}}
                                   for i, (p, c) in enumerate(files.items())]}
    return {"summary": f"DEMO SCRIPT wrote {len(files)} scripted file(s)", "files_changed": list(files),
            "test_command": ["python", "-m", "pytest", "-q"], "notes": "scripted fixture output"}


def resolve_conflict(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    results = _tool_results(req)
    sol = next((r for r in results if r.get("name") == "repo_read" and r.get("ok")), None)
    wrote = [r for r in results if r.get("name") == "repo_write" and r.get("ok")]
    if sol is None:
        return {"__tool_calls__": [{"id": "s0", "name": "repo_read", "arguments": {"path": ".nexus-demo/solutions.json"}}]}
    res = json.loads(sol["result"]["content"]).get("_resolutions", {})
    if not wrote:
        return {"__tool_calls__": [{"id": f"w{i}", "name": "repo_write", "arguments": {"path": p, "content": c}}
                                   for i, (p, c) in enumerate(res.items())]}
    return {"summary": "DEMO SCRIPT applied the fixture's scripted resolution", "resolved_files": list(res),
            "rationale": "keeps both helper functions"}


def consult_answer(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    ev = packet.get("evidence", [])
    shared: dict[str, int] = {}
    for e in ev:
        shared[e["lineage_root"]] = shared.get(e["lineage_root"], 0) + 1
    dup = [k for k, n in shared.items() if n > 1]
    ans = (f"Two recorded items share one origin ({dup[0]}), so demand rests on a single survey; treat it as stated"
           f" interest, not observed use." if dup else "The evidence is thin; no independent corroboration is recorded.")
    return {"answer": ans, "evidence_ids": [e["id"] for e in ev if e["lineage_root"] in dup][:2],
            "confidence_basis": "lineage analysis of recorded evidence (scripted demo)"}


def strategist(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    results = [r for r in _tool_results(req) if r.get("name") == "read_source" and r.get("ok")]
    sources = packet.get("workspace", [{}])[0].get("sources", [])
    if not _tool_results(req) and sources:
        return {"__tool_calls__": [{"id": f"rs{i}", "name": "read_source", "arguments": {"path": p}}
                                   for i, p in enumerate(sources) if p.endswith(".md")]}
    goals = packet["goal"].get("standing_goals", [])
    ev = [r["result"] for r in results]
    cites = " ".join(f"[E:{e['evidence_id']}]" for e in ev[:2])
    objectives = []
    for g in goals:
        objectives.append({
            "title": f"Close the evidence gap for: {g['text'][:60]}",
            "objective": f"Gather independent evidence and a costed option relevant to '{g['text']}'.",
            "success_conditions": ["Research independent sources for the open question",
                                   "Write a one-page options summary with costs"],
            "serves_goal": g["id"],
            "rationale": "Recorded sources rest on one survey lineage; an independent source would change the decision basis.",
            "evidence_ids": [e["evidence_id"] for e in ev[:2]], "est_cost_usd": 0.0})
    body = ["## Discovery findings", "",
            f"Reviewed {len(ev)} source(s). Current demand evidence shares a single origin {cites}.",
            "", "## Proposed objectives", ""] + [f"- {o['title']} (serves {o['serves_goal']})" for o in objectives]
    return {"content_markdown": "\n".join(body), "summary": f"{len(objectives)} objective(s) proposed (scripted demo)",
            "objectives": objectives}


def operator(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    # Prefer services the owner connected explicitly over the generic built-in web connector.
    svc = sorted((t for t in req.tools if t.name.startswith("svc__")), key=lambda t: t.name.startswith("svc__web__"))
    done = [r for r in _tool_results(req) if (r.get("name") or "").startswith("svc__")]
    if not svc:
        return {"content_markdown": "No connected service is available to this task; no action was taken.",
                "summary": "no connector available"}
    if not done:
        target = next((t for t in svc if "external_write" in t.description), svc[0])
        args: dict[str, Any] = {}
        for k, spec in (target.input_schema.get("properties") or {}).items():
            if k == "rationale":
                continue
            if k == "url":
                owner_text = f"{packet['task']['description']} {packet['goal']['objective']}"
                m = re.search(r"https?://[^\s)\]]+", owner_text)
                args[k] = m.group(0) if m else "https://example.invalid/"
                continue
            if k == "method":
                args[k] = "POST"
                continue
            if k == "json":
                args[k] = {"text": f"NEXUS: {packet['task']['title']}"}
                continue
            if k in target.input_schema.get("required", []):
                args[k] = 1 if spec.get("type") == "integer" else (
                    f"{packet['task']['title']}" if k in ("title", "subject") else
                    f"NEXUS: {packet['task']['description']} (based on accepted mission results)")
        args["rationale"] = f"Task '{packet['task']['title']}' requires this action; inputs are accepted mission results."
        return {"__tool_calls__": [{"id": "a1", "name": target.name, "arguments": args}]}
    lines = ["## Actions", ""]
    for r in done:
        res = r.get("result") or {}
        lines.append(f"- {r['name']}: {res.get('status', 'error')} (request {res.get('action_request_id')})"
                     + (f" — {res.get('note')}" if res.get("note") else "") + (f" — error: {r.get('error')}" if not r.get("ok") else ""))
    return {"content_markdown": "\n".join(lines), "summary": f"{len(done)} action request(s) made"}


def guardian(req: CompletionRequest, packet: dict[str, Any]) -> dict[str, Any]:
    """DEMO heuristic standing in for a model judgment: misaligned if the action data carries
    injection-like text or a rationale deferring to a source; otherwise aligned."""
    from ..guard.injection import scan

    data = (packet.get("inputs") or [{}])[0].get("content", "")
    try:
        action = json.loads(data)
    except json.JSONDecodeError:
        return {"verdict": "unsure", "reason": "unreadable action"}
    rationale = str(action.get("agent_rationale", "")).lower()
    if scan(json.dumps(action.get("arguments"))).get("flagged") or any(
            p in rationale for p in ("page said", "instructed", "the document asks", "as requested by the source")):
        return {"verdict": "misaligned", "reason": "action appears to follow instructions from content, not the owner"}
    return {"verdict": "aligned", "reason": "action matches the task (scripted demo judgment)"}


def demo_script(req: CompletionRequest, packet: dict[str, Any]):
    role = req.metadata.get("role")
    purpose = req.metadata.get("purpose", "")
    if purpose.endswith(":repair"):
        # A repair request after malformed output: answer with a valid minimal object.
        return {"content_markdown": "(repaired output)", "summary": "repaired"}
    if purpose == "consult":
        return consult_answer(req, packet)
    if purpose == "guard" or role == "guardian":
        return guardian(req, packet)
    if purpose == "review" or role == "reviewer":
        return review(req, packet)
    if role == "strategist":
        return strategist(req, packet)
    if role == "operator":
        return operator(req, packet)
    if role == "planner":
        return plan(packet)
    if role == "researcher":
        return research(req, packet)
    if role == "engineer":
        return engineer(req, packet)
    if role == "integrator":
        return resolve_conflict(req, packet) if purpose == "resolve_conflict" else integrate_doc(req, packet)
    return write(req, packet)
