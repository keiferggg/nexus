# NEXUS

A local-first desktop workbench where persistent AI agents plan, research, write, change code,
take real actions through services you connect, discover objectives that serve your goals, and build
new tools — while NEXUS (application code, not the models) verifies every result, enforces budgets and
permissions, and keeps durable, inspectable state.

Status: see `docs/IMPLEMENTATION_STATUS.md` and `docs/TEST_RESULTS.md`.

## Install (Windows)

Run the newest `apps/desktop/installer/<build>/NEXUS-Setup-0.1.0.exe` (`installer/LATEST.txt` names it). It installs per user (no admin) and bundles the
service, so no Python is needed. The installer is **unsigned**: Windows SmartScreen will show
"Windows protected your PC", so choose *More info → Run anyway*. Git is needed only for coding
missions, and Docker Desktop only for running code.

Rebuild it with `cd apps/desktop && npm run dist:win`. Each build gets the next version (0.2.N) and is
published to the update feed (`apps/desktop/installer/latest.json`).

**Updates:** the installed app checks the feed at startup and every 30 minutes. When a newer build exists
it shows "NEXUS 0.2.N is ready": click **Restart to update**, or just close NEXUS and it installs on exit.
The installer's SHA-256 is verified first. Your data (`%APPDATA%/NEXUS`), keys (Windows Credential Manager)
and missions are kept; interrupted missions resume. Settings → Diagnostics → Updates shows the
version and lets you check now or turn off install-on-close. The feed is currently a folder on this
machine; to update other machines, point it at an https URL (e.g. GitHub Releases).

## Run from source (Windows)

Prerequisites: Python 3.11–3.13, Node 22, git. Docker Desktop is optional (code-execution sandbox).

```
python -m venv services/.venv
services/.venv/Scripts/python -m pip install -r services/nexus/requirements.lock
services/.venv/Scripts/python -m pip install --no-deps -e services/nexus
cd apps/desktop
npm ci
npm run start
```

The desktop app starts the local service itself (loopback only, random port, per-launch token).

## First mission (offline, no API key)
Setup → Workspace → **Create research demo** → **New mission** → *Create and start mission*. The
deterministic fake provider plays every role; outputs are scripted and labeled **DEMO**.

## Real providers
Setup → Providers: add Anthropic, OpenAI (or any OpenAI-compatible server via Base URL, e.g. LM Studio
or vLLM; DeepSeek has a preset) or Ollama, then *Save and test* (lists models, no spend). Run **Check capabilities** on the
model: 3 tiny calls confirm JSON output, tool calling and usage reporting. Choose it as the default
model, enter its price (provider APIs do not report prices), then create a **Live** mission with a small
spend cap. API billing is separate from consumer chat subscriptions.

## Internet access
Every agent can search and read the public web and call public APIs (26 built-in tools). Writes to
websites and email go through approvals by default; change this under Connected services → Internet
access. Local-network and cloud-metadata addresses are always blocked unless you allowlist a host.

## Unattended mode
Connected services → Autonomy → Unattended: agents act without asking, but can only write to
destinations you supplied (in the mission text or allowlists). Pages they read cannot redirect them;
see `docs/SECURITY_AND_DATA_FLOW.md`. Overview shows what happened while you were away.

## Connected services
Connected services → connect an HTTP API (you declare its actions), a GitHub repository, or an MCP
server. Writes wait for your approval unless you pre-approve them; destructive actions always ask.
A demo MCP server is in `fixtures/mcp-demo/notes_server.py`.

## Tests
```
services/.venv/Scripts/python -m pytest -q
cd apps/desktop && npm run build && npx playwright test
services/.venv/Scripts/python probes/af_compat_probe.py all probes/out
NEXUS_LIVE_WEB=1 services/.venv/Scripts/python -m pytest -q tests/service/test_live_web.py   # real internet, read-only
NEXUS_PACKAGED=1 npx playwright test tests/packaged.spec.ts          # in apps/desktop, after npm run dist:win
services/.venv/Scripts/python evals/run_evals.py --offline           # evaluation harness (see evals/README.md)
```

## Troubleshooting
- *Local service unavailable* banner: **Show log** / **Open log folder**; the log is under
  `%APPDATA%/nexus-desktop/logs/service.log`. "Another NEXUS service is already using…" means a second
  instance holds the data folder.
- *Live models* disabled: add and test a provider, then choose a default model in Settings.
- Coding tasks waiting for *owner approval*: the Docker sandbox is unavailable, so behavior checks
  became explicit approvals (code is never run unsandboxed).

Docs: `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/SECURITY_AND_DATA_FLOW.md`, `docs/USER_GUIDE.md`.
