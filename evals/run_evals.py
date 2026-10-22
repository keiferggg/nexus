"""NEXUS evaluation pack: does the swarm help?

Compares, on the same tasks, inputs, tools and spend cap:
  single      one agent, one task, no independent review, one round (strong single-agent baseline)
  pwr         planner -> workers -> reviewer, without lineage-independence or quote checks
  nexus       full NEXUS (verification, lineage, strategy, collaboration)
  ablations   nexus minus one module: no_reviewer, no_lineage, no_quote_check

Every run's final deliverable is scored post hoc by the SAME deterministic scorer (required facts,
independent source lineages, verified quotations), so modes are compared on outcomes, not on
their own self-reports. Failed runs are kept. Repeats give variation.

  python evals/run_evals.py --offline --repeats 1            # plumbing check (scripted fake: NOT a quality measure)
  set NEXUS_EVAL_API_KEY=...   (read from the environment, never stored)
  python evals/run_evals.py --provider anthropic --model MODEL --price-in 3 --price-out 15 --cap 2 --repeats 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "services" / "nexus"))
os.environ.setdefault("NEXUS_SECRET_BACKEND", "memory")

from nexus.appctx import build_context  # noqa: E402
from nexus.bootstrap import ensure_fake_provider, fixtures_dir  # noqa: E402
from nexus.domain.budget import summary  # noqa: E402
from nexus.domain.missions import create_mission, create_mission_with_tasks, create_project, start_mission  # noqa: E402
from nexus.domain.understanding import change_evidence, lineage_summary  # noqa: E402
from nexus.domain.verification import CITE_RE, QUOTE_RE  # noqa: E402
from nexus.orchestration.supervisor import Supervisor  # noqa: E402
from nexus.persistence.db import dumps  # noqa: E402
from nexus.tools.sandbox import UnavailableSandbox  # noqa: E402

MODES = {
    "single": {"single": True},
    "pwr": {"flags": {"no_lineage": True, "no_quote_check": True}},
    "nexus": {"flags": {}},
    "nexus-no_reviewer": {"flags": {"no_reviewer": True}},
    "nexus-no_lineage": {"flags": {"no_lineage": True}},
    "nexus-no_quote_check": {"flags": {"no_quote_check": True}},
}


def load_tasks() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((REPO / "evals" / "tasks").glob("*.json"))]


def score(c, mid: str, deliverable: str, task: dict) -> dict:
    s = task["scoring"]
    low = deliverable.lower()
    facts = [f for f in s.get("required_facts", []) if f.lower() in low]
    cited = sorted(set(CITE_RE.findall(deliverable)))
    with c.db.tx() as cur:
        lin = lineage_summary(cur, cited)
    quotes = QUOTE_RE.findall(deliverable)
    ok_quotes = 0
    for q, eid in quotes:
        e = c.db.one("SELECT artifact_id FROM evidence WHERE id = ?", (eid,))
        if e and e["artifact_id"] and " ".join(q.split()).lower() in " ".join(c.artifacts.read_text(e["artifact_id"]).split()).lower():
            ok_quotes += 1
    forbidden = [f for f in s.get("forbidden", []) if f.lower() in low]
    checks = {
        "facts": len(facts) / max(1, len(s.get("required_facts", []))),
        "independent_sources": min(1.0, lin["independent_lineages"] / max(1, s.get("min_independent_sources", 1))),
        "quote_accuracy": (ok_quotes / len(quotes)) if quotes else (0.0 if s.get("require_quotes") else 1.0),
        "no_forbidden": 0.0 if forbidden else 1.0,
    }
    return {"score": round(sum(checks.values()) / len(checks), 3), "checks": checks, "cited_lineages": lin["lineages"],
            "forbidden_found": forbidden}


async def run_one(task: dict, mode: str, args, workdir: Path) -> dict:
    cfg = MODES[mode]
    c = build_context(workdir / "data", sandbox=UnavailableSandbox("evals"))
    c.heartbeat_s, c.lease_ttl_ms = 0.5, 30_000
    if args.offline:
        ensure_fake_provider(c)
    ws = workdir / "ws"
    shutil.copytree(fixtures_dir() / task["workspace"], ws)
    with c.db.tx() as cur:
        pid = create_project(cur, name=f"eval {task['id']}", workspace_root=str(ws))
    if not args.offline:
        prov = c.registry.add(kind=args.provider, label=f"eval {args.provider}", base_url=args.base_url,
                              api_key=os.environ.get("NEXUS_EVAL_API_KEY") or None)
        if args.provider != "ollama":
            c.registry.set_price(prov, args.model, args.price_in, args.price_out, "eval run (owner-supplied)")
        with c.db.tx() as cur:
            cur.execute("INSERT OR REPLACE INTO settings VALUES ('default_model_policy', ?, '')",
                        (dumps({"provider_id": prov, "model": args.model, "max_output_tokens": 2048}),))
    sup = Supervisor(c)
    await sup.start()
    sup._loop_task.cancel()
    mode_exec = "offline_fake" if args.offline else "live"
    t0 = time.monotonic()
    status, mid, err = "error", None, None
    try:
        with c.db.tx() as cur:
            if cfg.get("single"):
                mid = create_mission_with_tasks(cur, project_id=pid, title=task["id"], objective=task["objective"],
                                                success_conditions=[task["objective"]], constraints=task.get("constraints", []),
                                                execution_mode=mode_exec, spend_cap_usd=None if args.offline else args.cap,
                                                tasks=[{"key": "solo", "title": task["objective"], "kind": "write",
                                                        "description": task["objective"] + " Research the workspace sources yourself.",
                                                        "addresses": "SC1",
                                                        "acceptance": [{"id": "A1", "text": "non-empty", "check": {"kind": "min_words", "args": {"min": 20}}}]}],
                                                purpose="evaluation")
                cur.execute("UPDATE missions SET max_revision_rounds = 1 WHERE id = ?", (mid,))
                cur.execute("UPDATE agent_versions SET tool_permissions_json = json_insert(tool_permissions_json, '$[#]',"
                            " 'read_source') WHERE id IN (SELECT current_version_id FROM agent_identities WHERE project_id = ?)", (pid,))
            else:
                mid = create_mission(cur, project_id=pid, title=task["id"], objective=task["objective"],
                                     success_conditions=task["success_conditions"], constraints=task.get("constraints", []),
                                     execution_mode=mode_exec, spend_cap_usd=None if args.offline else args.cap)
                cur.execute("INSERT INTO settings VALUES (?, ?, '')", (f"mission:{mid}:flags", dumps(cfg["flags"])))
            start_mission(cur, mid, actor="nexus:evals")
        status = await sup.wait_idle(mid, timeout=args.timeout)
        recovery = None
        if task.get("evidence_update") and status == "completed":
            loc = task["evidence_update"]["withdraw_locator"]
            ev = c.db.one("SELECT id FROM evidence WHERE mission_id = ? AND locator = ?", (mid, loc))
            if ev:
                from nexus.domain.missions import set_mission_status

                with c.db.tx() as cur:
                    res = change_evidence(cur, ev["id"], new_status="withdrawn", reason="eval: source retracted", actor="owner")
                    if res["tasks"]:
                        set_mission_status(cur, mid, "running", actor="owner", reason="eval recovery", allowed_from={"completed"})
                status2 = await sup.wait_idle(mid, timeout=args.timeout)
                recovery = {"reopened_tasks": len(res["tasks"]), "status_after": status2}
                status = status2
    except Exception as exc:  # keep failed runs
        err = f"{type(exc).__name__}: {exc}"[:400]
    elapsed = time.monotonic() - t0
    deliverable = ""
    if mid:
        row = c.db.one("SELECT t.current_artifact_id FROM tasks t WHERE t.mission_id = ? AND t.status = 'accepted' AND t.is_stale = 0"
                       " ORDER BY CASE WHEN t.kind = 'integrate' THEN 0 ELSE 1 END, t.updated_at DESC LIMIT 1", (mid,))
        if row and row["current_artifact_id"]:
            deliverable = c.artifacts.read_text(row["current_artifact_id"])
    sc = score(c, mid, deliverable, task) if deliverable else {"score": 0.0, "checks": {}, "note": "no accepted deliverable"}
    with c.db.tx() as cur:
        spend = summary(cur, mid) if mid else {}
    owner_actions = c.db.one("SELECT COUNT(*) AS n FROM events WHERE mission_id = ? AND actor = 'owner'", (mid,))["n"] if mid else 0
    calls = c.db.one("SELECT COUNT(*) AS n FROM model_calls WHERE mission_id = ?", (mid,))["n"] if mid else 0
    await sup.stop()
    c.close()
    accepted = status == "completed"
    return {"task": task["id"], "mode": mode, "status": status, "accepted": accepted, "error": err, "elapsed_s": round(elapsed, 2),
            "model_calls": calls, "spend_usd": round(spend.get("committed_usd", 0.0), 6), "owner_interventions": owner_actions,
            "recovery": recovery, **sc}


def summarize(runs: list[dict]) -> list[dict]:
    rows = []
    for mode in MODES:
        rs = [r for r in runs if r["mode"] == mode]
        if not rs:
            continue
        scores = [r["score"] for r in rs]
        acc = [r for r in rs if r["accepted"]]
        spend = sum(r["spend_usd"] for r in rs)
        rows.append({"mode": mode, "runs": len(rs), "accepted": len(acc), "mean_score": round(statistics.mean(scores), 3),
                     "stdev_score": round(statistics.pstdev(scores), 3) if len(scores) > 1 else 0.0,
                     "usd_per_accepted": round(spend / len(acc), 6) if acc else None,
                     "mean_elapsed_s": round(statistics.mean(r["elapsed_s"] for r in rs), 2),
                     "mean_model_calls": round(statistics.mean(r["model_calls"] for r in rs), 1)})
    return rows


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="scripted fake provider (plumbing check only)")
    ap.add_argument("--provider", choices=["anthropic", "openai", "ollama"])
    ap.add_argument("--base-url", default=None, help="for OpenAI-compatible servers or a remote Ollama")
    ap.add_argument("--model")
    ap.add_argument("--price-in", type=float, default=None, help="USD per million input tokens (from the provider's pricing page)")
    ap.add_argument("--price-out", type=float, default=None, help="USD per million output tokens")
    ap.add_argument("--cap", type=float, default=2.0, help="spend cap per run (USD)")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args()
    if not args.offline:
        if not (args.provider and args.model):
            ap.error("live evaluation needs --provider and --model")
        if args.provider != "ollama" and (args.price_in is None or args.price_out is None or not os.environ.get("NEXUS_EVAL_API_KEY")):
            ap.error("set NEXUS_EVAL_API_KEY and pass --price-in/--price-out (budget accounting needs real prices)")
    tasks = load_tasks()
    runs = []
    for task in tasks:
        for mode in args.modes.split(","):
            for rep in range(args.repeats):
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                    r = await run_one(task, mode, args, Path(td))
                r["repeat"] = rep
                runs.append(r)
                print(f"{task['id']:<22} {mode:<22} rep{rep} {r['status']:<10} score={r['score']:.3f} calls={r['model_calls']}"
                      f" ${r['spend_usd']:.4f} {r['elapsed_s']}s" + (f" ERROR {r['error']}" if r["error"] else ""))
    out_dir = REPO / "evals" / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = "offline-fake" if args.offline else f"live-{args.model}"
    table = summarize(runs)
    report = {"label": label, "created_at": stamp, "repeats": args.repeats, "summary": table, "runs": runs,
              "caveat": ("OFFLINE SCRIPTED FAKE: agent outputs are scripted, so differences reflect NEXUS's checks and "
                         "repair loop acting on scripted behavior, not model or swarm quality.") if args.offline else
              "Live run: results vary between runs; compare means with their spread, not single runs."}
    (out_dir / f"{stamp}-{label}.json").write_text(json.dumps(report, indent=2))
    md = [f"# Evaluation {stamp} ({label})", "", report["caveat"], "",
          "| mode | runs | accepted | mean score | stdev | $/accepted | mean s | mean calls |", "|---|---|---|---|---|---|---|---|"]
    md += [f"| {r['mode']} | {r['runs']} | {r['accepted']} | {r['mean_score']} | {r['stdev_score']} | {r['usd_per_accepted']} |"
           f" {r['mean_elapsed_s']} | {r['mean_model_calls']} |" for r in table]
    (out_dir / f"{stamp}-{label}.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
