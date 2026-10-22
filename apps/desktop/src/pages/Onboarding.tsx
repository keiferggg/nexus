import { useEffect, useState } from "react";
import { bridge, post, put, useApi } from "../api";
import { AutonomyPanel } from "../components/Autonomy";
import { InternetPanel } from "../components/Internet";
import { DefaultModelPicker, ProvidersPanel } from "../components/Providers";
import { Badge, Button, Field, KV, Status } from "../components/ui";

const STEPS = ["Welcome", "Workspace", "Providers", "Models", "Sandbox", "Internet & autonomy", "Spending"] as const;

export default function Onboarding({ onDone }: { onDone: (projectId: string | null) => void }) {
  const [step, setStep] = useState(0);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [info, setInfo] = useState<any>(null);
  const health = useApi<any>("/api/health");
  const settings = useApi<any>("/api/settings");
  useEffect(() => { void bridge().appInfo().then(setInfo); }, []);

  return (
    <div className="page" style={{ maxWidth: 980 }}>
      <div className="page-head"><div className="titles"><h1>Set up NEXUS</h1>
        <div className="sub">Local-first: missions, agents, evidence and files stay on this computer. Cloud models are optional and use your own API credentials.</div></div></div>
      <nav className="steps" aria-label="Setup steps">
        {STEPS.map((s, i) => <button key={s} className={`step${i < step ? " done" : ""}`} aria-current={i === step ? "step" : undefined} onClick={() => setStep(i)}>{i + 1}. {s}</button>)}
      </nav>

      {step === 0 && (
        <section className="card"><div className="card-body col">
          <h2>How NEXUS works</h2>
          <p>You give a mission with measurable success conditions. A planner proposes tasks; specialist agents work in isolated contexts; every submission is checked by NEXUS — deterministic checks first, then an independent reviewer — before anything is accepted. You can pause, cancel, change requirements, and inspect the evidence for every result.</p>
          <p className="muted">Agents keep persistent identities and memory across restarts. Idle agents cost nothing: there is no background “thinking”.</p>
          {info && <KV items={[["Data folder", <span className="mono">{info.dataDir}</span>], ["Service", info.serviceKind], ["Version", info.version]]} />}
          <div><Button variant="primary" onClick={() => setStep(1)}>Continue</Button></div>
        </div></section>
      )}

      {step === 1 && <WorkspaceStep onCreated={(id) => { setProjectId(id); setStep(2); }} />}

      {step === 2 && (
        <div className="col gap-lg">
          <ProvidersPanel />
          <div className="row"><Button onClick={() => setStep(3)} variant="primary">Continue</Button>
            <span className="small muted">You can skip this and run offline demo missions only.</span></div>
        </div>
      )}

      {step === 3 && (
        <section className="card"><div className="card-head"><h3>Default model</h3></div><div className="card-body col">
          <DefaultModelPicker />
          <div className="small muted">Each agent can override this in Agent studio (for example a different model for reviewers).</div>
          <div><Button variant="primary" onClick={() => setStep(4)}>Continue</Button></div>
        </div></section>
      )}

      {step === 4 && (
        <section className="card"><div className="card-head"><h3>Code execution sandbox</h3>
          {health.data && <Status value={health.data.sandbox.available ? "ok" : "unvalidated"} label={health.data.sandbox.available ? "available" : "unavailable"} />}</div>
          <div className="card-body col">
            <p>Running tests or code uses a host-controlled Docker container with no network and only the task's worktree mounted read-only. Docker is optional.</p>
            {health.data && !health.data.sandbox.available && (
              <div className="callout info small">To enable it: install Docker Desktop, start it, then in a terminal run <code>docker pull python:3.12-slim</code> and press “Check again”.</div>
            )}
            {health.data && !health.data.sandbox.available && (
              <div className="callout warn"><strong>Sandbox unavailable:</strong> {health.data.sandbox.detail}<br />
                Research and writing work normally. Coding tasks still run in isolated git worktrees, but behavior checks become explicit <em>owner approval</em> steps — NEXUS never runs code unsandboxed.</div>
            )}
            {health.data?.sandbox.available && <div className="callout ok">{health.data.sandbox.detail}</div>}
            <div className="row"><Button onClick={async () => { health.reload(); }}>Check again</Button><Button variant="primary" onClick={() => setStep(5)}>Continue</Button></div>
          </div></section>
      )}

      {step === 5 && (projectId ? (
        <div className="col gap-lg">
          <InternetPanel projectId={projectId} />
          <AutonomyPanel projectId={projectId} />
          <div><Button variant="primary" onClick={() => setStep(6)}>Continue</Button></div>
        </div>
      ) : (
        <section className="card"><div className="card-body col"><div>Create or choose a workspace first (step 2); internet and autonomy are set per project.</div>
          <div><Button onClick={() => setStep(1)}>Go to Workspace</Button> <Button variant="primary" onClick={() => setStep(6)}>Skip</Button></div></div></section>
      ))}
      {step === 6 && <SpendingStep settings={settings.data} onDone={() => onDone(projectId)} />}
    </div>
  );
}

