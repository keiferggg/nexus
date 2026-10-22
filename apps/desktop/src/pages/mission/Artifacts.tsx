import { useCallback, useEffect, useMemo, useState } from "react";
import { get, NexusEvent, shortId, timeAgo, useApi } from "../../api";
import { Badge, Card, Doc, KV, lineDiff, Load, Mono, Status } from "../../components/ui";

const KINDS = ["deliverable", "diff", "plan", "verification_result", "source", "context_packet", "model_output"];

export default function Artifacts({ m }: { m: any }) {
  const mid = m.mission.id;
  const [kind, setKind] = useState("deliverable");
  const match = useCallback((e: NexusEvent) => e.mission_id === mid && (e.type.startsWith("submission") || e.type === "task.accepted" || e.type === "evidence.registered"), [mid]);
  const q = useApi<any[]>(`/api/missions/${mid}/artifacts${kind === "all" ? "" : `?kind=${kind}`}`, match, [kind]);
  const [sel, setSel] = useState<string | null>(null);
  const [cmp, setCmp] = useState<string | null>(null);
  return (
    <div className="grid" style={{ gridTemplateColumns: "minmax(380px, 1fr) minmax(0, 1.6fr)" }}>
      <Card title="Versions" id="art-list" actions={
        <select aria-label="Artifact kind" value={kind} onChange={(e) => { setKind(e.target.value); setSel(null); setCmp(null); }} style={{ width: 180 }}>
          <option value="all">all kinds</option>{KINDS.map((k) => <option key={k} value={k}>{k.replace(/_/g, " ")}</option>)}</select>} flush>
        <Load q={q} empty={(d) => (d.length ? null : "No artifacts of this kind yet")}>{(rows) => (
          <table className="data"><thead><tr><th>Artifact</th><th>Status</th><th>Compare</th></tr></thead><tbody>
            {rows.slice().reverse().map((a) => (
              <tr key={a.id} className="clickable" aria-selected={sel === a.id} onClick={() => setSel(a.id)} data-testid="artifact-row">
                <td><div className="small">{a.title}</div><div className="faint xs mono">{a.task_key ?? "—"} · {shortId(a.id)} · {a.sha256.slice(0, 10)} · {timeAgo(a.created_at)}</div></td>
                <td>{a.acceptance ? (a.acceptance.is_current ? <Status value="accepted" label="accepted · current" /> : <Status value="stale" label="accepted · historical" />)
                  : a.submission_status ? <Status value={a.submission_status} /> : <Badge>{a.kind.replace(/_/g, " ")}</Badge>}</td>
                <td><input type="checkbox" aria-label="compare with selected" checked={cmp === a.id} onClick={(e) => e.stopPropagation()} onChange={(e) => setCmp(e.target.checked ? a.id : null)} /></td>
              </tr>))}
          </tbody></table>
        )}</Load>
      </Card>
      {sel ? <Viewer id={sel} compareWith={cmp && cmp !== sel ? cmp : null} /> : <Card title="Select an artifact"><div className="small muted">Artifacts are immutable and addressed by SHA-256. Every accepted result keeps its exact inputs, evidence and reviews. Tick “compare” on a second version to see a diff.</div></Card>}
    </div>
  );
}

function Viewer({ id, compareWith }: { id: string; compareWith: string | null }) {
  const q = useApi<any>(`/api/artifacts/${id}`, undefined, [id]);
  const [other, setOther] = useState<any>(null);
  useEffect(() => { setOther(null); if (compareWith) void get(`/api/artifacts/${compareWith}`).then(setOther); }, [compareWith]);
  const diff = useMemo(() => (other && q.data ? lineDiff(other.content ?? "", q.data.content ?? "") : null), [other, q.data]);
  return (
    <Card title="Artifact" id="art-view">
      <Load q={q}>{(a) => (
        <div className="col">
          <KV items={[["Title", a.meta.title], ["Kind", a.meta.kind], ["SHA-256", <Mono>{a.meta.sha256}</Mono>], ["Integrity", <Badge tone="ok">{a.integrity}</Badge>],
            ["Created by", <Mono>{a.meta.created_by}</Mono>], ["Mission revision", a.meta.mission_revision ?? "—"],
            ["Input versions", Object.keys(a.meta.input_versions).length ? <Mono>{JSON.stringify(a.meta.input_versions)}</Mono> : "none"],
            ["Lineage", Object.keys(a.meta.lineage).length ? <Mono>{JSON.stringify(a.meta.lineage).slice(0, 300)}</Mono> : "—"]]} />
          {diff ? (
            <><h4>Diff: {other.meta.title} ({other.meta.sha256.slice(0, 8)}) → this ({a.meta.sha256.slice(0, 8)})</h4>
              <pre className="pre" aria-label="diff">{diff.map((d, i) => <span key={i} className={`diff-${d.kind}`}>{d.kind === "add" ? "+ " : d.kind === "del" ? "- " : "  "}{d.text}</span>)}</pre></>
          ) : a.content === null ? <div className="small muted">Binary content not shown.</div>
            : a.meta.media_type.includes("markdown") ? <Doc text={a.content} /> : <pre className="pre">{a.content}</pre>}
          {a.truncated && <div className="small muted">Content truncated for display.</div>}
        </div>
      )}</Load>
    </Card>
  );
}
