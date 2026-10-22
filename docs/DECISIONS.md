# Decisions

## D1. Orchestration foundation: Microsoft Agent Framework (Python) behind one adapter

- **Pinned:** `agent-framework-core==1.22.0`, `agent-framework-orchestrations==1.3.2` (MIT, Python ≥3.10;
  run on CPython 3.13.2). Hashes in `services/nexus/requirements.lock`.
- **Evidence (executed):** `probes/af_compat_probe.py` with deterministic fake workers:
  plan → execute → review rejects → revise → review passes → suspend on `request_info`; a separate process
  rebuilds the workflow and resumes from a `FileCheckpointStorage` checkpoint with executor state restored;
  cancellation of a streaming run returns immediately; Magentic with a custom `MagenticManagerBase`
  plans, emits ledger events and completes; Magentic resumes mid-run from an intermediate checkpoint in a
  new process. Also verified: an executor exception propagates out of the stream unchanged, and restoring
  a checkpoint without responses re-emits the pending request with the same id (this is the recovery path).
- **Use:** each task attempt is a small checkpointed workflow `Execute → Gate`. The gate suspends with
  `request_info(SubmissionReady)`; NEXUS verifies and answers accept / revise / stop. Framework completion
  is never acceptance. Only `nexus/orchestration/adapter.py` imports the framework.
- **Risk:** checkpoint values are pickled (restricted unpickler). NEXUS keeps workflow payloads to
  registered dataclasses and stores checkpoints only in its own data folder.
- **LangGraph** was not needed: no probe failed.
- **Not yet done:** Magentic is validated but not wired as a NEXUS strategy mode.

## D2. Provider adapters are NEXUS-owned (httpx), not the framework connectors

The framework's Anthropic and Ollama connectors are published only as betas (`1.0.0b…`). NEXUS needs
exact control of usage accounting, error classification (auth / rate limit / timeout / server /
malformed / missing usage), budget reservation and key handling, so it calls the documented REST APIs
directly (Anthropic Messages, OpenAI Chat Completions, Ollama `/api/chat`). Model ids come from each
provider's live model list; none are hardcoded. Tested against recorded response shapes only.

## D3. State: SQLite (WAL) is canonical; checkpoints are the framework's

One serialized write path (`BEGIN IMMEDIATE` under a lock), foreign keys on, numbered migrations.
Migrations that rebuild a table run with foreign keys off and a `foreign_key_check` before commit
(an upgrade test confirms dependent rows survive). Reconciliation protocol: see ARCHITECTURE.md.

## D4. Real actions go through owner-configured connectors, executed by NEXUS

Agents *request* actions; NEXUS validates arguments, applies per-action autonomy, executes with an
idempotency key, and records the result as observation evidence. Connector kinds:
- **HTTP**: the owner declares every action (method + path); agents fill arguments only.
- **GitHub**: one repository; writes embed a hidden marker so an ambiguous create/comment is reconciled
  by searching for it.
- **MCP** (`mcp==2.3.0`, MIT): stdio or streamable HTTP. Effect class from tool annotations
  (`readOnlyHint` → read-only, `destructiveHint` → destructive, otherwise external write).

Defaults: reads pre-approved, writes ask, destructive denied; destructive can never be pre-approved.
Rationale: the owner asked for agents that take real actions; per-action approval with an opt-in
pre-approval keeps that useful without granting unbounded authority.

## D5. Objectives, strategies, tools and collaboration are bounded proposals

- Objectives must link to an owner **standing goal**; adoption is the owner's, or an opt-in policy
  (allowed goals + cost cap).
- Strategy proposals apply only within existing bounds (mode list, branch/worker limits).
- Agent-created tools: composite pipelines (no code; each step re-checked under the caller's
  permissions) or Python (sandbox only). Tests + owner activation before use.
- Consultation runs a budgeted model call under the consulted agent's identity; reviewers are not
  consultable during authoring; delegation is depth-limited.

## D6. Desktop: Electron 44.6.0 + React 19.3.0 + Vite 8.3.3, TypeScript 6.0.3

Versions chosen as the newest releases at least two weeks old. The main process owns the service
lifecycle and an IPC proxy; the renderer has no network or Node access.

## D7. Every agent gets open internet tools; writes stay owner-gated by default

Owner request: every agent can do anything on the internet. Implemented as 17 internet tools (search,
fetch any public URL, links, downloads, PDFs, JSON APIs, RSS, Wikipedia, arXiv, GitHub, Stack Exchange,
PyPI/npm, geocoding, weather, exchange rates, Wayback, DNS) plus 9 utilities, given to all roles
(existing agents upgraded once, as a new version). Writes to arbitrary URLs (POST/PUT/PATCH, forms,
DELETE) go through a built-in **Web** connector, and email through an **SMTP** connector, so they inherit
approval, quotas, idempotency keys, ambiguity handling and evidence recording.
Search providers: DuckDuckGo HTML (no key, fragile parsing), Brave, Tavily, SearXNG.
New dependencies: `pypdf==6.19.0` (BSD-3-Clause), `tzdata==2026.5` (Apache-2.0; Windows has no
system timezone database). The SSRF guards are not configurable off, by design.

## D8. Unattended operation is gated by owner-sourced scope, not by trusting the model

Owner request: run unattended without agents being steered. A prompt can be argued with; a set of
destinations taken only from owner-authored records cannot. So the primary control is
destination scope from trusted provenance, plus exposure tainting, payload and rate limits, a
circuit breaker, and an independent tool-less monitor (Sentinel) as a second layer. Destructive
actions are never unattended. New migration `0004_guard.sql` (exposures, guard_decisions).
