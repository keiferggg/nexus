# User guide

## First run
**Setup** walks you through: workspace folder (or an offline demo project), providers (API keys go
into the Windows credential store; "Save and test" lists models without spending), default model,
sandbox status, and spending defaults. Every live mission needs a spend cap.

## Checking a model
Settings → Providers → choose a model → **Check capabilities**. Three tiny real calls test that the
model returns structured JSON, calls a tool and uses its result, and reports token usage. Do this
before your first live mission with a new model.

## Missions
**New mission**: objective, measurable success conditions, constraints, execution mode.
*Offline demo* uses the deterministic fake provider (scripted, labeled DEMO). *Live models* needs a
tested provider, a default model, a price for that model, a spend cap, and your acknowledgment of
what data is sent.

Mission header: **Pause** (no new work), **Cancel** (stops scheduling, signals agents, keeps
uncertain charges visible), **Change requirements** (new revision; affected work is invalidated
explicitly, unaffected work continues), budget meter (actual / reserved / unresolved).

## Mission control
Success conditions with verified status; the plan with each agent's current activity and strategy;
**Needs your attention** (approvals, blocked tasks, objections, uncertain actions or charges). Click a
task for *why it is running*, its verification plan, every submission and review, attempts, model calls
and tool operations.

## Agent rooms
Threaded, typed messages (question, proposal, evidence, objection, review request, decision, blocker…).
You can message the agent on a task, or raise an **objection** (mechanism, evidence/test, resolution
condition). Blocking objections prevent acceptance until you resolve them.

## Shared understanding
Evidence grouped by **source lineage** (copies of one source count once), claims with their basis,
ranked uncertainties, assumptions, decisions, predictions vs outcomes, and changes since you last
looked. **Withdraw** a source to mark only its dependents stale and re-verify them.

## Artifacts and timeline
Immutable versions with SHA-256, lineage, input versions and acceptance status (current / historical),
with diffs. The timeline shows the causal event history and checkpoints; **Replay** shows recorded
outputs without calling any model; **New branch** (task drawer) re-runs a task live in a new epoch.

## Connected services and real actions
**Connected services** → *Connect a service*:
- *HTTP API*: declare each action (method, path, parameters). Agents can only fill parameters.
- *GitHub repository*: issues and comments for one repo (fine-grained token recommended).
- *MCP server*: a local command (stdio) or URL. Only add servers you trust.

Each discovered action has an **effect** and an **autonomy** setting: *deny*, *ask me each time*
(default for writes), *pre-approved* (with a daily quota; never available for destructive actions).
When an agent requests an action you see the exact arguments and its rationale; **Approve & run**
executes it once. If the outcome is unknown (e.g. timeout after sending), NEXUS will not retry:
use *Ask the service* or mark what happened.

## Internet access
Every agent can search the web, read any public page, PDF, JSON API or feed, download files, and use
public knowledge sources (Wikipedia, arXiv, GitHub, Stack Exchange, package registries, weather,
exchange rates, Wayback). Pages they read become citable evidence.
**Connected services → Internet access** sets, per project:
- *Mode*: read and write (default), read only, or off.
- *Web writes*: **ask me each time** (default) or **pre-approved** (no prompt, daily quota; a warning
  explains the risk). Deletes always ask.
- *Contact*: an email or URL for the User-Agent (Wikipedia requires one).
- *Blocked domains*, *allowed local-network hosts* (local addresses are blocked otherwise),
  requests per minute.
- *Search provider*: DuckDuckGo (no key), Brave, Tavily or your SearXNG.

To let agents send email, connect an **Email (SMTP)** service with your mail server and an app
password; you can restrict recipient domains.

## Running unattended
**Connected services → Autonomy → Unattended** lets agents act without asking you. They can still
only write to destinations **you** supplied: URLs and email addresses you wrote in the mission (or in
the *Unattended destinations* box on the new-mission form, or in a message), the always-trusted
lists, or services you connected. Anything else is blocked automatically and recorded. An agent
that read text trying to instruct AI agents loses unattended writes for that attempt (they wait
for you); an agent that keeps trying blocked actions gets its task suspended. Deletes always ask.
Sentinel, an independent agent that never reads web content, double-checks each write
(one small model call per write; you can turn it off, but it is recommended).
Check **Overview → While you were away** for what was done, blocked, waiting and suspended.

## Goals and objectives
Set **standing goals** (G1, G2…). **Run discovery** starts a mission where the strategist reviews
sources and read-only services and proposes objectives — each must name the goal it serves, with
success conditions and evidence. **Adopt** turns a proposal into a mission. Optional auto-adoption
is limited to goals and a cost cap you choose.

## Tool workshop
Agents can create tools: *composite* (a pipeline of existing tools; every step runs with the calling
agent's own permissions) or *Python* (runs only in the Docker sandbox). Each tool must pass its own
tests; you **Activate** it before agents can use it. External-effect steps are simulated in tests.

## Agent studio
Persistent agents with role, model, tools and memory scope; edits create a new version.
Performance counts are verified outcomes (accepted vs rejected submissions), not self-reports.

## Updates
NEXUS updates itself. When a new version is ready you see a blue banner: **Restart to update**, or
close NEXUS and it installs on exit. Updates never touch your data, keys or settings, and missions
that were running resume after the restart. Settings → Diagnostics → Updates: version, last check,
*Check for updates*, and the install-on-close switch.

## Recovery and backups
If the service stops, a red banner appears; the app restarts it automatically (up to 3 times) and
offers *Restart service* and the log. On restart NEXUS reconciles interrupted work (see Diagnostics).
**Settings → Backups** writes a zip of data and artifacts — never credentials. Restore with the
service stopped:

```
nexus-service restore --data-dir NEW_FOLDER --from backup.zip
```
