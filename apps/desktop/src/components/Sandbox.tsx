import { useEffect, useState } from "react";
import { put, useApi } from "../api";
import { Badge, Button, Card, Field, Load, Mono } from "./ui";

/** Per-project sandbox profile: which Docker image runs this project's tests, and how much room it gets.
 *  Isolation (no network, read-only worktree, no new privileges) is fixed and shown, not editable. */
export function SandboxPanel({ projectId }: { projectId: string }) {
  const q = useApi<any>(`/api/projects/${projectId}/sandbox`, (e) => e.type === "sandbox.profile_changed");
  const [f, setF] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);
  useEffect(() => { if (q.data && !f) setF({ ...q.data }); }, [q.data]);
  const edit = (patch: any) => { setF({ ...f, ...patch }); setDirty(true); setSaved(false); };
  async function save() {
    setErr(null); setSaved(false);
    try {
      await put(`/api/projects/${projectId}/sandbox`, { image: f.image, memory_mb: Number(f.memory_mb), cpus: Number(f.cpus),
        tmpfs_mb: Number(f.tmpfs_mb), timeout_s: Number(f.timeout_s) });
      setSaved(true); setDirty(false); q.reload();
    } catch (e) { setErr((e as Error).message); }
  }
  return (
    <Card title="Code sandbox" id="sandbox" actions={q.data && (q.data.available ? <Badge tone="ok">available</Badge> : <Badge tone="warn">unavailable</Badge>)}>
      <Load q={q}>{(d) => !f ? null : (
        <div className="col">
          <div className="small">Agents' code and tests run only here: a Docker container with this project's worktree. Always: {d.fixed.join(", ")}.</div>
          {!d.available && <div className="callout warn small">{d.detail}</div>}
          <div className="grid thirds">
            <Field label="Image" htmlFor="sbi" hint={`Default ${d.default.image}. Use an image with your project's dependencies (no network inside).`}>
              <input id="sbi" value={f.image} onChange={(e) => edit({ image: e.target.value })} /></Field>
            <Field label="Memory (MB)" htmlFor="sbm"><input id="sbm" type="number" min={128} max={8192} value={f.memory_mb} onChange={(e) => edit({ memory_mb: e.target.value })} /></Field>
            <Field label="CPUs" htmlFor="sbc"><input id="sbc" type="number" min={0.25} max={8} step={0.25} value={f.cpus} onChange={(e) => edit({ cpus: e.target.value })} /></Field>
            <Field label="Scratch space /tmp (MB)" htmlFor="sbt"><input id="sbt" type="number" min={16} max={4096} value={f.tmpfs_mb} onChange={(e) => edit({ tmpfs_mb: e.target.value })} /></Field>
            <Field label="Time limit per run (s)" htmlFor="sbs"><input id="sbs" type="number" min={10} max={1800} value={f.timeout_s} onChange={(e) => edit({ timeout_s: e.target.value })} /></Field>
          </div>
          <div className="small faint">Running now: <Mono>{d.image}</Mono> · {d.detail}</div>
          {err && <div className="callout bad" role="alert">{err}</div>}
          <div className="row"><Button variant="primary" onClick={save}>Save sandbox</Button>{saved && <span className="small muted" role="status">Saved</span>}
            {dirty && <span className="small" style={{ color: "var(--warn)" }} role="status">Unsaved changes</span>}</div>
        </div>
      )}</Load>
    </Card>
  );
}
