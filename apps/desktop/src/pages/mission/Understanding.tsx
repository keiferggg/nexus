import { useCallback, useState } from "react";
import { NexusEvent, post, timeAgo, useApi } from "../../api";
import { Badge, Button, Card, Dialog, Field, Load, Mono, Status } from "../../components/ui";

const SEEN_KEY = (mid: string) => `nexus.seen.${mid}`;

export default function Understanding({ missionId }: { missionId: string }) {
  const [since] = useState<number>(() => {
    try { return Number(localStorage.getItem(SEEN_KEY(missionId)) || 0); } catch { return 0; }
  });
  const match = useCallback((e: NexusEvent) => e.mission_id === missionId && /^(evidence|claim|uncertainty|assumption|decision|objection|prediction|plan|strategy|task\.invalidated|mission\.revised)/.test(e.type), [missionId]);
  const q = useApi<any>(`/api/missions/${missionId}/understanding?since=${since}`, match);
  const [withdraw, setWithdraw] = useState<any>(null);

  function markSeen(changes: any[]) {
    const last = changes.length ? changes[changes.length - 1].seq : since;
    try { localStorage.setItem(SEEN_KEY(missionId), String(last)); } catch { /* ignore */ }
  }

  return (
    <Load q={q}>{(u) => (
      <div className="col gap-lg">
        <Card title={`Changes since your last checkpoint (${u.changes.length})`} id="chg" actions={u.changes.length ? <Button size="sm" onClick={() => markSeen(u.changes)}>Mark as seen</Button> : undefined}>
          {u.changes.length === 0 ? <div className="small muted">Nothing new since you last marked this mission as seen.</div> : (
            <table className="data"><tbody>{u.changes.slice(-25).map((c: any) => <tr key={c.seq}><td className="mono small" style={{ width: 150 }}>{c.type}</td>
              <td className="small">{summarize(c)}</td><td className="small faint nowrap">{timeAgo(c.at)}</td></tr>)}</tbody></table>)}
        </Card>
        <div className="grid halves">
          <Card title={`Evidence by source lineage (${u.evidence.length} items, ${u.lineages.length} lineages)`} id="ev" flush>
            {u.evidence.length === 0 ? <div className="state">No evidence recorded yet.</div> : (
              <table className="data"><thead><tr><th>Lineage</th><th>Item</th><th>Status</th><th /></tr></thead><tbody>
                {u.lineages.flatMap((l: any) => l.evidence_ids.map((id: string, i: number) => {
                  const e = u.evidence.find((x: any) => x.id === id);
                  return <tr key={id}>{i === 0 && <td rowSpan={l.count} className="small"><Mono>{l.lineage_root}</Mono>
                    {l.count > 1 && <div><Badge tone="warn" title="These items share one origin and count as ONE independent source">{l.count} items · 1 independent source</Badge></div>}</td>}
                    <td className="small">{e.title}<div className="faint mono">{e.locator}</div></td><td><Status value={e.status} />{e.status_reason && <div className="small faint">{e.status_reason}</div>}</td>
                    <td>{e.status === "active" && <Button size="sm" onClick={() => setWithdraw(e)}>Withdraw…</Button>}</td></tr>;
                }))}
              </tbody></table>)}
          </Card>
          <Card title={`Claims (${u.claims.length})`} id="claims" flush>
            {u.claims.length === 0 ? <div className="state">No claims yet.</div> : (
              <table className="data"><thead><tr><th>Proposition</th><th>Status</th><th>Basis</th></tr></thead><tbody>
                {u.claims.map((c: any) => <tr key={c.id}><td className="small">{c.proposition}</td><td><Status value={c.status} />{c.is_stale ? <Badge tone="warn">stale</Badge> : null}</td>
                  <td className="small faint">{c.confidence_basis}{c.supports.length > 1 && c.independent_lineages < 2 ? <div style={{ color: "var(--warn)" }}>multiple items, one lineage</div> : null}</td></tr>)}
              </tbody></table>)}
          </Card>
        </div>
        <div className="grid halves">
          <Card title="Uncertainties (ranked)" id="unc" flush>
            {u.uncertainties.length === 0 ? <div className="state">No open uncertainties recorded.</div> : (
              <table className="data"><thead><tr><th>Question</th><th>Kind</th><th>Would change</th><th>Rank</th></tr></thead><tbody>
                {u.uncertainties.map((x: any) => <tr key={x.id}><td className="small">{x.question}<div><Status value={x.status} /></div></td><td className="small">{x.kind.replace(/_/g, " ")}</td>
                  <td className="small">{x.would_change}</td><td className="small" title={x.explanation}>{x.score ?? "—"}<div className="faint">{x.explanation}</div></td></tr>)}
              </tbody></table>)}
            <div className="small faint" style={{ padding: "6px 10px" }}>Ranking uses transparent points (dependency impact, kind, cheapness, urgency) — not probabilities.</div>
          </Card>
          <Card title="Assumptions & decisions" id="ad">
            <h4>Assumptions</h4>
            {u.assumptions.length === 0 ? <div className="small muted">None recorded.</div> : <ul className="small" style={{ paddingLeft: 18 }}>{u.assumptions.map((a: any) => <li key={a.id}>{a.statement} <span className="faint">({a.basis})</span> <Status value={a.status} /></li>)}</ul>}
            <h4 style={{ marginTop: 10 }}>Decisions</h4>
            {u.decisions.length === 0 ? <div className="small muted">None recorded.</div> : <ul className="small" style={{ paddingLeft: 18 }}>{u.decisions.map((d: any) => <li key={d.id}><strong>{d.title}</strong>: {d.choice} — {d.rationale} <Status value={d.status} /></li>)}</ul>}
          </Card>
        </div>
        <div className="grid halves">
          <Card title="Predictions vs outcomes" id="pred" flush>
            {u.predictions.length === 0 ? <div className="state">No predictions recorded.</div> : (
              <table className="data"><thead><tr><th>Prediction</th><th>Expected</th><th>Observed</th><th>Result</th></tr></thead><tbody>
                {u.predictions.map((p: any) => <tr key={p.id}><td className="small">{p.statement}<div className="faint mono">{p.observable}</div></td><td className="mono small">{p.expected_json}</td>
                  <td className="mono small">{p.observed_json ?? "—"}</td><td><Status value={p.status} />{p.diagnosis && <div className="small faint">{p.diagnosis.replace(/_/g, " ")}</div>}</td></tr>)}
              </tbody></table>)}
          </Card>
          <Card title="Proposals & objections" id="prop" flush>
            {u.proposals.length === 0 ? <div className="state">None.</div> : (
              <table className="data"><tbody>{u.proposals.map((p: any) => <tr key={p.id}><td><Badge tone={p.kind === "objection" ? "bad" : "info"}>{p.kind}</Badge></td>
                <td className="small">{p.summary}<div className="faint">Mechanism: {p.mechanism ?? "—"} · Resolved when: {p.resolution_condition}</div></td><td><Status value={p.status} />{p.blocking ? <Badge tone="warn">blocking</Badge> : null}</td></tr>)}</tbody></table>)}
          </Card>
        </div>
        {withdraw && <WithdrawDialog e={withdraw} onClose={() => { setWithdraw(null); q.reload(); }} />}
      </div>
    )}</Load>
  );
}

