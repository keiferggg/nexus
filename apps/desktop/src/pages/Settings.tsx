import { useEffect, useState } from "react";
import { bridge, post, put, useApi } from "../api";
import { DefaultModelPicker, ProvidersPanel } from "../components/Providers";
import { UpdatesPanel } from "../components/Updates";
import { Badge, Button, Card, Field, KV, Load, Mono, Status, Tabs } from "../components/ui";

type Sec = "providers" | "budgets" | "learning" | "initiative" | "backups" | "diagnostics";

export default function Settings({ project }: { project: any }) {
  const [sec, setSec] = useState<Sec>("providers");
  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <div className="page-head"><div className="titles"><h1>Settings</h1></div></div>
      <Tabs<Sec> value={sec} onChange={setSec} tabs={[{ id: "providers", label: "Providers & models" }, { id: "budgets", label: "Budgets & concurrency" },
        { id: "learning", label: "Learning" }, { id: "initiative", label: "Initiative" }, { id: "backups", label: "Backups" }, { id: "diagnostics", label: "Diagnostics" }]} />
      {sec === "providers" && <div className="col gap-lg"><Card title="Default model" id="dm"><DefaultModelPicker /></Card><ProvidersPanel /></div>}
      {sec === "budgets" && <Budgets />}
      {sec === "learning" && <Learning />}
      {sec === "initiative" && <Initiative project={project} />}
      {sec === "backups" && <Backups />}
      {sec === "diagnostics" && <Diagnostics />}
    </div>
  );
}

function Budgets() {
  const s = useApi<any>("/api/settings");
  return (
    <Load q={s}>{(st) => {
      const d = st["concurrency.defaults"] ?? {};
      const ret = st["retention.days"] ?? null;
      return (
        <Card title="Defaults for new missions" id="bd">
          <KV items={[["Default live spend cap", d.default_cap_usd ? `$${d.default_cap_usd}` : "not set (required per live mission)"],
            ["Max active workers", d.max_active_workers ?? 4], ["Max exploratory branches", d.max_branches ?? 2],
            ["Event/artifact retention", ret ? `${ret} days` : "keep everything (default)"]]} />
          <div className="small muted" style={{ marginTop: 8 }}>Change these in Setup → Spending. Each mission's cap can be changed from its header; agents can never raise it.</div>
        </Card>
      );
    }}</Load>
  );
}

function Learning() {
  const skills = useApi<any[]>("/api/skills", (e) => e.type.startsWith("skill."));
  const s = useApi<any>("/api/settings", (e) => e.type === "settings.changed");
  const cfg = s.data?.["learning.auto_promote"] ?? { enabled: false, scopes: [] };
  const [scopes, setScopes] = useState("");
  useEffect(() => setScopes((cfg.scopes ?? []).join(", ")), [s.data]);
  return (
    <div className="col gap-lg">
      <Card title="Automatic promotion" id="ap">
        <div className="col small">
          <div>Off by default. When enabled, only the named, narrowly scoped procedural skills may auto-promote — and only to <em>canary</em> after passing held-out evaluation against the frozen baseline. Nothing changes the running application, permissions or budgets.</div>
          <label className="row"><input type="checkbox" checked={!!cfg.enabled} onChange={async (e) => { await put("/api/settings/learning.auto_promote", { value: { ...cfg, enabled: e.target.checked } }); s.reload(); }} /> Enable automatic promotion for these skill names</label>
          <div className="row"><input aria-label="skill scopes" value={scopes} onChange={(e) => setScopes(e.target.value)} placeholder="skill-name, other-skill" />
            <Button size="sm" onClick={async () => { await put("/api/settings/learning.auto_promote", { value: { ...cfg, scopes: scopes.split(",").map((x) => x.trim()).filter(Boolean) } }); s.reload(); }}>Save scopes</Button></div>
        </div>
      </Card>
      <Card title="Skill versions" id="sk" flush>
        <Load q={skills} empty={(d) => (d.length ? null : "No skill candidates yet")}>{(rows) => (
          <table className="data"><thead><tr><th>Skill</th><th>Status</th><th>Hypothesis</th><th>Evaluation</th><th /></tr></thead><tbody>
            {rows.map((k) => <tr key={k.id}><td><Mono>{k.skill_name}</Mono> v{k.version}<div className="faint small">{k.target} · {k.conditions}</div></td><td><Status value={k.status} /></td>
              <td className="small">{k.hypothesis}</td>
              <td className="small">{k.eval?.rates ? Object.entries(k.eval.rates).map(([sp, r]: any) => <div key={sp}>{sp}: {(r.baseline * 100).toFixed(0)}% → {(r.candidate * 100).toFixed(0)}% (n={r.n})</div>) : "not evaluated"}
                {k.eval?.reasons?.length ? <div style={{ color: "var(--warn)" }}>{k.eval.reasons.join("; ")}</div> : null}</td>
              <td>{["evaluating", "canary"].includes(k.status) && k.eval?.eligible && <Button size="sm" onClick={async () => { await post(`/api/skills/${k.id}/promote`); skills.reload(); }}>Promote</Button>}
                {["promoted", "canary"].includes(k.status) && <Button size="sm" onClick={async () => { await post(`/api/skills/${k.id}/rollback`, { resolution: "owner rollback" }); skills.reload(); }}>Roll back</Button>}</td></tr>)}
          </tbody></table>)}</Load>
      </Card>
    </div>
  );
}

