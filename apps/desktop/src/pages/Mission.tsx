import { useCallback, useEffect, useState } from "react";
import { fmtUsd, NexusEvent, post, put, useApi } from "../api";
import { Button, Dialog, Field, Load, SimBadge, Status, Tabs } from "../components/ui";
import Artifacts from "./mission/Artifacts";
import Control from "./mission/Control";
import Room from "./mission/Room";
import Timeline from "./mission/Timeline";
import Understanding from "./mission/Understanding";

type Tab = "control" | "room" | "understanding" | "artifacts" | "timeline";
const TABS: { id: Tab; label: string; kbd: string }[] = [
  { id: "control", label: "Mission control", kbd: "Ctrl 1" },
  { id: "room", label: "Agent room", kbd: "Ctrl 2" },
  { id: "understanding", label: "Shared understanding", kbd: "Ctrl 3" },
  { id: "artifacts", label: "Artifacts", kbd: "Ctrl 4" },
  { id: "timeline", label: "Timeline", kbd: "Ctrl 5" },
];

export function Budget({ b, missionId, editable }: { b: any; missionId: string; editable: boolean }) {
  const [edit, setEdit] = useState(false);
  const [cap, setCap] = useState(String(b.cap_usd ?? ""));
  const [err, setErr] = useState<string | null>(null);
  const capV = b.cap_usd ?? 0;
  const pct = (v: number) => (capV > 0 ? Math.min(100, (v / capV) * 100) : 0);
  return (
    <div className="col" style={{ minWidth: 300 }} data-testid="budget">
      <div className="row between small">
        <span><strong>{fmtUsd(b.committed_usd)}</strong> committed{b.cap_usd !== null ? <> of {fmtUsd(b.cap_usd, 2)} cap</> : " (no cap: offline)"}</span>
        {editable && <Button size="sm" variant="ghost" onClick={() => setEdit(true)}>Change cap</Button>}
      </div>
      {b.cap_usd !== null && (
        <div className="meter" role="img" aria-label={`actual ${fmtUsd(b.actual_usd)}, reserved ${fmtUsd(b.reserved_usd)}, unresolved ${fmtUsd(b.unresolved_usd)}`}>
          <span className="m-actual" style={{ width: `${pct(b.actual_usd)}%` }} />
          <span className="m-reserved" style={{ width: `${pct(b.reserved_usd)}%` }} />
          <span className="m-unresolved" style={{ width: `${pct(b.unresolved_usd)}%` }} />
        </div>
      )}
      <div className="legend"><span><i style={{ background: "var(--accent)" }} />actual {fmtUsd(b.actual_usd)}</span>
        <span><i style={{ background: "var(--warn)" }} />reserved {fmtUsd(b.reserved_usd)}</span>
        <span><i style={{ background: "var(--bad)" }} />unresolved {fmtUsd(b.unresolved_usd)}</span></div>
      {edit && (
        <Dialog title="Change spend cap" onClose={() => setEdit(false)} footer={<>
          <Button onClick={() => setEdit(false)}>Cancel</Button>
          <Button variant="primary" onClick={async () => { try { await put(`/api/missions/${missionId}/budget`, { value: Number(cap) }); setEdit(false); } catch (e) { setErr((e as Error).message); } }}>Save</Button></>}>
          <Field label="Spend cap (USD)" htmlFor="ncap"><input id="ncap" type="number" min="0.01" step="0.01" value={cap} onChange={(e) => setCap(e.target.value)} /></Field>
          <div className="small muted">Only you can change the cap; agents cannot. Already committed spend ({fmtUsd(b.committed_usd)}) is not refunded by lowering it.</div>
          {err && <div className="callout bad">{err}</div>}
        </Dialog>
      )}
    </div>
  );
}

