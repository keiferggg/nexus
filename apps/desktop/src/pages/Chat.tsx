// Chat: the default way to use NEXUS. Type anything; NEXUS answers or turns it into a mission and reports back here.
import { useEffect, useMemo, useRef, useState } from "react";
import { fmtUsd, NexusEvent, onEvent, post, put, useApi } from "../api";
import { PriceFix } from "../components/PriceFix";
import { DefaultModelPicker, ProvidersPanel } from "../components/Providers";
import { Badge, Button, Doc, Status } from "../components/ui";

const EXAMPLES = [
  "Research the 5 most credible AI agent frameworks released this year and recommend one for a small team, with sources",
  "Add scheduled missions to NEXUS (missions that re-run every N hours) in C:\\Users\\Keifers\\nexus-dev, with tests",
  "Find 10 recent papers on long-horizon agent planning, summarize each in 3 lines, and rank them by practical value",
  "Compare the real cost per 1M tokens of the top 6 LLM APIs today and post a summary table to my webhook",
];

export default function Chat({ conversationId, onCreated, openMission, openSettings }: {
  conversationId: string | null; onCreated: (id: string) => void; openMission: (id: string) => void; openSettings: () => void;
}) {
  const q = useApi<any>(conversationId ? `/api/chat/conversations/${conversationId}` : null,
    (e) => (e.type.startsWith("chat.") && e.payload?.conversation_id === conversationId), [conversationId]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const busy = sending || !!q.data?.busy;
  const messages: any[] = q.data?.messages ?? [];
  const lastOwner = [...messages].reverse().find((m) => m.role === "owner")?.content ?? "";

  useEffect(() => { endRef.current?.scrollIntoView({ block: "end" }); }, [messages.length, busy]);

  async function send(t = text) {
    const body = t.trim();
    if (!body || busy) return;
    setErr(null); setSending(true);
    try {
      if (!conversationId) {
        const c = await post<any>("/api/chat/conversations", { text: body });
        setText("");
        onCreated(c.id);
      } else {
        await post(`/api/chat/conversations/${conversationId}/messages`, { text: body });
        setText("");
        q.reload();
      }
    } catch (e) { setErr((e as Error).message); } finally { setSending(false); }
  }

  return (
    <div className="chat" data-testid="chat">
      <div className="chat-scroll">
        {!conversationId || (messages.length === 0 && !busy) ? (
          <div className="chat-empty">
            <h1>What should NEXUS do?</h1>
            <div className="muted">Ask anything. Real work becomes a mission: agents plan it, use the web and your tools, check each other, and report back here.</div>
            <div className="chat-examples">{EXAMPLES.map((x) => <button key={x} className="chat-example" onClick={() => setText(x)}>{x}</button>)}</div>
          </div>
        ) : (
          <div className="chat-thread">
            {messages.map((m) => <Message key={m.id} m={m} openMission={openMission} openSettings={openSettings}
              retry={() => send(lastOwner)} />)}
            {busy && <div className="cmsg nexus"><div className="bubble thinking" data-testid="thinking"><span /><span /><span /></div></div>}
            <div ref={endRef} />
          </div>
        )}
      </div>
      <div className="composer">
        {err && <div className="callout bad small" role="alert">{err}</div>}
        <div className="composer-box">
          <textarea aria-label="Message NEXUS" placeholder="Message NEXUS…" value={text} rows={Math.min(8, Math.max(1, text.split("\n").length))}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); } }} />
          <Button variant="primary" onClick={() => send()} disabled={busy || !text.trim()} aria-label="Send">{busy ? "…" : "Send"}</Button>
        </div>
        <div className="xs faint">Enter to send · Shift+Enter for a new line · Agents always work within your spending caps</div>
      </div>
    </div>
  );
}