function Initiative({ project }: { project: any }) {
  const s = useApi<any>("/api/settings", (e) => e.type === "settings.changed");
  const opps = useApi<any[]>(project ? `/api/projects/${project.id}/opportunities?rescan=true` : null, (e) => e.type.startsWith("opportunity."));
  const pol = s.data?.["initiative.policy"] ?? { enabled: false, budget_usd: 0 };
  const [budget, setBudget] = useState(String(pol.budget_usd ?? 0));
  return (
    <div className="col gap-lg">
      <Card title="Background initiative policy" id="ip">
        <div className="col small">
          <div>NEXUS queues opportunities tied to your existing missions (newly unblocked work, stale evidence, recurring failures). It never invents new objectives. Background execution needs this policy enabled <em>and</em> a capped budget.</div>
          <label className="row"><input type="checkbox" checked={!!pol.enabled} onChange={async (e) => { await put("/api/settings/initiative.policy", { value: { ...pol, enabled: e.target.checked } }); s.reload(); }} /> Allow background execution within the cap</label>
          <div className="row"><Field label="Background cap (USD)" htmlFor="ib"><input id="ib" type="number" min="0" step="0.01" value={budget} onChange={(e) => setBudget(e.target.value)} /></Field>
            <Button size="sm" onClick={async () => { await put("/api/settings/initiative.policy", { value: { ...pol, budget_usd: Number(budget) } }); s.reload(); }}>Save cap</Button></div>
        </div>
      </Card>
      <Card title="Opportunity queue" id="oq" flush>
        <Load q={opps} empty={(d) => (d.length ? null : "No opportunities")}>{(rows) => (
          <table className="data"><thead><tr><th>Kind</th><th>Rationale</th><th>Est. cost</th><th>Status</th><th /></tr></thead><tbody>
            {rows.map((o) => <tr key={o.id}><td className="small">{o.kind.replace(/_/g, " ")}<div className="faint mono">{o.goal_ref}</div></td><td className="small">{o.rationale}<div className="faint">scope: {o.scope} · expires {new Date(o.expires_at).toLocaleDateString()}</div></td>
              <td className="small">${o.cost_estimate_usd.toFixed(4)}</td><td><Status value={o.status} /><div className="xs faint">{o.background_reason}</div></td>
              <td>{o.status === "open" && <div className="btn-group"><Button size="sm" onClick={async () => { await post(`/api/opportunities/${o.id}/approve`); opps.reload(); }}>Approve</Button>
                <Button size="sm" variant="ghost" onClick={async () => { await post(`/api/opportunities/${o.id}/dismiss`); opps.reload(); }}>Dismiss</Button></div>}</td></tr>)}
          </tbody></table>)}</Load>
      </Card>
    </div>
  );
}

