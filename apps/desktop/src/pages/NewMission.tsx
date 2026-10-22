import { useState } from "react";
import { post, useApi } from "../api";
import { PriceFix } from "../components/PriceFix";
import { Badge, Button, Field, Load } from "../components/ui";

const DEMOS: Record<string, { title: string; objective: string; success: string[]; constraints: string[] }> = {
  research: {
    title: "Larkspur bike-share brief",
    objective: "Brief the Larkspur council on a bike-share pilot",
    success: ["Research resident demand using workspace sources", "Write a one-page recommendation for the council"],
    constraints: ["Use only workspace sources"],
  },
  code: {
    title: "Extend textkit",
    objective: "Extend the textkit package with two helpers",
    success: ["Implement slugify(text) in textkit/textutils.py with a test", "Implement truncate(text, n) in textkit/textutils.py with a test"],
    constraints: ["Keep the public API backwards compatible"],
  },
};

function ListEditor({ label, items, setItems, placeholder, id }: { label: string; items: string[]; setItems: (v: string[]) => void; placeholder: string; id: string }) {
  return (
    <div className="field">
      <span className="label" id={id}>{label}</span>
      {items.map((it, i) => (
        <div className="row" key={i}>
          <span className="mono small faint" style={{ width: 34 }}>{id === "sc" ? `SC${i + 1}` : `C${i + 1}`}</span>
          <input aria-labelledby={id} value={it} placeholder={placeholder} onChange={(e) => setItems(items.map((x, j) => (j === i ? e.target.value : x)))} />
          <Button size="sm" variant="ghost" aria-label={`remove ${label} ${i + 1}`} onClick={() => setItems(items.filter((_, j) => j !== i))}>✕</Button>
        </div>
      ))}
      <div><Button size="sm" onClick={() => setItems([...items, ""])}>＋ Add</Button></div>
    </div>
  );
}

