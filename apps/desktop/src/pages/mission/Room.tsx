import { useCallback, useMemo, useState } from "react";
import { NexusEvent, post, timeAgo, useApi } from "../../api";
import { Badge, Button, Field, Load, Status } from "../../components/ui";

const TYPES = ["question", "proposal", "evidence", "objection", "task_offer", "task_claim", "handoff", "review_request", "decision", "blocker", "status"];

export default function Room({ m }: { m: any }) {
  const mid = m.mission.id;
  const match = useCallback((e: NexusEvent) => e.mission_id === mid && (e.type.startsWith("message.") || e.type.startsWith("objection")), [mid]);
  const msgs = useApi<any[]>(`/api/missions/${mid}/messages`, match);
  const [thread, setThread] = useState<string>(`mission:${mid}`);
  const tasks = m.tasks as any[];
  const agentNames = useMemo(() => Object.fromEntries(tasks.filter((t) => t.agent).map((t) => [`agent:${t.agent.id}`, `${t.agent.name} (${t.agent.role})`])), [tasks]);
  const name = (s: string) => agentNames[s] ?? s.replace(/^nexus:/, "NEXUS ");
  const threads = [{ id: `mission:${mid}`, label: "Mission room" }, ...tasks.map((t) => ({ id: `task:${t.id}`, label: t.title, task: t }))];

  return (
    <div className="grid" style={{ gridTemplateColumns: "260px minmax(0,1fr) 340px" }}>
      <nav className="card" aria-label="Threads"><div className="card-head"><h3>Threads</h3></div><div className="card-body thread-list">
        {threads.map((t) => {
          const n = msgs.data?.filter((x) => x.thread_id === t.id).length ?? 0;
          return <button key={t.id} className="nav-item" aria-current={thread === t.id ? "page" : undefined} onClick={() => setThread(t.id)}>
            <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.label}</span><span className="count">{n}</span></button>;
        })}
      </div></nav>
      <section className="card" aria-label="Messages"><div className="card-head"><h3>{threads.find((t) => t.id === thread)?.label}</h3></div>
        <div className="card-body">
          <Load q={msgs}>{(all) => {
            const list = all.filter((x) => x.thread_id === thread);
            if (!list.length) return <div className="state">No messages in this thread yet.</div>;
            return <div>{list.map((x) => (
              <article key={x.id} className={`msg ${x.type}${x.sender === "owner" ? " owner" : ""}`} data-testid="room-message">
                <div className="msg-head"><Badge tone={x.type === "objection" || x.type === "blocker" ? "bad" : x.type === "decision" ? "ok" : "neutral"}>{x.type.replace("_", " ")}</Badge>
                  <strong>{name(x.sender)}</strong><span className="faint">→ {x.recipients.map(name).join(", ")}</span><span className="grow" />
                  <span className="faint small">{timeAgo(x.created_at)}</span></div>
                <div style={{ whiteSpace: "pre-wrap" }}>{x.body}</div>
                <div className="row wrap small faint" style={{ marginTop: 4 }}>
                  {Object.entries(x.refs ?? {}).map(([k, v]) => <span key={k} className="mono">{k}: {String(v).slice(0, 18)}</span>)}
                  {x.deliveries.map((d: any) => <span key={d.recipient}><Status value={d.status} label={`${name(d.recipient)}: ${d.status}`} /></span>)}
                </div>
              </article>))}</div>;
          }}</Load>
        </div></section>
      <Intervene m={m} thread={thread} onPosted={msgs.reload} />
    </div>
  );
}