function Backups() {
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Card title="Backup" id="bk">
      <div className="col small">
        <div>Creates a zip with the database, immutable artifacts and checkpoints, plus a manifest of every artifact hash. <strong>Credentials are never included</strong> — after restoring on another machine, re-enter API keys.</div>
        <div>Restore runs while the service is stopped: <Mono>nexus-service restore --data-dir NEWDIR --from backup.zip</Mono> (see the user guide).</div>
        <div><Button variant="primary" disabled={busy} onClick={async () => {
          const dest = await bridge().selectSaveFile(`nexus-backup-${new Date().toISOString().slice(0, 10)}.zip`);
          if (!dest) return;
          setBusy(true); setErr(null);
          try { setRes(await post("/api/backup", { value: dest })); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
        }}>{busy ? "Creating…" : "Create backup…"}</Button></div>
        {res && <div className="callout ok">Backup written: <Mono>{res.path}</Mono> · {res.artifacts} artifacts · {(res.bytes / 1024).toFixed(0)} KB · credentials included: no</div>}
        {err && <div className="callout bad">{err}</div>}
      </div>
    </Card>
  );
}

function Diagnostics() {
  const q = useApi<any>("/api/diagnostics");
  const [info, setInfo] = useState<any>(null);
  useEffect(() => { void bridge().appInfo().then(setInfo); }, []);
  return (
    <Load q={q}>{(d) => (
      <div className="grid halves">
        <UpdatesPanel />
        <Card title="Health" id="h">
          <KV items={[["Service", <Status value={d.health.status === "ready" ? "ok" : "warn"} label={d.health.status} />], ["Version", d.health.version],
            ["Framework", Object.entries(d.health.framework).map(([k, v]) => `${k} ${v}`).join(", ")],
            ["Schema", d.health.schema.join(", ")], ["Credential store", <>{d.health.secrets.backend} {d.health.secrets.available ? <Badge tone="ok">ok</Badge> : <Badge tone="bad">unavailable</Badge>}</>],
            ["Sandbox", <>{d.health.sandbox.available ? <Badge tone="ok">available</Badge> : <Badge tone="warn">unavailable</Badge>} <span className="small faint">{d.health.sandbox.detail}</span></>],
            ["Supervisor", d.health.supervisor_error ?? "no errors"], ["Data folder", <Mono>{d.health.data_dir}</Mono>],
            ["Activity log", d.activity_log ? <><Mono>{d.activity_log}</Mono> <Button size="sm" onClick={() => bridge().openActivity()}>Open folder</Button></> : "off"],
            ["Service log", info ? <><Mono>{info.logPath}</Mono> <Button size="sm" onClick={() => bridge().revealLog()}>Open</Button></> : "…"]]} />
        </Card>
        <Card title="Integrity & recovery" id="ir">
          <KV items={[["Artifacts verified", d.artifact_integrity.ok], ["Corrupt", d.artifact_integrity.corrupt.length], ["Missing", d.artifact_integrity.missing.length],
            ["Startup: abandoned attempts", d.startup_reconciliation?.abandoned_attempts?.length ?? 0],
            ["Startup: uncertain model calls", d.startup_reconciliation?.uncertain_model_calls ?? 0],
            ["Startup: uncertain tool writes", d.startup_reconciliation?.uncertain_tool_ops ?? 0],
            ...Object.entries(d.counts).map(([k, v]) => [k, String(v)] as [string, string])]} />
          <div className="small faint" style={{ marginTop: 8 }}>{d.note}</div>
        </Card>
      </div>
    )}</Load>
  );
}
