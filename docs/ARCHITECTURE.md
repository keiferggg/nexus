# Architecture

```
apps/desktop      Electron main (service lifecycle, IPC proxy, SSE relay) + React workbench
services/nexus    Python service (FastAPI on 127.0.0.1)
  nexus/domain          tasks (state machine), execution (leases, fencing, submissions, acceptance),
                        missions/revisions, budget, understanding (evidence, claims, invalidation),
                        verification checks, rooms (durable messages)
  nexus/orchestration   adapter (the only framework import), worker, verifier, supervisor, strategy, roles
  nexus/providers       Anthropic / OpenAI / Ollama adapters, deterministic fake, gateway (budget + usage)
  nexus/tools           broker (policy), built-in tools, git worktrees, Docker sandbox
  nexus/connectors      HTTP / GitHub / MCP adapters, action requests and approvals
  nexus/autonomy        objectives, tool creation (toolsmith), consult / delegate / strategy proposals
  nexus/memory          scoped memory + FTS, context compiler
  nexus/learning        skill candidates + evaluation, opportunity queue
  nexus/persistence     SQLite, migrations, event log/outbox, content-addressed artifacts
fixtures/         fictional demo data (labeled), demo MCP server
probes/           Stage 0 framework compatibility probe
tests/service     pytest (service, real processes)      apps/desktop/tests  Playwright (Electron)
```

## State ownership

| Owner | State |
|---|---|
| NEXUS SQLite | projects, agent identities and versions, missions and revisions, tasks, dependencies, leases, attempts, submissions, reviews, acceptances, evidence, claims, decisions, objections, predictions, budgets, model calls, tool operations, connectors, action requests, objective proposals, tool definitions, skills, opportunities, events |
| Framework checkpoint store (`data/checkpoints`) | in-flight workflow state per task attempt |
| Artifact store (`data/artifacts`) | immutable files addressed by SHA-256 |
| OS credential store | provider keys and connector secrets |
| Rebuildable projections | `memory_fts`, read models in `api/views.py` |

## Task lifecycle

`draft → ready → leased → running → submitted → verifying → accepted`, plus `blocked`, `failed`,
`canceled`, `superseded`. The full table of legal transitions, allowed actors and preconditions is
`nexus/domain/tasks.py::TRANSITIONS` (also served at `GET /api/meta/transitions`). Agents can perform
no transition. Staleness is separate (`is_stale`, `epoch`): an accepted result can remain historically
accepted but non-current.

## Acceptance

A submission is recorded idempotently under the attempt's fencing token. The verifier runs each
criterion of the task's verification plan (deterministic checks, sandbox, independent model reviewer,
owner approval, actions settled, objectives proposed) and stores raw results as artifacts.
`decide()` accepts only if every required criterion passed and no blocking objection is open; it
checks the fencing token, task epoch and mission revision, and `UNIQUE(task_id, epoch)` prevents
double acceptance. Failures request repair; inconclusive results block the task explicitly.
When the inputs of an inconclusive criterion change (owner approval, an action settling),
`reverify()` re-applies the policy to the same submission; a repair at that point schedules a new attempt.

## Checkpoint reconciliation (framework store and SQLite are not one transaction)

1. Attempt id + fencing token are recorded before work starts.
2. Every model/tool step uses a deterministic operation id; results are recorded and reused on replay.
3. After each workflow segment the latest checkpoint id is recorded.
4. Submissions are idempotent by `attempt:round`; acceptance is one transaction.
5. On startup, running attempts are fenced; unverified submissions become stale; in-flight model
   calls and external writes become *uncertain* with unresolved reservations; the task is re-queued
   pointing at the attempt's latest checkpoint (discovered from the store if never recorded).
6. A new attempt resumes the suspended workflow; the pending submission is re-emitted and re-submitted.
7. An already-accepted task is never resumed.

Fault tests crash before the submission record, between record and verification, and between the
acceptance commit and the workflow response, both in-process and by killing the real service process.

## Real actions

```
agent tool call svc__<connector>__<action>(args, rationale)
  -> ConnectorService.request: validate args, apply autonomy (deny / ask / auto+quota)
  -> pending (attention item)  --owner approve-->  executing (claimed once)
  -> adapter.execute(idempotency key)  -> succeeded | failed | uncertain
  -> result artifact + observation evidence + room message
  -> task re-verified (actions_settled)
uncertain -> reconcile (ask the destination) or owner resolves; never retried blindly
```

## Events

Every state change writes an `events` row in the same transaction (transactional outbox). The SSE
stream reads by `seq` cursor and supports `Last-Event-ID`; the Electron main process holds the one
stream and relays it to the renderer.
