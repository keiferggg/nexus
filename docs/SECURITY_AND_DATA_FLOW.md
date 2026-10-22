# Security and data flow

## Trust boundaries

| Boundary | What crosses it | Control |
|---|---|---|
| Renderer ↔ Electron main | validated IPC calls (`/api/*` paths, GET/POST/PUT/DELETE) and events | context isolation, sandboxed renderer, no Node in renderer, CSP, navigation and window-open blocked, all permission requests denied |
| Main ↔ local service | HTTP on `127.0.0.1:<random port>` | 32-byte bearer token passed to the service **only via its environment**; host and Origin validation; the renderer never sees the token |
| Service ↔ model providers | context packets (see below) | optional; only in **live** missions; spend cap required; per-call budget reservation; explicit data-exposure acknowledgment before the first live mission |
| Service ↔ connected services | owner-declared actions with agent-filled arguments | per-action autonomy (deny / ask / pre-approved); destructive actions never pre-approved; idempotency keys; ambiguous outcomes are reconciled, never blindly retried |
| Service ↔ sandbox | the task worktree, mounted read-only | Docker: no network, read-only root, CPU/memory/pids limits, timeout; the Docker socket is never given to agents; no Docker → code is not run |
| Agents ↔ everything | tool calls | the tool broker: capability checks per agent version, workspace path confinement, credential-like files refused, domain allowlist for web fetches, unknown tools denied and recorded |

## What agents can do, and who decides

| Capability | Agent can | Requires |
|---|---|---|
| Read workspace files / sources | yes, inside the project folder | capability in agent version |
| Fetch web pages | allowlisted domains only | owner allowlist per project |
| Real actions (HTTP, GitHub, MCP) | **request** an action with a rationale | the owner's per-action autonomy: `ask` (default for writes) waits for approval; `auto` runs within a daily quota; `deny` (default for destructive) refuses. NEXUS executes, not the agent |
| Propose objectives | yes, must name an owner standing goal | owner adoption, or the opt-in objectives policy (goal list + cost cap) |
| Propose strategies | yes, for tasks not yet running | applied only within existing bounds; otherwise waits for the owner |
| Create tools | composite (pipelines of existing tools) or Python | tests must pass; owner activation (opt-in auto-activation only for read-only composites). Composites run every step under the caller's permissions, so they cannot escalate. Python runs only in the sandbox |
| Consult / delegate | ask another role; create depth-limited subtasks | budgeted; reviewers cannot be consulted during authoring (keeps review independent); delegation depth ≤ 2, ≤ 24 tasks/mission |
| Accept work, grant permissions, change budgets or policies | **never** | no tool exists for these; attempts are denied and recorded |

## Data that leaves the machine (live missions only)

Sent to the configured model provider for each call: the role contract, mission objective,
success conditions, constraints, the task and its acceptance criteria, contents of files and
sources the agent read through tools, accepted upstream outputs, the evidence index, and permitted
memory. The exact packet is stored as a `context_packet` artifact and viewable in the timeline.

Never sent: API keys or other credentials, files outside the project workspace, other projects'
private memory. Ollama on `localhost` keeps data on the machine; a remote Ollama host is shown as
"sends data off-machine".

Real actions send exactly the arguments shown in the approval card (plus an idempotency key) to
the connected service.

## Credentials

Provider keys and connector secrets are stored in the OS credential store (`keyring`; Windows
Credential Manager on Windows). Database rows hold opaque references (`secret:<uuid>`). Keys are
read in-process only when making a call, placed in request headers, and never appear in URLs,
command lines, logs, prompts, API responses or backups. Tests confirm this for backups, provider
API responses and action records.

`NEXUS_SECRET_BACKEND=memory` selects a non-persistent in-memory store; it exists for tests and
is reported as such in Diagnostics.

## Prompt injection

Retrieved content is marked as untrusted in every packet, but enforcement does not depend on the
model: a test uses a deliberately compromised fake agent that obeys an injected document and
tries to grant itself permissions, raise the budget, read `../../secrets/.env`, exfiltrate via
HTTP and accept its own task. All five are denied by the broker and recorded.

## Known limitations

- MCP servers launched over stdio run as local processes with the owner's privileges; only add
  servers you trust. NEXUS controls *which tools* agents may call, not what a server process does.
