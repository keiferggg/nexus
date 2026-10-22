import { useCallback, useMemo, useState } from "react";
import { get, NexusEvent, shortId, useApi } from "../../api";
import { Badge, Button, Card, Drawer, Load, Mono, Status } from "../../components/ui";

const GROUPS: Record<string, (t: string) => boolean> = {
  all: () => true,
  decisions: (t) => /^(task\.accepted|review\.|submission\.|decision|objection|plan\.|strategy\.|mission\.revised)/.test(t),
  execution: (t) => /^(attempt\.|model_call\.|tool\.|task\.transition)/.test(t),
  evidence: (t) => /^(evidence|claim|uncertainty|prediction|task\.invalidated)/.test(t),
  messages: (t) => t.startsWith("message."),
  budget: (t) => t.startsWith("budget.") || t.startsWith("model_call."),
};

export default function Timeline({ m }: { m: any }) {
  const mid = m.mission.id;
  const match = useCallback((e: NexusEvent) => e.mission_id === mid, [mid]);
  const q = useApi<any>(`/api/missions/${mid}/timeline`, match);
  const [group, setGroup] = useState("decisions");
  const [replay, setReplay] = useState<string | null>(null);
  const tasks = useMemo(() => Object.fromEntries((m.tasks as any[]).map((t) => [t.id, t])), [m.tasks]);
  return (
    <div className="grid two">
      <Card title="Causal event history" id="tl" flush actions={
        <select aria-label="Event filter" value={group} onChange={(e) => setGroup(e.target.value)} style={{ width: 160 }}>
          {Object.keys(GROUPS).map((g) => <option key={g}>{g}</option>)}</select>}>
        <Load q={q}>{(d) => {
          const evs = d.events.filter((e: any) => GROUPS[group](e.type));
          if (!evs.length) return <div className="state">No events in this view.</div>;
          return <div role="log" aria-label="events">{evs.slice(-400).map((e: any) => (
            <div key={e.seq} className="timeline-row" data-testid="timeline-row">
              <span className="mono faint">#{e.seq}</span>
              <span className="mono">{e.type}</span>
              <span className="small">{e.task_id && <Badge>{tasks[e.task_id]?.key ?? shortId(e.task_id)}</Badge>} {describe(e)}
                <span className="faint"> · {e.actor}{e.causal_parent ? ` · caused by ${shortId(e.causal_parent)}` : ""}</span></span>
            </div>))}</div>;
        }}</Load>
      </Card>
      <div className="col gap-lg">
        <Card title="Checkpoints" id="ck" flush>
          <Load q={q}>{(d) => d.checkpoints.length === 0 ? <div className="state">No framework checkpoints recorded.</div> : (
            <table className="data"><thead><tr><th>Task</th><th>Attempt</th><th>Checkpoint</th><th>Pending</th><th /></tr></thead><tbody>
              {d.checkpoints.slice(-40).reverse().map((c: any) => <tr key={c.id}><td className="small">{tasks[c.task_id]?.key}</td><td>#{c.attempt_no}</td>
                <td className="mono small">{c.checkpoint_id.slice(0, 8)}<div className="faint">{c.source}</div></td><td className="small">{JSON.parse(c.pending_request_ids_json).length ? "submission awaiting NEXUS" : "—"}</td>
                <td><Button size="sm" onClick={() => setReplay(c.attempt_id)}>Replay</Button></td></tr>)}
            </tbody></table>)}</Load>
        </Card>
        <Card title="Replay and branches" id="rb">
          <div className="small col">
            <div><strong>Replay</strong> shows the recorded model outputs and tool results of an attempt in order. It makes no model calls and changes nothing.</div>
            <div><strong>New branch</strong> (from a task's drawer) reopens the task in a new epoch and runs it again with live calls. Rewinding NEXUS state never undoes external effects.</div>
          </div>
        </Card>
      </div>
      {replay && <ReplayDrawer attemptId={replay} onClose={() => setReplay(null)} />}
    </div>
  );
}

function describe(e: any): string {
  const p = e.payload ?? {};
  if (e.type === "task.transition") return `${p.from} → ${p.to}: ${p.reason}`;
  if (e.type === "review.recorded") return `${p.criterion} ${p.check} → ${p.verdict} (${p.independence}) ${p.summary ?? ""}`;
  if (e.type === "model_call.started") return `${p.provider}:${p.model} ${p.purpose} · reserved $${p.reserved_usd}`;
  if (e.type === "model_call.finished") return `${p.status}${p.error_kind ? ` (${p.error_kind})` : ""}${p.actual_usd != null ? ` · $${Number(p.actual_usd).toFixed(5)}` : ""}${p.note ? ` · ${p.note}` : ""}`;
  if (e.type.startsWith("tool.")) return `${p.tool}: ${p.reason ?? ""}`;
  if (e.type === "message.posted") return `${p.type}: ${p.body}`;
  if (e.type === "attempt.started") return `attempt #${p.attempt_no} token ${p.fencing_token}${p.resumed_from_checkpoint ? " (resumed from checkpoint)" : ""}`;
  if (e.type === "attempt.abandoned" || e.type === "attempt.fenced") return p.reason;
  return Object.entries(p).slice(0, 4).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v).slice(0, 60) : v}`).join(" ");
}

function ReplayDrawer({ attemptId, onClose }: { attemptId: string; onClose: () => void }) {
  const q = useApi<any>(`/api/attempts/${attemptId}/replay`);
  const [ctx, setCtx] = useState<string | null>(null);
  return (
    <Drawer title={`Replay attempt ${shortId(attemptId)}`} onClose={onClose}>
      <Load q={q}>{(r) => (
        <>
          <Badge tone="info">{r.mode}</Badge>
          {r.steps.map((s: any, i: number) => (
            <div key={i} className="card"><div className="card-head"><Badge>{s.type}</Badge><Mono>{s.operation_id.split(":").slice(1).join(":")}</Mono><span className="grow" /><Status value={s.status} /></div>
              <div className="card-body">
                {s.type === "model_call" ? (
                  <>{s.output?.tool_calls?.length ? <div className="small">tool calls: {s.output.tool_calls.map((t: any) => t.name).join(", ")}</div> : null}
                    {s.output?.text && <pre className="pre" style={{ maxHeight: 200 }}>{s.output.text.slice(0, 3000)}</pre>}
                    {s.context_artifact_id && <Button size="sm" onClick={async () => setCtx((await get(`/api/artifacts/${s.context_artifact_id}`)).content)}>View supplied context</Button>}</>
                ) : (
                  <div className="small"><span className="mono">{s.tool}</span> {s.reason && <span className="faint">— {s.reason}</span>}
                    <pre className="pre" style={{ maxHeight: 140 }}>{JSON.stringify(s.args, null, 1)}</pre></div>
                )}
              </div></div>))}
          {ctx && <><h4>Supplied context packet (secrets are never included)</h4><pre className="pre">{ctx}</pre></>}
        </>
      )}</Load>
    </Drawer>
  );
}
