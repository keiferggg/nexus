import { useEffect, useState } from "react";
import { put, timeAgo, useApi } from "../api";
import { Badge, Button, Card, Field, Load, Status } from "./ui";

export function AutonomyPanel({ projectId }: { projectId: string }) {
  const q = useApi<any>(`/api/projects/${projectId}/autonomy`, (e) => e.type === "autonomy.changed");
  const [f, setF] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);
  const edit = (patch: any) => { setF({ ...f, ...patch }); setDirty(true); setSaved(false); };
  useEffect(() => { if (q.data && !f) setF({ ...q.data, hosts: q.data.web_hosts.join(", "), domains: q.data.email_domains.join(", ") }); }, [q.data]);
  // The mode saves the moment it is chosen: leaving the page (or the setup wizard) must never silently drop it.
  async function save(next: any = f) {
    setErr(null); setSaved(false);
    const f = next;
    const split = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
    try {
      await put(`/api/projects/${projectId}/autonomy`, { mode: f.mode, web_hosts: split(f.hosts), email_domains: split(f.domains),
        max_writes_per_mission: Number(f.max_writes_per_mission), max_writes_per_hour: Number(f.max_writes_per_hour),
        monitor: !!f.monitor, suspend_after_violations: Number(f.suspend_after_violations) });
      setSaved(true); setDirty(false); q.reload();
    } catch (e) { setErr((e as Error).message); }
  }
  const setMode = (mode: string) => { const n = { ...f, mode }; setF(n); void save(n); };
  return (
    <Card title="Autonomy" id="autonomy" actions={q.data && <Status value={q.data.mode === "unattended" ? "running" : "ok"} label={q.data.mode} />}>
      <Load q={q}>{(d) => !f ? null : (
        <div className="col">
          <div className="row wrap">
            <label className="row"><input type="radio" name="am" checked={f.mode === "supervised"} onChange={() => setMode("supervised")} />
              <span><strong>Supervised</strong> — writes wait for your approval</span></label>
            <label className="row"><input type="radio" name="am" checked={f.mode === "unattended"} onChange={() => setMode("unattended")} />
              <span><strong>Unattended</strong> — agents act on their own inside guardrails</span></label>
          </div>
          {f.mode === "unattended" && (
            <>
              <div className="callout info small">Agents can write only to destinations <strong>you</strong> supplied: hosts and email addresses you write in a mission or message, the lists below, or services you connected. A web page cannot add a destination. Every unattended write is also checked for credentials, limits and — if enabled — by Sentinel, an independent agent that never reads untrusted content.</div>
              <div className="grid halves">
                <Field label="Always-trusted web hosts" htmlFor="ah" hint="Comma-separated; subdomains included."><input id="ah" value={f.hosts} onChange={(e) => edit({ hosts: e.target.value })} /></Field>
                <Field label="Always-trusted email domains" htmlFor="ad"><input id="ad" value={f.domains} onChange={(e) => edit({ domains: e.target.value })} /></Field>
              </div>
              <div className="grid thirds">
                <Field label="Max unattended writes per mission" htmlFor="am1"><input id="am1" type="number" min={1} value={f.max_writes_per_mission} onChange={(e) => edit({ max_writes_per_mission: e.target.value })} /></Field>
                <Field label="Max per hour (project)" htmlFor="am2"><input id="am2" type="number" min={1} value={f.max_writes_per_hour} onChange={(e) => edit({ max_writes_per_hour: e.target.value })} /></Field>
                <Field label="Suspend a task after N blocked actions" htmlFor="am3"><input id="am3" type="number" min={1} max={20} value={f.suspend_after_violations} onChange={(e) => edit({ suspend_after_violations: e.target.value })} /></Field>
              </div>
              <label className="row small"><input type="checkbox" checked={!!f.monitor} onChange={(e) => edit({ monitor: e.target.checked })} />
                Sentinel reviews each unattended write (one small model call per write; recommended)</label>
              <details className="small"><summary>Checks applied to every unattended write</summary><ul>{d.checks.map((c: string) => <li key={c}>{c}</li>)}</ul></details>
            </>
          )}
          {err && <div className="callout bad" role="alert">{err}</div>}
          <div className="row"><Button variant="primary" onClick={() => save()}>Save autonomy</Button>{saved && <span className="small muted" role="status">Saved</span>}
            {dirty && <span className="small" style={{ color: "var(--warn)" }} role="status">Unsaved changes</span>}</div>
        </div>
      )}</Load>
    </Card>
  );
}

export function Digest({ projectId, onOpenMission }: { projectId: string; onOpenMission: (id: string) => void }) {
  const [since] = useState<string>(() => {
    try { return localStorage.getItem(`nexus.digest.${projectId}`) || new Date(Date.now() - 86400000).toISOString(); } catch { return ""; }
  });
  const q = useApi<any>(`/api/projects/${projectId}/digest?since=${encodeURIComponent(since)}`,
    (e) => e.type.startsWith("action.") || e.type.startsWith("guard.") || e.type.startsWith("attention."));
  return (
    <Card title="While you were away" id="digest" actions={<Button size="sm" onClick={() => { try { localStorage.setItem(`nexus.digest.${projectId}`, new Date().toISOString()); } catch { /* ignore */ } q.reload(); }}>Mark as seen</Button>}>
      <Load q={q}>{(d) => {
        const quiet = !d.actions_done.length && !d.actions_blocked.length && !d.actions_waiting.length && !d.suspended_tasks.length && !d.exposures.length;
        return (
          <div className="col small" data-testid="digest">
            <div className="row wrap">
              <Badge tone="ok">{d.actions_done.length} actions taken</Badge>
              <Badge tone={d.actions_blocked.length ? "bad" : "neutral"}>{d.actions_blocked.length} blocked by guard</Badge>
              <Badge tone={d.actions_waiting.length ? "warn" : "neutral"}>{d.actions_waiting.length} waiting for you</Badge>
              <Badge tone={d.suspended_tasks.length ? "bad" : "neutral"}>{d.suspended_tasks.length} tasks suspended</Badge>
              <Badge tone={d.exposures.length ? "warn" : "neutral"}>{d.exposures.length} suspicious sources read</Badge>
              <Badge>${Number(d.model_spend_usd).toFixed(4)} model spend</Badge>
            </div>
            {quiet && <div className="muted">Nothing happened since {timeAgo(d.since)}.</div>}
            {d.actions_done.slice(0, 8).map((a: any) => <div key={a.id}><Status value="succeeded" label="done" /> {a.what} → <span className="mono">{JSON.stringify(a.target)}</span> <span className="faint">{timeAgo(a.when)} · {a.approval}</span></div>)}
            {d.actions_blocked.slice(0, 8).map((a: any) => <div key={a.id}><Status value="denied" label="blocked" /> {a.what} → <span className="mono">{JSON.stringify(a.target)}</span><div className="faint">{a.note}</div></div>)}
            {d.suspended_tasks.map((s: any, i: number) => <div key={i}><Status value="blocked" label="suspended" /> {s.task_key}: {s.reason}
              {s.mission_id && <Button size="sm" variant="ghost" onClick={() => onOpenMission(s.mission_id)}>Open</Button>}</div>)}
            {d.exposures.slice(0, 5).map((e: any, i: number) => <div key={i} className="faint">Flagged source: <span className="mono">{e.source}</span> ({e.kinds.join(", ")})</div>)}
          </div>
        );
      }}</Load>
    </Card>
  );
}