function WorkspaceStep({ onCreated }: { onCreated: (id: string) => void }) {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [allow, setAllow] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function create() {
    setBusy(true); setErr(null);
    try {
      const p = await post("/api/projects", { name: name || path.split(/[\\/]/).pop(), workspace_root: path,
        fetch_allowlist: allow.split(",").map((s) => s.trim()).filter(Boolean) });
      onCreated(p.id);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  async function demo(kind: string) {
    setBusy(true); setErr(null);
    try { const p = await post(`/api/projects/demo/${kind}`); onCreated(p.id); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  return (
    <div className="grid halves">
      <section className="card"><div className="card-head"><h3>Use a folder</h3></div><div className="card-body col">
        <p className="small muted">Agents can read files inside this folder only. Put research sources under <code>sources/</code>. For coding missions the folder must be a git repository; agents change code in separate worktrees, never in your checkout.</p>
        <div className="row"><input aria-label="Workspace folder" value={path} onChange={(e) => setPath(e.target.value)} placeholder="C:\\path\\to\\workspace" />
          <Button onClick={async () => { const p = await bridge().selectFolder(); if (p) setPath(p); }}>Browse…</Button></div>
        <Field label="Project name" htmlFor="pn"><input id="pn" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Web domains agents may fetch (optional)" htmlFor="pa" hint="Comma-separated, e.g. example.org. Empty means no web access.">
          <input id="pa" value={allow} onChange={(e) => setAllow(e.target.value)} /></Field>
        {err && <div className="callout bad" role="alert">{err}</div>}
        <div><Button variant="primary" disabled={!path || busy} onClick={create}>Create project</Button></div>
      </div></section>
      <section className="card"><div className="card-head"><h3>Or try an offline demo</h3><Badge tone="sim">simulation</Badge></div><div className="card-body col">
        <p className="small">Demo projects use fictional fixture data and the deterministic fake provider. They exercise planning, evidence lineage, verification, repair and integration without any API key or cost — but the agent outputs are scripted, not model-generated.</p>
        <div className="btn-group">
          <Button disabled={busy} onClick={() => demo("research")}>Create research demo</Button>
          <Button disabled={busy} onClick={() => demo("code")}>Create coding demo</Button>
        </div>
      </div></section>
    </div>
  );
}

function SpendingStep({ settings, onDone }: { settings: any; onDone: () => void }) {
  const cur = settings?.["concurrency.defaults"] ?? {};
  const [cap, setCap] = useState<string>(cur.default_cap_usd ? String(cur.default_cap_usd) : "2.00");
  const [workers, setWorkers] = useState<number>(cur.max_active_workers ?? 4);
  const [branches, setBranches] = useState<number>(cur.max_branches ?? 2);
  const [err, setErr] = useState<string | null>(null);
  return (
    <section className="card"><div className="card-head"><h3>Spending and concurrency</h3></div><div className="card-body col">
      <p className="small">Every live mission requires a spend cap. Before each model call NEXUS reserves the maximum it could cost (bounded output, conservative input estimate) and reconciles with provider-reported usage afterwards. Concurrent agents share the cap. These are estimates from the prices you configured, not invoices.</p>
      <div className="grid thirds">
        <Field label="Default spend cap per live mission (USD)" htmlFor="cap"><input id="cap" type="number" min="0.01" step="0.01" value={cap} onChange={(e) => setCap(e.target.value)} /></Field>
        <Field label="Max active workers" htmlFor="mw" hint="Default 4"><input id="mw" type="number" min="1" max="16" value={workers} onChange={(e) => setWorkers(Number(e.target.value))} /></Field>
        <Field label="Max exploratory branches" htmlFor="mb" hint="Default 2"><input id="mb" type="number" min="0" max="4" value={branches} onChange={(e) => setBranches(Number(e.target.value))} /></Field>
      </div>
      {err && <div className="callout bad" role="alert">{err}</div>}
      <div><Button variant="primary" onClick={async () => {
        try {
          await put("/api/settings/concurrency.defaults", { value: { default_cap_usd: Number(cap), max_active_workers: workers, max_branches: branches } });
          onDone();
        } catch (e) { setErr((e as Error).message); }
      }}>Finish setup</Button></div>
    </div></section>
  );
}