function Message({ m, openMission, openSettings, retry }: { m: any; openMission: (id: string) => void; openSettings: () => void; retry: () => void }) {
  if (m.role === "owner") return <div className="cmsg owner"><div className="bubble">{m.content}</div></div>;
  const body = (() => {
    switch (m.kind) {
      case "mission": return <MissionCard m={m} openMission={openMission} />;
      case "progress": return <div className="bubble progress"><Doc text={m.content} /></div>;
      case "attention": return <AttentionCard m={m} openMission={openMission} />;
      case "result": return <ResultCard m={m} openMission={openMission} />;
      case "setup": return <SetupCard m={m} retry={retry} openSettings={openSettings} />;
      case "autopilot": return <AutopilotCard m={m} />;
      case "error": return <div className="bubble error" role="alert">{m.content}</div>;
      default: return <div className="bubble"><Doc text={m.content} /></div>;
    }
  })();
  return <div className={`cmsg nexus kind-${m.kind}`} data-kind={m.kind}>{body}</div>;
}

const PURPOSE: Record<string, string> = { plan: "Planning", execute: "Working", review: "Reviewing", integrate: "Assembling the result",
  guard: "Safety check", consult: "Consulting a colleague", resolve_conflict: "Resolving a conflict" };

function activityLine(e: NexusEvent): string | null {
  const p = e.payload || {};
  if (e.type === "model_call.started") return `${PURPOSE[String(p.purpose).split(":")[0]] ?? "Thinking"}…`;
  if (e.type === "tool.succeeded" || e.type === "tool.started") {
    const a = p.args || {};
    const host = (u: string) => { try { return new URL(u).hostname; } catch { return u; } };
    switch (p.tool) {
      case "web_search": return `Searching the web${a.query ? `: “${a.query}”` : ""}`;
      case "fetch_url": return `Reading ${host(a.url ?? "")}`;
      case "read_source": case "read_file": return `Reading ${a.path ?? "a file"}`;
      case "repo_write": return `Editing ${a.path ?? "code"}`;
      case "run_tests": return "Running tests in the sandbox";
      default: return `Using ${String(p.tool).replace(/^svc__|^tool__/, "").replace(/__/g, " ")}`;
    }
  }
  if (e.type === "action.requested") return `Requesting ${p.action ?? "an action"}`;
  if (e.type === "review.recorded") return `Checking: ${p.criterion} ${String(p.verdict).toUpperCase()}`;
  return null;
}

function MissionCard({ m, openMission }: { m: any; openMission: (id: string) => void }) {
  const mid = m.mission_id as string;
  const q = useApi<any>(`/api/missions/${mid}`, (e) => e.mission_id === mid &&
    (e.type.startsWith("mission.") || e.type.startsWith("task.") || e.type === "model_call.finished"));
  const [line, setLine] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => onEvent((e) => { if (e.mission_id === mid) { const l = activityLine(e); if (l) setLine(l); } }), [mid]);
  const v = q.data;
  const tasks: any[] = v?.tasks ?? [];
  const done = tasks.filter((t) => t.status === "accepted").length;
  const status = v?.mission?.status ?? v?.status;
  const spent = v?.budget?.actual_usd;
  const d = m.data || {};
  const live = status === "running";
  return (
    <div className="mission-card" data-testid="mission-card">
      <div className="row"><strong className="grow">{m.content}</strong><Status value={status} /></div>
      <div className="small muted">{d.objective}</div>
      {d.success_conditions?.length ? <ul className="small sc">{d.success_conditions.map((s: string, i: number) => <li key={i}>{s}</li>)}</ul> : null}
      {tasks.length > 0 && <div className="progress-bar" aria-label="progress"><div style={{ width: `${(100 * done) / tasks.length}%` }} /></div>}
      <div className="row small wrap">
        {tasks.length > 0 && <span>{done}/{tasks.length} steps</span>}
        <span className="faint">spent {fmtUsd(spent ?? 0, 3)} of {fmtUsd(d.cap_usd, 2)} cap</span>
        {live && line && <span className="live-line" data-testid="live-line">● {line}</span>}
      </div>
      {status === "draft" && (
        <div className="row">
          <span className="small muted grow">{d.why_not_started}</span>
          <Button size="sm" variant="primary" onClick={async () => { try { await post(`/api/chat/missions/${mid}/start`); q.reload(); } catch (e) { setErr((e as Error).message); } }}>Start (est. {fmtUsd(d.estimate_usd, 2)})</Button>
        </div>
      )}
      {err && <div className="callout bad small">{err}</div>}
      <div className="row"><Button size="sm" variant="ghost" onClick={() => openMission(mid)}>Open details</Button>
        {live && <Button size="sm" variant="ghost" onClick={() => post(`/api/missions/${mid}/pause`).then(q.reload)}>Pause</Button>}
        {status === "paused" && <Button size="sm" variant="ghost" onClick={() => post(`/api/missions/${mid}/start`).then(q.reload)}>Resume</Button>}</div>
    </div>
  );
}

