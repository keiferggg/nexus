import { useEffect, useState } from "react";
import { post, put, timeAgo, useApi } from "../api";
import { Badge, Button, Card, Field, Load, Status } from "../components/ui";

export default function Objectives({ project, openMission }: { project: any; openMission: (id: string) => void }) {
  const goals = useApi<any[]>(`/api/projects/${project.id}/goals`, (e) => e.type === "goals.changed");
  const props = useApi<any[]>(`/api/projects/${project.id}/objectives`, (e) => e.type.startsWith("objective."));
  const settings = useApi<any>("/api/settings", (e) => e.type === "settings.changed");
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Initialize from saved goals only until the owner starts typing (never overwrite their edit).
  useEffect(() => { if (goals.data && !dirty) setText(goals.data.map((g) => g.text).join("\n")); }, [goals.data, dirty]);
  const pol = settings.data?.["objectives.policy"] ?? { auto_adopt: false, goal_ids: [], max_cost_usd: 0 };

  return (
    <div className="page">
      <div className="page-head"><div className="titles"><h1>Goals & objectives</h1>
        <div className="sub">Standing goals are yours. Agents may discover objectives that serve them; every proposal must name the goal it advances.</div></div></div>
      <div className="grid two">
        <div className="col gap-lg">
          <Card title="Proposed objectives" id="po" flush>
            <Load q={props} empty={(d) => (d.length ? null : "No proposals yet — run discovery")}>{(rows) => (
              <table className="data"><tbody>{rows.map((o) => <ProposalRow key={o.id} o={o} goals={goals.data ?? []} reload={props.reload} openMission={openMission} />)}</tbody></table>
            )}</Load>
          </Card>
        </div>
        <div className="col gap-lg">
          <Card title="Standing goals" id="sg">
            <div className="col">
              <Field label="One goal per line" htmlFor="gl" hint="Goal ids (G1, G2…) stay stable when you edit text."><textarea id="gl" value={text} onChange={(e) => { setDirty(true); setText(e.target.value); }} /></Field>
              <div className="small">{(goals.data ?? []).map((g) => <div key={g.id}><Badge>{g.id}</Badge> {g.text}</div>)}</div>
              {err && <div className="callout bad">{err}</div>}
              <div className="btn-group">
                <Button onClick={async () => { try { await put(`/api/projects/${project.id}/goals`, { goals: text.split("\n") }); setDirty(false); goals.reload(); } catch (e) { setErr((e as Error).message); } }}>Save goals</Button>
                <Button variant="primary" disabled={!(goals.data ?? []).length} onClick={async () => {
                  try { const m = await post(`/api/projects/${project.id}/discover`, { execution_mode: "offline_fake" }); openMission(m.mission.id); }
                  catch (e) { setErr((e as Error).message); } }}>Run discovery (offline)</Button>
              </div>
              <div className="small faint">Discovery runs as a normal mission: the strategist reads sources and read-only services, and its proposals are verified (goal link, evidence) before they reach you.</div>
            </div>
          </Card>
          <Card title="Auto-adoption policy" id="ap">
            <div className="col small">
              <label className="row"><input type="checkbox" checked={!!pol.auto_adopt} onChange={async (e) => { await put("/api/settings/objectives.policy", { value: { ...pol, auto_adopt: e.target.checked } }); settings.reload(); }} />
                Let agents start missions for proposals that fit this policy</label>
              <div>Only for goals: {(goals.data ?? []).map((g) => (
                <label key={g.id} className="row" style={{ display: "inline-flex", marginRight: 10 }}><input type="checkbox" checked={(pol.goal_ids ?? []).includes(g.id)}
                  onChange={async (e) => { const ids = new Set(pol.goal_ids ?? []); e.target.checked ? ids.add(g.id) : ids.delete(g.id);
                    await put("/api/settings/objectives.policy", { value: { ...pol, goal_ids: [...ids] } }); settings.reload(); }} />{g.id}</label>))}</div>
              <div className="row">Max estimated cost $<input aria-label="max cost" type="number" min={0} step={0.01} defaultValue={pol.max_cost_usd ?? 0} style={{ width: 90 }}
                onBlur={async (e) => { await put("/api/settings/objectives.policy", { value: { ...pol, max_cost_usd: Number(e.target.value) } }); settings.reload(); }} /></div>
              <div className="faint">Auto-adopted missions run offline unless you also set a live spend cap in the policy. Off by default.</div>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

function ProposalRow({ o, goals, reload, openMission }: { o: any; goals: any[]; reload: () => void; openMission: (id: string) => void }) {
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const g = goals.find((x) => x.id === o.serves_goal);
  return (
    <tr data-testid="objective-row"><td>
      <div className="row"><strong className="grow">{o.title}</strong><Status value={o.status} /></div>
      <div className="small">{o.objective}</div>
      <div className="small muted">Serves <Badge>{o.serves_goal}</Badge> {g?.text} · est. ${Number(o.est_cost_usd).toFixed(2)} · by <span className="mono">{o.proposed_by}</span> · {timeAgo(o.created_at)}</div>
      <ul className="small" style={{ margin: "4px 0", paddingLeft: 18 }}>{o.success_conditions.map((s: string) => <li key={s}>{s}</li>)}</ul>
      <div className="small faint">Why: {o.rationale} {o.evidence.length ? `· evidence: ${o.evidence.length}` : ""}</div>
      {o.status === "proposed" && <div className="row" style={{ marginTop: 6 }}>
        <input aria-label="note" placeholder="note" value={note} onChange={(e) => setNote(e.target.value)} style={{ width: 200 }} />
        <Button size="sm" variant="primary" onClick={async () => { try { const r = await post(`/api/objectives/${o.id}/adopt`, { execution_mode: "offline_fake", note }); reload(); openMission(r.mission_id); } catch (e) { setErr((e as Error).message); } }}>Adopt (offline)</Button>
        <Button size="sm" variant="ghost" onClick={async () => { try { await post(`/api/objectives/${o.id}/reject`, { note }); reload(); } catch (e) { setErr((e as Error).message); } }}>Decline</Button></div>}
      {o.mission_id && <Button size="sm" onClick={() => openMission(o.mission_id)}>Open mission</Button>}
      {err && <div className="small" style={{ color: "var(--bad)" }}>{err}</div>}
    </td></tr>
  );
}
