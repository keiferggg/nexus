import { useState } from "react";
import { del, post, put, timeAgo, useApi } from "../api";
import { AutonomyPanel } from "../components/Autonomy";
import { InternetPanel } from "../components/Internet";
import { SandboxPanel } from "../components/Sandbox";
import { Badge, Button, Card, Field, Load, Mono, Status } from "../components/ui";

const TEMPLATES: Record<string, { label: string; config: any; secretLabel: string; help: string }> = {
  http: {
    label: "HTTP API", secretLabel: "Token (sent as Authorization: Bearer …)",
    help: "You declare every action (method + path). Agents can only fill in arguments; they cannot reach other URLs.",
    config: { base_url: "https://api.example.com", actions: [
      { name: "post_message", method: "POST", path: "/messages", description: "Post a message", params: { text: "string" } }] },
  },
  github: {
    label: "GitHub repository", secretLabel: "Fine-grained personal access token",
    help: "Issues and comments for one repository. Writes carry a hidden marker so ambiguous outcomes can be reconciled.",
    config: { repo: "owner/name" },
  },
  mcp: {
    label: "MCP server", secretLabel: "Secret (optional; passed as an env var for stdio servers)",
    help: "Model Context Protocol server over stdio (a local command you trust) or streamable HTTP (a URL). Effects come from the server's tool annotations; unknown effects are treated as external writes.",
    config: { transport: "stdio", command: "python", args: ["server.py"] },
  },
};

export default function Connections({ project }: { project: any }) {
  const cons = useApi<any[]>(`/api/projects/${project.id}/connectors`,
    (e) => e.type.startsWith("connector.") || e.type === "internet.policy_changed");
  const acts = useApi<any[]>(`/api/projects/${project.id}/actions`, (e) => e.type.startsWith("action."));
  return (
    <div className="page">
      <div className="page-head"><div className="titles"><h1>Connected services</h1>
        <div className="sub">Agents take real actions only through these connections. You choose, per action, whether it is denied, needs your approval, or is pre-approved.</div></div></div>
      <div className="col gap-lg">
        <AutonomyPanel projectId={project.id} />
        <InternetPanel projectId={project.id} />
        <SandboxPanel projectId={project.id} />
        <Card title="Action requests" id="ar" flush>
          <Load q={acts} empty={(d) => (d.length ? null : "No action has been requested yet")}>{(rows) => (
            <table className="data"><thead><tr><th>Action</th><th>Requested by / why</th><th>Status</th><th /></tr></thead><tbody>
              {rows.map((r) => <ActionRow key={r.id} r={r} reload={acts.reload} />)}</tbody></table>
          )}</Load>
        </Card>
        <Load q={cons}>{(rows) => (
          <div className="col gap-lg">{rows.map((c) => <ConnectorCard key={c.id} c={c} reload={cons.reload} />)}</div>
        )}</Load>
        <AddConnector projectId={project.id} onAdded={cons.reload} />
      </div>
    </div>
  );
}

export function ActionRow({ r, reload }: { r: any; reload: () => void }) {
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  async function act(path: string, body: any = { note }) {
    setErr(null);
    try { await post(path, body); reload(); } catch (e) { setErr((e as Error).message); }
  }
  return (
    <tr data-testid="action-row">
      <td><Mono>{r.label}: {r.action}</Mono><div><Badge tone={r.effect_class === "read_only" ? "neutral" : "warn"}>{r.effect_class.replace("_", " ")}</Badge></div>
        <pre className="pre" style={{ maxHeight: 90, marginTop: 4 }}>{JSON.stringify(r.args, null, 1)}</pre></td>
      <td className="small"><Mono>{r.requested_by}</Mono><div>{r.rationale}</div><div className="faint">{timeAgo(r.created_at)}{r.approval ? ` · approval: ${r.approval}` : ""}</div></td>
      <td><Status value={r.status} />{r.error && <div className="small" style={{ color: "var(--bad)" }}>{r.error}</div>}
        {r.decision_note && <div className="small faint">{r.decision_note}</div>}</td>
      <td style={{ minWidth: 220 }}>
        {r.status === "pending" && <div className="col">
          <input aria-label="decision note" placeholder="note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="btn-group"><Button size="sm" variant="primary" onClick={() => act(`/api/actions/${r.id}/approve`)}>Approve & run</Button>
            <Button size="sm" variant="danger" onClick={() => act(`/api/actions/${r.id}/deny`)}>Deny</Button></div></div>}
        {r.status === "uncertain" && <div className="col">
          <div className="small">The request may have taken effect. NEXUS will not retry it.</div>
          <Button size="sm" onClick={() => act(`/api/actions/${r.id}/reconcile`, {})}>Ask the service</Button>
          <input aria-label="resolution note" placeholder="how you checked" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="btn-group"><Button size="sm" disabled={!note} onClick={() => act(`/api/actions/${r.id}/resolve`, { outcome: "succeeded", note })}>It happened</Button>
            <Button size="sm" disabled={!note} onClick={() => act(`/api/actions/${r.id}/resolve`, { outcome: "failed", note })}>It did not</Button></div></div>}
        {err && <div className="small" style={{ color: "var(--bad)" }} role="alert">{err}</div>}
      </td>
    </tr>
  );
}