function Intervene({ m, thread, onPosted }: { m: any; thread: string; onPosted: () => void }) {
  const taskId = thread.startsWith("task:") ? thread.slice(5) : null;
  const [type, setType] = useState("question");
  const [body, setBody] = useState("");
  const [mech, setMech] = useState("");
  const [test, setTest] = useState("");
  const [cond, setCond] = useState("");
  const [blocking, setBlocking] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  async function send() {
    setErr(null); setOk(null);
    try {
      if (type === "objection") {
        if (!taskId) throw new Error("open a task thread to object to a specific task");
        await post(`/api/missions/${m.mission.id}/objections`, { target_ref: `task:${taskId}`, summary: body, mechanism: mech,
          evidence_or_test: test, resolution_condition: cond, blocking });
        await post(`/api/missions/${m.mission.id}/messages`, { type: "objection", body: `${body}\nMechanism: ${mech}\nTest: ${test}\nResolved when: ${cond}`, task_id: taskId });
        setOk(blocking ? "Blocking objection recorded: the task cannot be accepted until you resolve it." : "Objection recorded.");
      } else {
        await post(`/api/missions/${m.mission.id}/messages`, { type, body, task_id: taskId });
        setOk("Delivered to the agent's next context packet.");
      }
      setBody(""); setMech(""); setTest(""); setCond("");
      onPosted();
    } catch (e) { setErr((e as Error).message); }
  }
  return (
    <section className="card" aria-label="Intervene"><div className="card-head"><h3>Intervene</h3></div><div className="card-body col">
      <div className="small muted">{taskId ? "Messages go to the agent assigned to this task and appear in its next context packet." : "Mission-room messages go to the planner."}</div>
      <Field label="Type" htmlFor="it"><select id="it" value={type} onChange={(e) => setType(e.target.value)}>{TYPES.filter((t) => ["question", "proposal", "objection", "decision", "status"].includes(t)).map((t) => <option key={t}>{t}</option>)}</select></Field>
      <Field label={type === "objection" ? "Objection (what is wrong)" : "Message"} htmlFor="ib"><textarea id="ib" value={body} onChange={(e) => setBody(e.target.value)} /></Field>
      {type === "objection" && (
        <>
          <Field label="Mechanism (why it will fail)" htmlFor="im"><input id="im" value={mech} onChange={(e) => setMech(e.target.value)} /></Field>
          <Field label="Evidence or proposed test" htmlFor="ie"><input id="ie" value={test} onChange={(e) => setTest(e.target.value)} /></Field>
          <Field label="Resolved when…" htmlFor="ic"><input id="ic" value={cond} onChange={(e) => setCond(e.target.value)} /></Field>
          <label className="row small"><input type="checkbox" checked={blocking} onChange={(e) => setBlocking(e.target.checked)} /> Blocking (prevents acceptance until resolved)</label>
        </>
      )}
      {err && <div className="callout bad" role="alert">{err}</div>}
      {ok && <div className="callout ok" role="status">{ok}</div>}
      <Button variant="primary" disabled={!body.trim() || (type === "objection" && (!mech || !test || !cond))} onClick={send}>Send</Button>
      <OpenObjections m={m} />
    </div></section>
  );
}

function OpenObjections({ m }: { m: any }) {
  const q = useApi<any>(`/api/missions/${m.mission.id}/understanding`, (e) => e.mission_id === m.mission.id && e.type.startsWith("objection"));
  const [res, setRes] = useState<Record<string, string>>({});
  return (
    <Load q={q}>{(u) => {
      const open = u.proposals.filter((p: any) => p.kind === "objection" && p.status === "open");
      if (!open.length) return null;
      return (
        <div className="col"><h4>Open objections</h4>{open.map((o: any) => (
          <div key={o.id} className="callout bad small"><strong>{o.summary}</strong><div>Resolved when: {o.resolution_condition}</div>
            <div className="row" style={{ marginTop: 4 }}>
              <input aria-label="resolution" placeholder="resolution" value={res[o.id] ?? ""} onChange={(e) => setRes({ ...res, [o.id]: e.target.value })} />
              <Button size="sm" disabled={!res[o.id]} onClick={async () => { await post(`/api/proposals/${o.id}/resolve`, { resolution: res[o.id] }); q.reload(); }}>Resolve</Button>
            </div></div>))}</div>
      );
    }}</Load>
  );
}
