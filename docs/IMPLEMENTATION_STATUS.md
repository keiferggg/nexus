# Implementation status

Updated 2026-10-21. "Done" means implemented **and** exercised by a test listed in TEST_RESULTS.md.

| Area | Status | Notes |
|---|---|---|
| Stage 0 framework validation | Done | probe + ADR (DECISIONS D1) |
| Stage 1 vertical slice (desktop + service + mission + events + artifacts + acceptance + budget) | Done | UI e2e test 1 |
| Stage 2 durable collaboration (leases, fencing, worktrees, integration, cancellation, recovery) | Done | |
| Stage 3 shared understanding, invalidation, strategy switching, context compiler | Done | Magentic validated in probe but **not** wired as a strategy |
| Stage 4 learning, promotion/rollback, predictions, opportunity queue | Done (mechanism) | no claim of general improvement |
| Real actions via connectors (HTTP, GitHub, MCP) with approvals | Done | live third-party services untested |
| Objective discovery tied to standing goals | Done | |
| Strategy proposals by agents (bounded) | Done | |
| Agent-created tools (composite, Python) | Done; Python path only tested as "sandbox unavailable" | |
| Collaboration: consult, delegate | Done | |
| Internet for every agent: 17 web tools + 9 utilities, web writes, email | Done | 13 of 15 live read checks pass (arXiv throttled, Wikipedia needs a contact); live writes untested |
| Owner internet policy (mode, write autonomy, guards, search provider) + UI | Done | |
| Unattended mode with steering guard (scope provenance, exposure taint, payload, limits, Sentinel, circuit breaker, output drift check, digest) | Done | Sentinel tested with a deterministic stand-in only |
| Stage 5 packaging: standalone service + NSIS installer | Done | packaged app tested; installer unsigned; clean-machine install not run |
| Real Windows Credential Manager | Done | opt-in test passes |
| Live-mode path over Anthropic/OpenAI/Ollama wire formats + in-app capability check | Done | against protocol simulators; a real model has not been run |
| Onboarding: internet, autonomy, Docker guidance | Done | |
| Evaluation pack (`evals/`) | Done (harness) | only offline/scripted results so far; live comparison needs a key |
| Live provider smoke tests | Pending credentials and allowance | |
| Sandbox-executed checks | Pending Docker engine | |

## Next steps
1. With an API key: run *Check capabilities*, a small live mission, and `evals/run_evals.py` live.
2. Code signing for the installer (needs a certificate) and a clean-machine install test.
3. Start Docker Desktop and `docker pull python:3.12-slim` to exercise sandboxed tests and Python tools.
4. Wire Magentic as an optional `collaborative` planning strategy.