function ConnectorCard({ c, reload }: { c: any; reload: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  async function setPolicy(aid: string, body: any) {
    setErr(null);
    try { await put(`/api/connector-actions/${aid}`, body); reload(); } catch (e) { setErr((e as Error).message); }
  }
  return (
    <section className="card" data-testid={`connector-${c.kind}`}>
      <div className="card-head"><h3>{c.label}</h3><Badge>{c.kind}</Badge><Status value={c.status} />
        {c.has_secret && <Badge tone="ok">credential in OS store</Badge>}
        <Button size="sm" onClick={async () => { await post(`/api/connectors/${c.id}/discover`); reload(); }}>Re-check</Button>
        {c.kind === "web" ? <Badge tone="info">built-in · set by Internet access</Badge> :
        <Button size="sm" variant="danger" onClick={async () => { if (confirm(`Remove ${c.label}? Pending requests are canceled and its credential is deleted.`)) { await del(`/api/connectors/${c.id}`); reload(); } }}>Remove</Button>}</div>
      <div className="card-body col">
        {c.status_detail && <div className="small muted">{c.status_detail}</div>}
        {err && <div className="callout bad">{err}</div>}
        <table className="data"><thead><tr><th>Action</th><th>Effect</th><th>Autonomy</th><th>Auto quota / day</th></tr></thead><tbody>
          {c.actions.map((a: any) => (
            <tr key={a.id} style={{ opacity: a.enabled ? 1 : 0.5 }}>
              <td><Mono>{a.name}</Mono><div className="small faint">{a.description}</div></td>
              <td><Badge tone={a.effect_class === "read_only" ? "neutral" : a.effect_class === "destructive" ? "bad" : "warn"}>{a.effect_class.replace("_", " ")}</Badge></td>
              <td><select aria-label={`autonomy for ${a.name}`} value={a.autonomy} onChange={(e) => setPolicy(a.id, { autonomy: e.target.value })} style={{ width: 170 }}>
                <option value="deny">deny</option><option value="ask">ask me each time</option>
                <option value="auto" disabled={a.effect_class === "destructive"}>pre-approved{a.effect_class === "destructive" ? " (not allowed)" : ""}</option></select></td>
              <td><input aria-label={`quota for ${a.name}`} type="number" min={0} max={1000} defaultValue={a.max_auto_per_day} style={{ width: 80 }}
                onBlur={(e) => Number(e.target.value) !== a.max_auto_per_day && setPolicy(a.id, { max_auto_per_day: Number(e.target.value) })} /></td>
            </tr>))}
        </tbody></table>
        <div className="small faint">Destructive actions always need your approval. Pre-approved actions still run through NEXUS, are recorded as evidence, and stop at the daily quota.</div>
      </div>
    </section>
  );
}

function AddConnector({ projectId, onAdded }: { projectId: string; onAdded: () => void }) {
  const [kind, setKind] = useState("http");
  const [label, setLabel] = useState("");
  const [cfg, setCfg] = useState(JSON.stringify(TEMPLATES.http.config, null, 2));
  const [secret, setSecret] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const t = TEMPLATES[kind];
  return (
    <Card title="Connect a service" id="add-con">
      <div className="col">
        <div className="grid thirds">
          <Field label="Kind" htmlFor="ck"><select id="ck" value={kind} onChange={(e) => { setKind(e.target.value); setCfg(JSON.stringify(TEMPLATES[e.target.value].config, null, 2)); }}>
            {Object.entries(TEMPLATES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select></Field>
          <Field label="Label" htmlFor="cl"><input id="cl" value={label} onChange={(e) => setLabel(e.target.value)} placeholder={t.label} /></Field>
          <Field label={t.secretLabel} htmlFor="cs"><input id="cs" type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} /></Field>
        </div>
        <div className="small muted">{t.help}</div>
        <Field label="Configuration (JSON)" htmlFor="cc"><textarea id="cc" className="mono" style={{ minHeight: 160 }} value={cfg} onChange={(e) => setCfg(e.target.value)} /></Field>
        {err && <div className="callout bad" role="alert">{err}</div>}
        <div><Button variant="primary" disabled={busy} onClick={async () => {
          setBusy(true); setErr(null);
          try {
            await post(`/api/projects/${projectId}/connectors`, { kind, label: label || t.label, config: JSON.parse(cfg), secret: secret || null });
            setSecret(""); onAdded();
          } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
        }}>Connect and discover actions</Button></div>
        <div className="small faint">New actions start conservative: reads pre-approved, writes ask you, destructive actions denied.</div>
      </div>
    </Card>
  );
}