- The model-call input estimate (characters/3) is a heuristic; reservations can be under- or
  over-estimated, which is why unreported usage stays as an unresolved reservation.
- Live-provider calls and live connector services (real GitHub, a real Slack-style webhook)
  have not been exercised; adapters are tested against recorded-shape fakes and local servers.

## Internet access (every agent)

All agents have the `internet` and `utilities` tool bundles (26 tools) and can request real
actions. Per project the owner chooses: **read_write** (default), **read** or **off** (legacy
allowlist only), and whether web writes **ask** (default) or are **pre-approved** (daily quota,
warning shown). Deletes always ask.

Guards that stay on in every mode (they protect the owner's machine and accounts):
- **Public addresses only.** Hostnames are resolved and loopback, RFC1918/ULA, link-local,
  carrier-grade NAT, cloud-metadata (169.254.169.254), multicast and reserved addresses are refused,
  on every redirect hop. The owner can allowlist specific LAN hosts. This stops a prompt-injected
  page from steering an agent at the NEXUS service itself, a router or other local services.
- **Connection pinning.** Each request connects to the exact vetted IP, with the Host header and TLS
  SNI/certificate verification bound to the real hostname (defeats DNS rebinding). Verified live
  against real HTTPS sites.
- **No agent-supplied credentials.** Authorization, Cookie, Proxy-Authorization and X-API-Key headers
  are stripped. Authenticated services go through connectors whose secrets live in the credential
  store. Owner search-API keys are never forwarded to another host on redirect.
- Size (8 MB pages, 25 MB downloads), time and per-project rate limits; http(s) only; downloads are
  stored, never executed.
- Every page read becomes an immutable artifact and citable evidence with a URL lineage (tracking
  parameters ignored), so independence checks and withdrawal work for web sources.

**Residual risk.** Open internet access greatly enlarges the prompt-injection surface: any page can
try to instruct an agent. NEXUS's enforcement does not depend on the model obeying, but with
pre-approved writes an injected agent *can* send data to an arbitrary public URL, up to the daily
quota. Prefer "ask" for writes unless you need unattended operation. Email is sent only through an
SMTP account the owner configures, optionally restricted to recipient domains.

## Unattended mode: acting without approvals, without being steered

Per project, **Autonomy** is *supervised* (writes ask the owner) or *unattended*. In unattended mode
no human approves individual actions; instead every write passes `nexus/guard/action_guard.py`:

| Check | What it stops | Can content an agent read satisfy it? |
|---|---|---|
| **destination_in_scope**: hosts and emails must appear in owner-authored text (mission objective, success conditions, constraints, owner messages, per-mission destinations) or the project's unattended allowlist, or be an owner-configured connector | exfiltration or actions toward a URL/address injected by a page | **No.** Only owner-authored records are read |
| **not_exposed**: if the attempt read text addressed to AI agents (heuristic scan of every page, PDF, file, API and connector result), its writes fall back to owner approval | a steered agent acting on in-scope destinations | No (it only makes things stricter) |
| **payload_clean**: no credential-like strings, at most 64 KB | leaking secrets an agent found | No |
| **within_limits**: per-mission and per-hour caps | runaway loops | No |
| **monitor**: Sentinel, an agent with no tools that never sees untrusted content, judges the action against the mission | in-scope but off-purpose actions | It sees the action's arguments as quoted data; it is model judgment, used as an additional layer |
| **destructive**: always escalates | irreversible damage | No |

**Circuit breaker:** after N denied actions (default 3) in one attempt, the attempt is fenced and the
task is **suspended** (blocked) for owner review; other tasks continue.
**Output drift:** in unattended projects, document tasks get a `no_injected_instructions` check, and
reviewers are told to fail work that drifts from the objective.
**Visibility:** the overview shows a *While you were away* digest of actions taken, blocked,
waiting, suspended tasks and flagged sources. Every guard decision is stored with its checks.

**Limits of this protection.** It prevents an injected page from choosing *where* data goes or what
gets deleted, and catches copied injection text. It cannot guarantee that an agent's *content*
(what it writes to an owner-approved destination) is free of subtle manipulation; verification,
lineage checks and reviewers reduce but do not eliminate that. Injection detection is heuristic
(it can miss attacks and can flag benign text about prompt injection). If the monitor is
unavailable, unattended writes escalate rather than proceed.
