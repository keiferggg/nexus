import { useState } from "react";
import { fmtUsd, put, useApi } from "../api";
import { Badge, Button, Field, Load, Mono, Status } from "../components/ui";

const CAPS = [
  ["internet", "Internet: search, fetch, PDFs, APIs, downloads"], ["utilities", "Utilities: calculator, dates, JSON/CSV, notes"],
  ["use_connectors", "Request real actions (connected services, web writes)"], ["custom_tools", "Use agent-created tools"],
  ["create_tools", "Create tools"], ["consult", "Consult other agents"], ["delegate_task", "Delegate subtasks"],
  ["propose_objective", "Propose objectives"], ["propose_strategy", "Propose strategies"],
  ["read_workspace", "Read workspace files"], ["read_source", "Read sources as evidence"], ["fetch_url", "Fetch allowlisted web pages"],
  ["search_memory", "Search permitted memory"], ["repo_read", "Read own worktree"], ["repo_write", "Write own worktree"],
  ["run_tests", "Run tests in sandbox"], ["http_post", "External write (also needs an owner grant)"],
];
const ROLES = ["planner", "researcher", "writer", "engineer", "reviewer", "integrator"];

export default function Studio({ project }: { project: any }) {
  const agents = useApi<any[]>(`/api/projects/${project.id}/agents`, (e) => e.type.startsWith("agent.") || e.type === "task.accepted");
  const providers = useApi<any[]>("/api/providers");
  return (
    <div className="page">
      <div className="page-head"><div className="titles"><h1>Agent studio</h1>
        <div className="sub">Persistent identities. Each change creates a new agent version; identity, current role and model/tool configuration are separate.</div></div></div>
      <Load q={agents} empty={(d) => (d.length ? null : "No agents")}>{(rows) => (
        <div className="grid halves">{rows.map((a) => <AgentCard key={a.id} a={a} providers={providers.data ?? []} reload={agents.reload} />)}</div>
      )}</Load>
    </div>
  );
}

function AgentCard({ a, providers, reload }: { a: any; providers: any[]; reload: () => void }) {
  const [edit, setEdit] = useState(false);
  const [role, setRole] = useState(a.role);
  const [prov, setProv] = useState(a.model_policy.provider_id ?? "");
  const [model, setModel] = useState(a.model_policy.model ?? "");
  const [maxOut, setMaxOut] = useState(a.model_policy.max_output_tokens ?? 8192);
  const [tools, setTools] = useState<string[]>(a.tools);
  const [scope, setScope] = useState(a.memory_scope);
  const [err, setErr] = useState<string | null>(null);
  const usable = providers.filter((p) => p.kind !== "fake");
  const perf = a.performance;
  return (
    <section className="card" data-testid={`agent-${a.name}`}>
      <div className="card-head"><h3>{a.name}</h3><Badge>{a.role}</Badge><span className="faint small">v{a.version}</span>
        <Button size="sm" onClick={() => setEdit(!edit)}>{edit ? "Close" : "Edit"}</Button></div>
      <div className="card-body col">
        <div className="small">{a.charter}</div>
        <div className="small"><span className="muted">Model:</span> {a.model_policy.model ? <Mono>{a.model_policy.model}</Mono> : <span className="faint">mission default</span>}
          {" · "}<span className="muted">Memory:</span> {a.memory_scope}</div>
        <div className="small"><span className="muted">Tools:</span> {a.tools.join(", ") || "none"}</div>
        <div className="row wrap small">
          <Badge tone="info">{perf.tasks ?? 0} tasks</Badge><Badge tone="ok">{perf.accepted ?? 0} accepted</Badge>
          <Badge tone={perf.rejected_submissions ? "warn" : "neutral"}>{perf.rejected_submissions ?? 0} rejected submissions</Badge>
          <Badge>{perf.model_calls} calls · {fmtUsd(perf.spend_usd)}</Badge>
        </div>
        {a.current_work.length > 0 && <div className="small">Working on: {a.current_work.map((w: any) => <span key={w.key}><Mono>{w.key}</Mono> <Status value={w.status} /> </span>)}</div>}
        <div className="small faint">Performance counts are NEXUS-verified outcomes (accepted vs rejected submissions), not self-reports.</div>
        {edit && (
          <div className="col" style={{ borderTop: "1px solid var(--border)", paddingTop: 10 }}>
            <div className="grid halves">
              <Field label="Role" htmlFor={`r-${a.id}`}><select id={`r-${a.id}`} value={role} onChange={(e) => setRole(e.target.value)}>{ROLES.map((r) => <option key={r}>{r}</option>)}</select></Field>
              <Field label="Memory scope" htmlFor={`m-${a.id}`}><select id={`m-${a.id}`} value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="private">private</option><option value="team">team</option><option value="project">project</option></select></Field>
              <Field label="Provider" htmlFor={`p-${a.id}`}><select id={`p-${a.id}`} value={prov} onChange={(e) => { setProv(e.target.value); setModel(""); }}>
                <option value="">use mission default</option>{usable.map((p) => <option key={p.id} value={p.id}>{p.label} ({p.status})</option>)}</select></Field>
              <Field label="Model" htmlFor={`mo-${a.id}`}><select id={`mo-${a.id}`} value={model} disabled={!prov} onChange={(e) => setModel(e.target.value)}>
                <option value="">choose…</option>{(usable.find((p) => p.id === prov)?.models ?? []).map((m: string) => <option key={m}>{m}</option>)}</select></Field>
              <Field label="Max output tokens" htmlFor={`o-${a.id}`} hint="Bounds the per-call budget reservation."><input id={`o-${a.id}`} type="number" min={128} max={64000} value={maxOut} onChange={(e) => setMaxOut(Number(e.target.value))} /></Field>
            </div>
            <fieldset style={{ border: 0, padding: 0, margin: 0 }}><legend className="label">Tool permissions</legend>
              <div className="grid halves">{CAPS.map(([c, l]) => <label key={c} className="row small"><input type="checkbox" checked={tools.includes(c)} onChange={(e) => setTools(e.target.checked ? [...tools, c] : tools.filter((t) => t !== c))} />{l}</label>)}</div></fieldset>
            {err && <div className="callout bad">{err}</div>}
            <div><Button variant="primary" onClick={async () => {
              try { await put(`/api/agents/${a.id}`, { role, memory_scope: scope, tools, model_policy: { provider_id: prov || null, model: model || null, max_output_tokens: maxOut } }); setEdit(false); reload(); }
              catch (e) { setErr((e as Error).message); } }}>Save as v{a.version + 1}</Button></div>
          </div>
        )}
        {a.versions.length > 1 && <div className="xs faint">History: {a.versions.map((v: any) => `v${v.version} ${v.role_default}`).join(" · ")}</div>}
      </div>
    </section>
  );
}