export default function NewMission({ project, onCreated }: { project: any; onCreated: (id: string) => void }) {
  const demoKind = project.is_demo ? (project.workspace_root.includes("code-") ? "code" : "research") : null;
  const d = demoKind ? DEMOS[demoKind] : null;
  const [title, setTitle] = useState(d?.title ?? "");
  const [objective, setObjective] = useState(d?.objective ?? "");
  const [success, setSuccess] = useState<string[]>(d?.success ?? [""]);
  const [constraints, setConstraints] = useState<string[]>(d?.constraints ?? []);
  const settings = useApi<any>("/api/settings");
  const exposure = useApi<any>(`/api/projects/${project.id}/data-exposure`);
  const defaults = settings.data?.["concurrency.defaults"] ?? {};
  const [mode, setMode] = useState<"offline_fake" | "live">("offline_fake");
  const [cap, setCap] = useState<string>("");
  const [workers, setWorkers] = useState<number | "">("");
  const [ack, setAck] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const autonomy = useApi<any>(`/api/projects/${project.id}/autonomy`);
  const [scopeHosts, setScopeHosts] = useState("");
  const [scopeEmails, setScopeEmails] = useState("");
  const readiness = useApi<any>(`/api/projects/${project.id}/live-readiness`, (e) => e.type === "pricing.set" || e.type.startsWith("provider.") || e.type === "settings.changed" || e.type === "agent.version_created");
  const liveReady = !!readiness.data?.ready;
  const capValue = cap === "" ? defaults.default_cap_usd ?? "" : cap;

  async function submit() {
    setBusy(true); setErr(null);
    try {
      const m = await post(`/api/projects/${project.id}/missions`, {
        title: title || objective.slice(0, 60), objective, success_conditions: success.filter((s) => s.trim()),
        constraints: constraints.filter((s) => s.trim()), execution_mode: mode,
        spend_cap_usd: mode === "live" ? Number(capValue) : null,
        max_active_workers: Number(workers || defaults.max_active_workers || 4), max_branches: defaults.max_branches ?? 2,
        data_exposure_acknowledged: ack, start: true,
        unattended_web_hosts: scopeHosts.split(",").map((s) => s.trim()).filter(Boolean),
        unattended_emails: scopeEmails.split(",").map((s) => s.trim()).filter(Boolean),
      });
      onCreated(m.mission.id);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  const valid = objective.trim() && success.some((s) => s.trim()) && (mode === "offline_fake" || (Number(capValue) > 0 && ack));
  return (
    <div className="page" style={{ maxWidth: 1100 }}>
      <div className="page-head"><div className="titles"><h1>New mission</h1><div className="sub">{project.name}</div></div></div>
      <div className="grid two">
        <section className="card"><div className="card-body col gap-lg">
          <Field label="Title" htmlFor="mt"><input id="mt" value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
          <Field label="Objective" htmlFor="mo" hint="What outcome do you want? Agents will ask why work serves this."><textarea id="mo" value={objective} onChange={(e) => setObjective(e.target.value)} /></Field>
          <ListEditor id="sc" label="Success conditions (measurable)" items={success} setItems={setSuccess} placeholder="e.g. Cite at least two independent sources for demand" />
          <ListEditor id="cons" label="Constraints" items={constraints} setItems={setConstraints} placeholder="e.g. Use only workspace sources" />
        </div></section>
        <div className="col gap-lg">
          <section className="card"><div className="card-head"><h3>Execution</h3></div><div className="card-body col">
            <label className="row"><input type="radio" name="mode" checked={mode === "offline_fake"} onChange={() => setMode("offline_fake")} />
              <span>Offline demo <Badge tone="sim">simulation</Badge></span></label>
            <div className="small muted" style={{ marginLeft: 22 }}>Deterministic fake provider; scripted outputs, no cost, no data leaves the machine.</div>
            <label className="row"><input type="radio" name="mode" checked={mode === "live"} disabled={!liveReady} onChange={() => setMode("live")} />
              <span>Live models</span></label>
            {readiness.data && !liveReady && (
              <div className="callout warn small" style={{ marginLeft: 22 }} data-testid="live-readiness">
                <strong>Before live models can run:</strong>
                <ul style={{ margin: "4px 0", paddingLeft: 18 }}>{readiness.data.problems.filter((p: any) => p.kind !== "no_price").map((p: any, i: number) =>
                  <li key={i}>{p.agent ? `${p.agent}: ` : p.model ? `${p.model}: ` : ""}{p.fix}</li>)}</ul>
                {readiness.data.problems.filter((p: any) => p.kind === "no_price").map((p: any) => (
                  <div key={p.model} style={{ marginTop: 6 }}><div>Price needed for <strong>{p.model}</strong> ({p.provider}, used by {p.agents.length} agent(s)):</div>
                    <PriceFix providerId={p.provider_id} model={p.model} onSaved={() => readiness.reload()} /></div>))}
              </div>
            )}
            {liveReady && readiness.data?.warnings?.length > 0 && (
              <div className="small faint" style={{ marginLeft: 22 }}>{readiness.data.warnings.map((w: any) => <div key={w.model}>⚠ {w.model}: {w.fix}</div>)}</div>
            )}
            {mode === "live" && (
              <>
                <Field label="Spend cap (USD, required)" htmlFor="cap"><input id="cap" type="number" min="0.01" step="0.01" value={capValue} onChange={(e) => setCap(e.target.value)} /></Field>
                <Load q={exposure}>{(x) => (
                  <div className="callout warn small" role="note">
                    <strong>Data leaving this machine.</strong> For live runs these are sent to the providers below:
                    <ul>{x.what_is_sent.map((w: string) => <li key={w}>{w}</li>)}</ul>
                    <table className="data"><tbody>{x.agents.map((a: any) => <tr key={a.agent}><td>{a.agent} ({a.role})</td><td>{a.provider ?? <em>none configured</em>}</td>
                      <td className="mono">{a.model ?? "—"}</td><td>{a.sends_off_machine === false ? "stays local" : a.sends_off_machine ? "off-machine" : "—"}</td></tr>)}</tbody></table>
                    <div>Never sent: {x.never_sent.join("; ")}.</div>
                  </div>
                )}</Load>
                <label className="row small"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I understand which data will be sent to these providers.</label>
              </>
            )}
            <Field label="Max active workers" htmlFor="mw" hint="Bounded by the mission budget."><input id="mw" type="number" min={1} max={16} placeholder={String(defaults.max_active_workers ?? 4)} value={workers} onChange={(e) => setWorkers(e.target.value === "" ? "" : Number(e.target.value))} /></Field>
          </div></section>
          {autonomy.data?.mode === "unattended" && (
            <section className="card"><div className="card-head"><h3>Unattended destinations</h3></div><div className="card-body col">
              <div className="small muted">Agents may write only to destinations you supply. URLs and email addresses in the objective or success conditions count automatically; add any others here.</div>
              <Field label="Web hosts" htmlFor="uh"><input id="uh" value={scopeHosts} onChange={(e) => setScopeHosts(e.target.value)} placeholder="hooks.slack.com, api.example.org" /></Field>
              <Field label="Email addresses" htmlFor="ue"><input id="ue" value={scopeEmails} onChange={(e) => setScopeEmails(e.target.value)} placeholder="team@example.org" /></Field>
            </div></section>
          )}
          {autonomy.data && (
            <div className={`callout small ${autonomy.data.mode === "unattended" ? "warn" : "info"}`} data-testid="autonomy-mode">
              {autonomy.data.mode === "unattended"
                ? <><strong>Unattended:</strong> agents act on their own inside the guardrails; anything the guard blocks is listed in Overview → While you were away.</>
                : <><strong>Supervised:</strong> every external action (posts, emails, writes) waits for your approval. To let agents act on their own, switch to Unattended in Connected services → Autonomy and click Save autonomy.</>}
            </div>
          )}
          {err && <div className="callout bad" role="alert">{err}</div>}
          <Button variant="primary" disabled={!valid || busy} onClick={submit}>{busy ? "Creating…" : "Create and start mission"}</Button>
        </div>
      </div>
    </div>
  );
}
