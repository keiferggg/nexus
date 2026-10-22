# Evaluation pack

`run_evals.py` answers "does the multi-agent machinery help?" by running the same tasks in several
modes and scoring every final deliverable with the same deterministic scorer:

| mode | what runs |
|---|---|
| `single` | one agent, one task, no independent review, one round (strong single-agent baseline) |
| `pwr` | planner, workers, reviewer, without lineage-independence or quote checks |
| `nexus` | full NEXUS |
| `nexus-no_reviewer`, `nexus-no_lineage`, `nexus-no_quote_check` | full NEXUS minus one module |

The scorer covers required facts, independent source lineages, quotes checked against the actual
sources, and forbidden content (for example an injected instruction, or a withdrawn source after
an evidence update). It also records spend, model calls, time, owner interventions and recovery
after evidence withdrawal. Failed runs are kept; use `--repeats` to measure variation.

```
services/.venv/Scripts/python evals/run_evals.py --offline --repeats 2                  # plumbing check
set NEXUS_EVAL_API_KEY=<your key>   (environment only, never stored)
services/.venv/Scripts/python evals/run_evals.py --provider anthropic --model MODEL --price-in IN --price-out OUT --cap 2 --repeats 3
```

Offline results come from the scripted fake provider. They show that the harness and NEXUS's
checks work, **not** that the swarm is better. Only live runs with a real model measure that.
Results are written to `evals/results/` (JSON plus a Markdown table). Tasks are in `evals/tasks/`.