function AttentionCard({ m, openMission }: { m: any; openMission: (id: string) => void }) {
  const a = m.data || {};
  const live = useApi<any[]>("/api/attention", (e) => e.type.startsWith("attention.") || e.type.startsWith("action.") || e.type === "task.transition");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const open = useMemo(() => (live.data ?? []).some((x) => x.kind === a.kind && (x.action_request_id ?? x.submission_id ?? x.task_id) ===
    (a.action_request_id ?? a.submission_id ?? a.task_id)), [live.data, a]);
  const act = async (fn: () => Promise<any>) => { setErr(null); try { await fn(); live.reload(); } catch (e) { setErr((e as Error).message); } };
  return (
    <div className={`attention-card ${open ? "open" : "done"}`} data-testid="attention-card">
      <div className="row"><strong className="grow">{m.content}</strong>{open ? <Badge tone="warn">needs you</Badge> : <Badge tone="ok">handled</Badge>}</div>
      {a.detail && <div className="small muted">{a.detail}</div>}
      {a.kind === "action_approval" && a.args && <pre className="pre small" style={{ maxHeight: 120 }}>{JSON.stringify(a.args, null, 1)}</pre>}
      {open && (
        <div className="row wrap">
          {a.kind === "action_approval" && <>
            <Button size="sm" variant="primary" onClick={() => act(() => post(`/api/actions/${a.action_request_id}/approve`, { note }))}>Approve & run</Button>
            <Button size="sm" variant="danger" onClick={() => act(() => post(`/api/actions/${a.action_request_id}/deny`, { note }))}>Deny</Button></>}
          {a.submission_id && (a.kind === "owner_approval" || a.kind === "task_blocked") &&
            <Button size="sm" variant="primary" onClick={() => act(() => post(`/api/submissions/${a.submission_id}/owner-review`, { criterion: a.criterion, verdict: "pass", note: note || "approved in chat" }))}>Approve it myself</Button>}
          {(a.kind === "task_blocked" || a.kind === "task_failed") && a.task_id &&
            <Button size="sm" onClick={() => act(() => post(`/api/tasks/${a.task_id}/retry`))}>Retry step</Button>}
          <input aria-label="note" placeholder="note (optional)" value={note} onChange={(e) => setNote(e.target.value)} style={{ width: 200 }} />
          {m.mission_id && <Button size="sm" variant="ghost" onClick={() => openMission(m.mission_id)}>Details</Button>}
        </div>
      )}
      {err && <div className="callout bad small">{err}</div>}
    </div>
  );
}

function ResultCard({ m, openMission }: { m: any; openMission: (id: string) => void }) {
  const d = m.data || {};
  const [full, setFull] = useState(m.content.length < 4000);
  const done = (d.actions ?? []).filter((a: any) => a.status === "succeeded").length;
  return (
    <div className="result-card" data-testid="result-card">
      <div className="row small"><Badge tone="ok">done</Badge><span className="faint">{d.steps} steps · {fmtUsd(d.spent_usd, 3)}{done ? ` · ${done} action(s) taken` : ""}</span>
        <span className="grow" />{m.mission_id && <Button size="sm" variant="ghost" onClick={() => openMission(m.mission_id)}>Evidence & details</Button>}</div>
      <div className={full ? "" : "clamped"}><Doc text={m.content} /></div>
      {!full && <Button size="sm" variant="ghost" onClick={() => setFull(true)}>Show all</Button>}
      <div className="row"><Button size="sm" variant="ghost" onClick={() => navigator.clipboard?.writeText(m.content)}>Copy</Button></div>
    </div>
  );
}