function ReviseDialog({ m, onClose }: { m: any; onClose: () => void }) {
  const [objective, setObjective] = useState(m.revision.objective);
  const [sc, setSc] = useState(m.revision.success_conditions.map((c: any) => c.text).join("\n"));
  const [cons, setCons] = useState(m.revision.constraints.map((c: any) => c.text).join("\n"));
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<any>(null);
  return (
    <Dialog title={`Change requirements (creates revision ${m.revision.revision + 1})`} onClose={onClose} footer={res ? <Button onClick={onClose}>Close</Button> : <>
      <Button onClick={onClose}>Cancel</Button>
      <Button variant="primary" disabled={!reason.trim()} onClick={async () => {
        try {
          const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);
          setRes(await post(`/api/missions/${m.mission.id}/revise`, {
            objective: objective !== m.revision.objective ? objective : null,
            success_conditions: sc !== m.revision.success_conditions.map((c: any) => c.text).join("\n") ? lines(sc) : null,
            constraints: cons !== m.revision.constraints.map((c: any) => c.text).join("\n") ? lines(cons) : null, reason }));
        } catch (e) { setErr((e as Error).message); }
      }}>Create revision</Button></>}>
      {res ? (
        <div className="col small">
          <div className="callout info">Revision {res.revision} recorded.</div>
          <div>Changed constraints: {res.changed_constraints.join(", ") || "none"}</div>
          <div>Invalidated tasks: {res.affected.length} · Rebased (unaffected) tasks: {res.rebased.length}</div>
          <div className="muted">Affected accepted results stay in history but are no longer current; in-flight work on them was stopped.</div>
        </div>
      ) : (
        <>
          <Field label="Objective" htmlFor="ro"><textarea id="ro" value={objective} onChange={(e) => setObjective(e.target.value)} /></Field>
          <Field label="Success conditions (one per line)" htmlFor="rsc"><textarea id="rsc" value={sc} onChange={(e) => setSc(e.target.value)} /></Field>
          <Field label="Constraints (one per line)" htmlFor="rc"><textarea id="rc" value={cons} onChange={(e) => setCons(e.target.value)} /></Field>
          <Field label="Reason (recorded)" htmlFor="rr"><input id="rr" value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
          {err && <div className="callout bad" role="alert">{err}</div>}
        </>
      )}
    </Dialog>
  );
}

export default function MissionPage({ missionId }: { missionId: string }) {
  const [tab, setTab] = useState<Tab>("control");
  const [revise, setRevise] = useState(false);
  const [actErr, setActErr] = useState<string | null>(null);
  const match = useCallback((e: NexusEvent) => e.mission_id === missionId, [missionId]);
  const q = useApi<any>(`/api/missions/${missionId}`, match);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.ctrlKey && /^[1-5]$/.test(e.key)) { e.preventDefault(); setTab(TABS[Number(e.key) - 1].id); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);

  async function act(path: string, confirmText?: string) {
    if (confirmText && !confirm(confirmText)) return;
    setActErr(null);
    try { await post(path); q.reload(); } catch (e) { setActErr((e as Error).message); }
  }

  return (
    <div className="page">
      <Load q={q}>{(m) => {
        const st = m.mission.status;
        return (
          <>
            <div className="page-head">
              <div className="titles">
                <div className="row wrap"><h1>{m.mission.title}</h1><Status value={st} />{m.is_simulated && <SimBadge />}
                  <span className="badge neutral">revision {m.revision.revision}</span></div>
                <div className="sub">{m.revision.objective}</div>
                {m.mission.status_reason && <div className="small muted" style={{ marginTop: 4 }}>{m.mission.status_reason}</div>}
              </div>
              <Budget b={m.budget} missionId={missionId} editable={m.mission.execution_mode === "live" && !["canceled", "completed"].includes(st)} />
              <div className="btn-group">
                {["draft", "paused", "blocked"].includes(st) && <Button variant="primary" onClick={() => act(`/api/missions/${missionId}/start`)}>{st === "draft" ? "Start" : "Resume"}</Button>}
                {["running", "blocked"].includes(st) && <Button onClick={() => act(`/api/missions/${missionId}/pause`)}>Pause</Button>}
                {!["canceled", "canceling", "completed"].includes(st) && (
                  <Button variant="danger" onClick={() => act(`/api/missions/${missionId}/cancel`, "Cancel this mission? New work stops immediately and running agents are signalled to stop. Charges already in flight may still be billed by the provider.")}>Cancel</Button>)}
                {!["canceled", "canceling"].includes(st) && <Button onClick={() => setRevise(true)}>Change requirements</Button>}
              </div>
            </div>
            {m.is_simulated && <div className="callout sim small" style={{ marginBottom: 12 }}>This mission runs the deterministic fake provider. Every agent output is scripted fixture behavior — useful for exercising NEXUS, not evidence of model capability.</div>}
            {actErr && <div className="callout bad" role="alert" style={{ marginBottom: 12 }}>{actErr}</div>}
            <Tabs tabs={TABS} value={tab} onChange={setTab} />
            <div role="tabpanel" aria-labelledby={`tab-${tab}`}>
              {tab === "control" && <Control m={m} reload={q.reload} />}
              {tab === "room" && <Room m={m} />}
              {tab === "understanding" && <Understanding missionId={missionId} />}
              {tab === "artifacts" && <Artifacts m={m} />}
              {tab === "timeline" && <Timeline m={m} />}
            </div>
            {revise && <ReviseDialog m={m} onClose={() => { setRevise(false); q.reload(); }} />}
          </>
        );
      }}</Load>
    </div>
  );
}