function summarize(c: any): string {
  const p = c.payload ?? {};
  switch (c.type) {
    case "evidence.changed": return `Evidence ${p.status}: ${p.reason} — ${p.affected?.tasks?.length ?? 0} task(s) reopened, ${p.affected?.claims?.length ?? 0} claim(s) updated`;
    case "claim.added": return `${p.status}: ${p.proposition}`;
    case "task.invalidated": return `Task reopened: ${p.reason}`;
    case "mission.revised": return `Revision ${p.revision}: ${p.reason}`;
    case "plan.changed": return `${p.task_key}: ${p.reason} → ${p.strategy}`;
    case "prediction.surprise": return `Surprise: ${p.statement} (expected ${JSON.stringify(p.expected)}, got ${JSON.stringify(p.observed)}; ${p.diagnosis})`;
    case "strategy.switched": return `${p.task_key}: ${p.from} → ${p.to} (${p.trigger})`;
    case "objection.raised": return `Objection: ${p.summary}`;
    default: return JSON.stringify(p).slice(0, 160);
  }
}

function WithdrawDialog({ e, onClose }: { e: any; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Dialog title={`Withdraw evidence: ${e.title}`} onClose={onClose} footer={res ? <Button onClick={onClose}>Close</Button> : <>
      <Button onClick={onClose}>Cancel</Button>
      <Button variant="danger" disabled={!reason.trim()} onClick={async () => { try { setRes(await post(`/api/evidence/${e.id}/change`, { status: "withdrawn", reason })); } catch (x) { setErr((x as Error).message); } }}>Withdraw</Button></>}>
      {res ? <div className="small col"><div className="callout info">Withdrawn. Only dependents were affected.</div>
        <div>Claims updated: {res.claims.length} · Tasks reopened for re-verification: {res.tasks.length} · Decisions marked stale: {res.decisions.length}</div></div> : (
        <>
          <div className="small muted">Claims and results that depend on this evidence become stale and are re-verified. Unrelated work is untouched. History is kept.</div>
          <Field label="Reason (recorded)" htmlFor="wr"><input id="wr" value={reason} onChange={(x) => setReason(x.target.value)} /></Field>
          {err && <div className="callout bad">{err}</div>}
        </>)}
    </Dialog>
  );
}
