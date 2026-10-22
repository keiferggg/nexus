import { useCallback, useState } from "react";
import { fmtUsd, NexusEvent, post, shortId, timeAgo, useApi } from "../../api";
import { Badge, Button, Card, Doc, Drawer, Empty, KV, Load, Mono, Status } from "../../components/ui";

export default function Control({ m, reload }: { m: any; reload: () => void }) {
  const [openTask, setOpenTask] = useState<string | null>(null);
  const attention = useApi<any[]>(`/api/attention?mission_id=${m.mission.id}`, (e) => e.mission_id === m.mission.id);
  const tasks = m.tasks as any[];
  const byId = Object.fromEntries(tasks.map((t) => [t.id, t]));
  const scs = m.revision.success_conditions as any[];
  const active = tasks.filter((t) => ["leased", "running", "submitted", "verifying"].includes(t.status));
  const final = m.final_artifact_id;
  return (
    <div className="grid two">
      <div className="col gap-lg">
        <Card title="Success conditions" id="sc-h">
          <table className="data"><tbody>{scs.map((sc) => {
            const ts = tasks.filter((t) => t.cause?.addresses === sc.id && !["superseded", "canceled"].includes(t.status));
            const done = ts.length > 0 && ts.every((t) => t.status === "accepted" && !t.is_stale);
            return (
              <tr key={sc.id}><td className="mono small" style={{ width: 40 }}>{sc.id}</td><td>{sc.text}</td>
                <td style={{ width: 140 }}>{ts.length === 0 ? <Badge>not planned yet</Badge> : <Status value={done ? "accepted" : ts.some((t) => t.status === "failed") ? "failed" : "running"} label={done ? "met (verified)" : `${ts.filter((t) => t.status === "accepted").length}/${ts.length} accepted`} />}</td></tr>
            );
          })}</tbody></table>
          {m.revision.constraints.length > 0 && (
            <div style={{ marginTop: 10 }}><h4>Constraints</h4>
              <ul className="small" style={{ margin: "4px 0 0", paddingLeft: 18 }}>{m.revision.constraints.map((c: any) => <li key={c.id}><span className="mono faint">{c.id}</span> {c.text}</li>)}</ul></div>
          )}
        </Card>
        <Card title={`Plan · ${tasks.length} task(s)`} id="plan-h" flush>
          {tasks.length === 0 ? <Empty title="No tasks yet" /> : (
            <table className="data" aria-label="Tasks">
              <thead><tr><th>Task</th><th>Agent</th><th>Doing now</th><th>Status</th><th>Strategy</th><th>Attempts</th></tr></thead>
              <tbody>{tasks.map((t) => (
                <tr key={t.id} className="clickable" tabIndex={0} aria-selected={openTask === t.id} onClick={() => setOpenTask(t.id)}
                    onKeyDown={(e) => e.key === "Enter" && setOpenTask(t.id)} data-testid={`task-${t.key}`}>
                  <td><div>{t.title}</div><div className="small faint mono">{t.key} · {t.kind}{t.depends_on.length ? ` · after ${t.depends_on.map((d: string) => byId[d]?.key).join(", ")}` : ""}</div></td>
                  <td className="small">{t.agent ? `${t.agent.name}` : "—"}<div className="faint">{t.agent?.role}</div></td>
                  <td className="small">{t.activity}</td>
                  <td><Status value={t.status} />{t.is_stale ? <> <Badge tone="warn" title={t.stale_reason ?? ""}>reopened</Badge></> : null}</td>
                  <td className="small">{t.strategy?.mode ?? "—"}</td>
                  <td className="small">{t.attempt_count}/{t.max_attempts}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </Card>
        {final && <FinalDeliverable artifactId={final} />}
      </div>
      <div className="col gap-lg">
        <Card title="Needs your attention" id="attn-h">
          <Load q={attention} empty={(d) => (d.length ? null : "Nothing needs you")}>{(items) => (
            <div className="col">{items.map((a, i) => <AttentionItem key={i} a={a} onOpen={() => a.task_id && setOpenTask(a.task_id)} reload={() => { attention.reload(); reload(); }} />)}</div>
          )}</Load>
        </Card>
        <Card title="Active now" id="active-h">
          {active.length === 0 ? <div className="small muted">No agent is working. Idle agents consume no tokens.</div> : (
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>{active.map((t) => <li key={t.id}><strong>{t.agent?.name ?? "agent"}</strong> — {t.activity} on <em>{t.title}</em></li>)}</ul>)}
          <div className="small faint" style={{ marginTop: 8 }}>Limits: {m.mission.max_active_workers} active workers, {m.mission.max_branches} exploratory branches, {m.mission.max_revision_rounds} repair rounds per attempt.</div>
        </Card>
        <Card title="Spend by category" id="spend-h">
          <table className="data"><thead><tr><th>Category</th><th>Actual</th><th>Reserved</th><th>Unresolved</th></tr></thead>
            <tbody>{Object.entries(m.budget.by_category).map(([k, v]: any) => <tr key={k}><td>{k}</td><td>{fmtUsd(v.actual_usd)}</td><td>{fmtUsd(v.reserved_usd)}</td><td>{fmtUsd(v.unresolved_usd)}</td></tr>)}
              {Object.keys(m.budget.by_category).length === 0 && <tr><td colSpan={4} className="muted">No model calls yet</td></tr>}</tbody></table>
          <div className="small faint" style={{ marginTop: 6 }}>{m.budget.note}</div>
        </Card>
      </div>
      {openTask && <TaskDrawer taskId={openTask} onClose={() => setOpenTask(null)} />}
    </div>
  );
}

function AttentionItem({ a, onOpen, reload }: { a: any; onOpen: () => void; reload: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState("");
  async function review(verdict: "pass" | "fail") {
    try { await post(`/api/submissions/${a.submission_id}/owner-review`, { criterion: a.criterion, verdict, note: note || `owner ${verdict}` }); reload(); }
    catch (e) { setErr((e as Error).message); }
  }
  return (
    <div className={`callout ${a.kind.includes("failed") || a.kind === "blocking_objection" ? "bad" : "warn"}`} data-testid="attention-item">
      <div className="row"><strong className="grow">{a.title}</strong><Badge tone="warn">{a.kind.replace(/_/g, " ")}</Badge></div>
      {a.detail && <div className="small muted">{a.detail}</div>}
      <div className="row wrap" style={{ marginTop: 6 }}>
        {a.task_id && <Button size="sm" onClick={onOpen}>Inspect</Button>}
        {a.kind === "owner_approval" && (
          <>
            <input aria-label="approval note" placeholder="note (e.g. how you verified)" value={note} onChange={(e) => setNote(e.target.value)} style={{ width: 220 }} />
            <Button size="sm" variant="primary" onClick={() => review("pass")}>Approve {a.criterion}</Button>
            <Button size="sm" variant="danger" onClick={() => review("fail")}>Reject</Button>
          </>
        )}
        {a.kind === "action_approval" && (
          <>
            <pre className="pre" style={{ maxHeight: 80, width: "100%" }}>{JSON.stringify(a.args, null, 1)}</pre>
            <Button size="sm" variant="primary" onClick={async () => { try { await post(`/api/actions/${a.action_request_id}/approve`, { note }); reload(); } catch (e) { setErr((e as Error).message); } }}>Approve & run</Button>
            <Button size="sm" variant="danger" onClick={async () => { try { await post(`/api/actions/${a.action_request_id}/deny`, { note }); reload(); } catch (e) { setErr((e as Error).message); } }}>Deny</Button>
            <input aria-label="decision note" placeholder="note" value={note} onChange={(e) => setNote(e.target.value)} style={{ width: 180 }} />
          </>
        )}
        {a.kind === "action_uncertain" && <Button size="sm" onClick={async () => { try { await post(`/api/actions/${a.action_request_id}/reconcile`); reload(); } catch (e) { setErr((e as Error).message); } }}>Ask the service what happened</Button>}
        {a.kind === "task_blocked" && a.submission_id && (
          <>
            <input aria-label="approval note" placeholder="note (e.g. I read it and it's correct)" value={note} onChange={(e) => setNote(e.target.value)} style={{ width: 220 }} />
            <Button size="sm" variant="primary" onClick={() => review("pass")}>Approve it myself</Button>
          </>
        )}
        {(a.kind === "task_blocked" || a.kind === "task_failed") && <Button size="sm" onClick={async () => { try { await post(`/api/tasks/${a.task_id}/retry`); reload(); } catch (e) { setErr((e as Error).message); } }}>Retry task</Button>}
      </div>
      {err && <div className="small" style={{ color: "var(--bad)" }} role="alert">{err}</div>}
    </div>
  );
}

function FinalDeliverable({ artifactId }: { artifactId: string }) {
  const q = useApi<any>(`/api/artifacts/${artifactId}`);
  return (
    <Card title="Accepted final deliverable" id="final-h" actions={<Badge tone="ok">verified</Badge>}>
      <Load q={q}>{(a) => (
        <div className="col">
          <div className="small faint">sha256 <Mono>{a.meta.sha256.slice(0, 16)}…</Mono> · {a.integrity}</div>
          {a.meta.media_type.includes("markdown") ? <Doc text={a.content ?? ""} /> : <pre className="pre">{a.content}</pre>}
        </div>
      )}</Load>
    </Card>
  );
}

export function TaskDrawer({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const match = useCallback((e: NexusEvent) => e.task_id === taskId, [taskId]);
  const q = useApi<any>(`/api/tasks/${taskId}`, match);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Drawer title={q.data ? q.data.title : "Task"} onClose={onClose} actions={q.data && (
      <div className="btn-group">
        {["blocked", "failed"].includes(q.data.status) && <Button size="sm" onClick={async () => { try { await post(`/api/tasks/${taskId}/retry`); q.reload(); } catch (e) { setErr((e as Error).message); } }}>Retry</Button>}
        {["accepted", "failed", "blocked"].includes(q.data.status) && <Button size="sm" onClick={async () => {
          if (!confirm("Start a new branch? The task reopens in a new epoch and runs again with live calls (costs apply). The current result stays in history.")) return;
          try { await post(`/api/tasks/${taskId}/branch`); q.reload(); } catch (e) { setErr((e as Error).message); } }}>New branch</Button>}
      </div>)}>
      <Load q={q}>{(t) => (
        <>
          {err && <div className="callout bad">{err}</div>}
          <KV items={[["Status", <><Status value={t.status} /> <span className="small muted">{t.status_reason}</span></>], ["Key", <Mono>{t.key}</Mono>],
            ["Kind / role", `${t.kind} / ${t.role}`], ["Epoch · revision", `${t.epoch} · r${t.mission_revision}`],
            ["Strategy", t.strategy?.mode ? `${t.strategy.mode} — ${t.strategy.why ?? ""}` : "—"],
            ["Switch trigger", t.strategy?.switch_if ?? "—"]]} />
          <section><h4>Why is this running?</h4>
            <div className="chain">{t.why.map((c: any, i: number) => <div key={i} className="chain-item"><span className="muted">{c.kind.replace(/_/g, " ")}</span>
              <span>{c.text ?? <Mono>{c.ref}</Mono>}{c.switch_if ? <span className="faint"> · switch if: {c.switch_if}</span> : null}</span></div>)}</div></section>
          <section><h4>Acceptance criteria & verification plan</h4>
            <table className="data"><thead><tr><th>Criterion</th><th>Check</th><th>Verifier</th><th>Independence</th><th>Limitations</th></tr></thead>
              <tbody>{t.verification_plan.map((v: any) => <tr key={v.criterion}><td><Mono>{v.criterion}</Mono> {v.text}</td><td className="mono small">{v.check.kind}</td>
                <td className="small">{v.verifier}</td><td className="small">{v.independence}</td><td className="small faint">{v.limitations}</td></tr>)}</tbody></table></section>
          <section><h4>Submissions & reviews</h4>
            {t.submissions.length === 0 ? <div className="small muted">No submission yet.</div> : t.submissions.map((s: any) => (
              <div key={s.id} className="card" style={{ marginBottom: 8 }}>
                <div className="card-head"><span className="small">Round {s.round} · attempt {shortId(s.attempt_id)} · token {s.fencing_token}</span><span className="grow" /><Status value={s.status} /></div>
                <div className="card-body">
                  {s.status_reason && <div className="small muted">{s.status_reason}</div>}
                  <table className="data"><tbody>{s.reviews.map((r: any) => (
                    <tr key={r.id}><td style={{ width: 70 }}><Status value={r.verdict} /></td><td className="small"><Mono>{r.criterion}</Mono> {r.check_kind}<div className="faint">{r.reviewer} · {r.independence}{r.blocking ? "" : " · non-blocking"}</div></td>
                      <td className="small">{r.findings?.summary}{r.limitations && <div className="faint">limitation: {r.limitations}</div>}</td></tr>))}</tbody></table>
                </div>
              </div>))}</section>
          <section><h4>Attempts</h4>
            <table className="data"><thead><tr><th>#</th><th>Status</th><th>Token</th><th>Resumed from</th><th>Started</th><th>Error</th></tr></thead>
              <tbody>{t.attempts.map((a: any) => <tr key={a.id}><td>{a.attempt_no}</td><td><Status value={a.status} /></td><td>{a.fencing_token}</td>
                <td className="mono small">{a.resumed_from_checkpoint ? shortId(a.resumed_from_checkpoint) : "—"}</td><td className="small">{timeAgo(a.started_at)}</td><td className="small faint">{a.error}</td></tr>)}</tbody></table></section>
          <section><h4>Model calls</h4>
            <table className="data"><thead><tr><th>Purpose</th><th>Model</th><th>Status</th><th>Tokens in/out</th><th>Cost</th></tr></thead>
              <tbody>{t.model_calls.map((c: any) => <tr key={c.id}><td className="small">{c.purpose}</td><td className="mono small">{c.provider_kind}:{c.model}</td>
                <td><Status value={c.status} />{c.error_kind && <span className="small faint"> {c.error_kind}</span>}</td>
                <td className="small">{c.input_tokens ?? "?"}/{c.output_tokens ?? "?"}{!c.usage_reported && c.status === "succeeded" ? <Badge tone="warn">no usage reported</Badge> : null}</td>
                <td className="small">{fmtUsd(c.actual_usd)}</td></tr>)}</tbody></table></section>
          <section><h4>Tool operations</h4>
            {t.tool_operations.length === 0 ? <div className="small muted">None.</div> : (
              <table className="data"><tbody>{t.tool_operations.map((o: any) => <tr key={o.id}><td><Status value={o.status} /></td><td className="mono small">{o.tool}</td>
                <td className="small">{o.effect_class}</td><td className="small faint">{o.decision_reason}<div className="mono">{JSON.stringify(o.args).slice(0, 160)}</div></td></tr>)}</tbody></table>)}</section>
        </>
      )}</Load>
    </Drawer>
  );
}