function SetupCard({ m, retry, openSettings }: { m: any; retry: () => void; openSettings: () => void }) {
  const d = m.data || {};
  const status = useApi<any>("/api/chat/status", (e) => e.type === "settings.changed" || e.type.startsWith("provider.") || e.type.startsWith("pricing."));
  const resolved = status.data && !status.data.setup;
  const need = status.data?.setup?.need ?? d.need;
  return (
    <div className="setup-card" data-testid="setup-card">
      <div className="row"><strong className="grow">{resolved ? "All set" : "One-time setup"}</strong>{resolved && <Badge tone="ok">ready</Badge>}</div>
      {!resolved && <div className="small">{status.data?.setup?.detail ?? m.content}</div>}
      {!resolved && need === "model" && <div className="col"><ProvidersPanel /><DefaultModelPicker /></div>}
      {!resolved && need === "price" && status.data?.setup?.provider_id &&
        <PriceFix providerId={status.data.setup.provider_id} model={status.data.setup.model} onSaved={status.reload} />}
      {!resolved && need === "data_ack" && (
        <div className="col small">
          <div>Never sent: API keys, files outside a project folder, other projects' private memory.</div>
          <div><Button variant="primary" onClick={async () => { await put("/api/settings/chat.data_ack", { value: true }); status.reload(); }}>I understand — continue</Button></div>
        </div>)}
      {!resolved && need === "readiness" && <div><Button size="sm" onClick={openSettings}>Open Settings → Providers</Button></div>}
      {resolved && <div><Button variant="primary" size="sm" onClick={retry}>Send my message again</Button></div>}
    </div>
  );
}

export function AutopilotControls({ a, reload }: { a: any; reload: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  const op = async (o: string) => { setErr(null); try { await post(`/api/autopilot/${a.id}/${o}`); reload(); } catch (e) { setErr((e as Error).message); } };
  return (
    <div className="row wrap">
      {a.enabled ? <Button size="sm" onClick={() => op("pause")}>Pause</Button> : <Button size="sm" variant="primary" onClick={() => op("resume")}>Enable</Button>}
      <Button size="sm" onClick={() => op("run_now")} disabled={!!a.active_run}>Run now</Button>
      <Button size="sm" variant="ghost" onClick={() => { if (confirm(`Delete autopilot “${a.title}”? Past runs are kept.`)) void op("delete"); }}>Delete</Button>
      {err && <span className="small" style={{ color: "var(--bad)" }}>{err}</span>}
    </div>
  );
}

function AutopilotCard({ m }: { m: any }) {
  const d = m.data || {};
  const q = useApi<any>("/api/autopilot", (e) => e.type.startsWith("autopilot.") || e.type.startsWith("mission."));
  const a = (q.data?.standing ?? []).find((x: any) => x.id === d.standing_id);
  return (
    <div className="autopilot-card" data-testid="autopilot-card">
      <div className="row"><strong className="grow">⟳ {m.content}</strong>
        {a ? (a.enabled ? <Badge tone="ok">on</Badge> : <Badge tone="warn">paused</Badge>) : q.data ? <Badge>deleted</Badge> : null}</div>
      <div className="small">{d.schedule} · up to {fmtUsd(d.run_cap_usd, 2)} per run, {fmtUsd(d.daily_cap_usd, 2)} per day</div>
      <div className="small muted">{d.objective}</div>
      {a && <div className="small faint">{a.active_run ? "running now" : a.next_run_at ? `next: ${new Date(a.next_run_at).toLocaleString()}` : ""}
        {a.status_note ? ` · ${a.status_note}` : ""} · spent today {fmtUsd(a.spent_today_usd, 3)}</div>}
      {!a?.enabled && d.why_disabled && a && <div className="small" style={{ color: "var(--warn)" }}>{d.why_disabled}</div>}
      {a && <AutopilotControls a={a} reload={q.reload} />}
    </div>
  );
}
