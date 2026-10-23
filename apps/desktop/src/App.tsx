import { useCallback, useEffect, useState } from "react";
import { bridge, NexusEvent, onEvent, ServiceState, useApi } from "./api";
import { Digest } from "./components/Autonomy";
import { UpdateBanner, UpdateCountdown } from "./components/Updates";
import { Button, SimBadge, Status } from "./components/ui";
import Chat from "./pages/Chat";
import MissionPage from "./pages/Mission";
import NewMission from "./pages/NewMission";
import Connections from "./pages/Connections";
import Objectives from "./pages/Objectives";
import Onboarding from "./pages/Onboarding";
import Settings from "./pages/Settings";
import Studio from "./pages/Studio";
import Tools from "./pages/Tools";

export type Route =
  | { view: "onboarding" }
  | { view: "new-mission" }
  | { view: "mission"; missionId: string; tab?: string }
  | { view: "studio" }
  | { view: "connections" }
  | { view: "objectives" }
  | { view: "tools" }
  | { view: "settings"; section?: string }
  | { view: "home" }
  | { view: "chat"; conversationId: string | null };

function ServiceBanner({ state, streamOk }: { state: ServiceState | null; streamOk: boolean }) {
  const [tail, setTail] = useState<string | null>(null);
  if (!state) return null;
  if (state.phase === "ready" && streamOk) return null;
  if (state.phase === "ready" && !streamOk) {
    return <div className="svc-banner warn banner" role="status">Event stream reconnecting — live updates paused; data shown may lag.</div>;
  }
  if (state.phase === "ready") return null;
  const bad = state.phase === "crashed" || state.phase === "failed";
  const detail = state.phase === "starting" ? state.detail : state.phase === "stopped" ? "service stopped"
    : `${state.detail}${state.phase === "failed" ? ` — ${state.hint}` : ""}`;
  return (
    <div className="banner">
      <div className={`svc-banner ${bad ? "bad" : "warn"}`} role="alert" data-testid="service-banner">
        <span>{state.phase === "starting" ? "Starting local service…" : bad ? "Local service unavailable" : "Service stopped"}</span>
        <span className="small" style={{ fontWeight: 400 }}>{detail}</span>
        <span className="grow" />
        {bad && <Button size="sm" onClick={async () => setTail(await bridge().logTail())}>Show log</Button>}
        {bad && <Button size="sm" onClick={() => bridge().revealLog()}>Open log folder</Button>}
        {state.phase !== "starting" && <Button size="sm" variant="primary" onClick={() => bridge().restartService()}>Restart service</Button>}
      </div>
      {tail !== null && <pre className="pre" style={{ margin: 0, borderRadius: 0, maxHeight: 240 }} aria-label="service log">{tail || "(log empty)"}</pre>}
    </div>
  );
}

export default function App() {
  const [svc, setSvc] = useState<ServiceState | null>(null);
  const [streamOk, setStreamOk] = useState(true);
  const [route, setRoute] = useState<Route>({ view: "chat", conversationId: null });
  const [advanced, setAdvanced] = useState<boolean>(() => { try { return localStorage.getItem("nexus.advanced") === "1"; } catch { return false; } });
  const [projectId, setProjectId] = useState<string | null>(() => {
    try { return localStorage.getItem("nexus.project"); } catch { return null; }
  });
  const ready = svc?.phase === "ready";

  useEffect(() => {
    let off1 = () => {}, off2 = () => {};
    try {
      void bridge().serviceState().then(setSvc);
      off1 = bridge().onServiceState(setSvc);
      off2 = bridge().onStream((s) => setStreamOk(s.connected));
    } catch (e) {
      setSvc({ phase: "failed", detail: (e as Error).message, logTail: "", hint: "Launch NEXUS from the desktop app." });
    }
    return () => { off1(); off2(); };
  }, []);

  const projects = useApi<any[]>(ready ? "/api/projects" : null, (e) => e.type === "project.created", [ready]);
  const missionMatch = useCallback((e: NexusEvent) => e.type.startsWith("mission.") || e.type === "task.accepted", []);
  const missions = useApi<any[]>(ready && projectId ? `/api/projects/${projectId}/missions` : null, missionMatch, [ready]);
  const convs = useApi<any[]>(ready ? "/api/chat/conversations" : null,
    (e) => e.type === "chat.conversation_created" || e.type === "chat.conversation_archived" || e.type === "chat.idle", [ready]);
  const autos = useApi<any>(ready ? "/api/autopilot" : null,
    (e) => e.type.startsWith("autopilot.") || e.type === "mission.status", [ready]);
  const attention = useApi<any[]>(ready ? "/api/attention" : null,
    (e) => e.type.startsWith("attention.") || e.type === "task.transition" || e.type.startsWith("model_call.finished"), [ready]);

  useEffect(() => {
    if (!projects.data) return;
    if (projects.data.length === 0) return;
    if (!projectId || !projects.data.some((p) => p.id === projectId)) selectProject(projects.data[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects.data]);

  useEffect(() => onEvent(() => {}), []);

  function selectProject(id: string) {
    setProjectId(id);
    try { localStorage.setItem("nexus.project", id); } catch { /* storage unavailable */ }
  }

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === ",") { e.preventDefault(); setRoute({ view: "settings" }); }
      if (e.ctrlKey && e.key.toLowerCase() === "n" && !e.shiftKey) { e.preventDefault(); setRoute({ view: "chat", conversationId: null }); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);

  const project = projects.data?.find((p) => p.id === projectId) ?? null;
  const attnCount = attention.data?.length ?? 0;

  return (
    <div className="shell">
      <div className="banner">
        <ServiceBanner state={svc} streamOk={streamOk} />
        <UpdateBanner />
      </div>
      <UpdateCountdown />
      <aside className="sidebar" aria-label="Navigation">
        <div className="brand"><span className="brand-mark" aria-hidden />NEXUS</div>
        <nav className="nav" aria-label="Main">
          <button className="nav-item new-chat" onClick={() => setRoute({ view: "chat", conversationId: null })}
                  aria-current={route.view === "chat" && !route.conversationId ? "page" : undefined}>＋ New chat <span className="count kbd">Ctrl N</span></button>
          <div className="nav-label">Chats</div>
          {convs.data?.length === 0 && <div className="small faint" style={{ padding: "4px 8px" }}>No chats yet</div>}
          {convs.data?.map((c) => (
            <button key={c.id} className="nav-item" title={c.title}
                    aria-current={route.view === "chat" && route.conversationId === c.id ? "page" : undefined}
                    onClick={() => setRoute({ view: "chat", conversationId: c.id })}>
              <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.title}</span>
              {c.busy && <span className="dot-busy" aria-label="working" />}
            </button>
          ))}
          {(autos.data?.standing?.length ?? 0) > 0 && <div className="nav-label">Autopilot</div>}
          {autos.data?.standing?.map((a: any) => (
            <button key={a.id} className="nav-item" title={`${a.schedule}${a.status_note ? ` · ${a.status_note}` : ""}`}
                    onClick={() => a.conversation_id && setRoute({ view: "chat", conversationId: a.conversation_id })}>
              <span className={`ap-dot ${a.active_run ? "run" : a.enabled ? "on" : ""}`} aria-hidden />
              <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.title}</span>
              <span className="xs faint">{a.enabled ? "" : "paused"}</span>
            </button>
          ))}
          <button className="nav-label nav-toggle" aria-expanded={advanced}
                  onClick={() => { const v = !advanced; setAdvanced(v); try { localStorage.setItem("nexus.advanced", v ? "1" : "0"); } catch { /* ignore */ } }}>
            {advanced ? "▾" : "▸"} Advanced</button>
          {advanced && <>
          <div style={{ padding: "0 12px 8px" }}>
          <label className="sr-only" htmlFor="project-select">Project</label>
          <select id="project-select" value={projectId ?? ""} onChange={(e) => selectProject(e.target.value)} disabled={!projects.data?.length}>
            {!projects.data?.length && <option value="">No projects</option>}
            {projects.data?.map((p) => <option key={p.id} value={p.id}>{p.is_demo ? "◆ " : ""}{p.name}</option>)}
          </select>
          {project?.is_demo ? <div style={{ marginTop: 6 }}><SimBadge /></div> : null}
        </div>

          <button className="nav-item" aria-current={route.view === "home" ? "page" : undefined}
                  onClick={() => setRoute({ view: "home" })} disabled={!project}>Overview</button>
          <button className="nav-item" aria-current={route.view === "new-mission" ? "page" : undefined}
                  onClick={() => setRoute({ view: "new-mission" })} disabled={!project}>＋ New mission <span className="count kbd">Ctrl N</span></button>
          <div className="nav-label">Missions</div>
          {missions.error && <div className="small" style={{ color: "var(--bad)", padding: "4px 8px" }}>{missions.error}</div>}
          {missions.data?.length === 0 && <div className="small faint" style={{ padding: "4px 8px" }}>No missions yet</div>}
          {missions.data?.map((m) => (
            <button key={m.id} className="nav-item" aria-current={route.view === "mission" && route.missionId === m.id ? "page" : undefined}
                    onClick={() => setRoute({ view: "mission", missionId: m.id })} title={m.title}>
              <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{m.title}</span>
              <Status value={m.status} />
            </button>
          ))}
          <div className="nav-label">Workspace</div>
          <button className="nav-item" aria-current={route.view === "objectives" ? "page" : undefined} onClick={() => setRoute({ view: "objectives" })} disabled={!project}>Goals & objectives</button>
          <button className="nav-item" aria-current={route.view === "connections" ? "page" : undefined} onClick={() => setRoute({ view: "connections" })} disabled={!project}>Connected services</button>
          <button className="nav-item" aria-current={route.view === "tools" ? "page" : undefined} onClick={() => setRoute({ view: "tools" })} disabled={!project}>Tool workshop</button>
          <button className="nav-item" aria-current={route.view === "studio" ? "page" : undefined} onClick={() => setRoute({ view: "studio" })} disabled={!project}>Agent studio</button>
          <button className="nav-item" aria-current={route.view === "settings" ? "page" : undefined} onClick={() => setRoute({ view: "settings" })}>
            Settings <span className="count kbd">Ctrl ,</span></button>
          <button className="nav-item" aria-current={route.view === "onboarding" ? "page" : undefined} onClick={() => setRoute({ view: "onboarding" })}>Setup</button>
          </>}
          {!advanced && <button className="nav-item" aria-current={route.view === "settings" ? "page" : undefined} onClick={() => setRoute({ view: "settings" })}>
            Settings <span className="count kbd">Ctrl ,</span></button>}
        </nav>
        <div className="sidebar-foot" aria-live="polite">
          <div className="row">
            <span>Service</span><span className="grow" />
            <Status value={svc?.phase === "ready" ? "ok" : svc?.phase ?? "starting"} label={svc?.phase ?? "…"} />
          </div>
          {attnCount > 0 && (
            <div className="row" style={{ marginTop: 6 }}>
              <span>Needs attention</span><span className="grow" /><span className="badge warn" data-testid="attention-count">{attnCount}</span>
            </div>
          )}
        </div>
      </aside>
      <main className="main" id="main">
        {!ready && svc && svc.phase !== "starting" ? (
          <div className="page"><div className="state error" role="alert"><h3>The local service is not running</h3>
            <div>Nothing shown here would be trustworthy without it. Use “Restart service” above, or open the log.</div></div></div>
        ) : !ready ? (
          <div className="page"><div className="state" role="status">Starting the local NEXUS service…</div></div>
        ) : route.view === "chat" ? (
          <Chat conversationId={route.conversationId} key={route.conversationId ?? "new"}
                onCreated={(id) => setRoute({ view: "chat", conversationId: id })}
                openMission={(id) => setRoute({ view: "mission", missionId: id })}
                openSettings={() => setRoute({ view: "settings" })} />
        ) : route.view === "onboarding" ? (
          <Onboarding onDone={(pid) => { if (pid) selectProject(pid); setRoute({ view: "new-mission" }); }} />
        ) : route.view === "new-mission" && project ? (
          <NewMission project={project} onCreated={(id) => setRoute({ view: "mission", missionId: id })} />
        ) : route.view === "mission" ? (
          <MissionPage missionId={route.missionId} key={route.missionId} />
        ) : route.view === "connections" && project ? (
          <Connections project={project} />
        ) : route.view === "objectives" && project ? (
          <Objectives project={project} openMission={(id) => setRoute({ view: "mission", missionId: id })} />
        ) : route.view === "tools" && project ? (
          <Tools project={project} />
        ) : route.view === "studio" && project ? (
          <Studio project={project} />
        ) : route.view === "settings" ? (
          <Settings project={project} />
        ) : (
          <Home project={project} missions={missions.data ?? []} attention={attention.data ?? []} go={setRoute} />
        )}
      </main>
    </div>
  );
}

function Home({ project, missions, attention, go }: { project: any; missions: any[]; attention: any[]; go: (r: Route) => void }) {
  if (!project) return <div className="page"><div className="state"><h3>No project selected</h3><Button onClick={() => go({ view: "onboarding" })}>Set up a workspace</Button></div></div>;
  return (
    <div className="page">
      <div className="page-head"><div className="titles"><h1>{project.name}</h1><div className="sub mono">{project.workspace_root}</div></div>
        <Button variant="primary" onClick={() => go({ view: "new-mission" })}>New mission</Button></div>
      <Digest projectId={project.id} onOpenMission={(id) => go({ view: "mission", missionId: id })} />
      <div style={{ height: 12 }} />
      <div className="grid halves">
        <section className="card"><div className="card-head"><h3>Missions</h3></div>
          {missions.length === 0 ? <div className="state"><h3>No missions</h3>Create one to give your agents work.</div> : (
            <table className="data"><thead><tr><th>Mission</th><th>Status</th><th>Tasks</th></tr></thead><tbody>
              {missions.map((m) => <tr key={m.id} className="clickable" onClick={() => go({ view: "mission", missionId: m.id })}>
                <td>{m.title}{m.execution_mode === "offline_fake" && <> <SimBadge /></>}</td><td><Status value={m.status} /></td>
                <td>{m.accepted_count}/{m.task_count} accepted</td></tr>)}
            </tbody></table>)}
        </section>
        <section className="card"><div className="card-head"><h3>Needs your attention</h3></div>
          {attention.length === 0 ? <div className="state">Nothing needs you right now.</div> : (
            <table className="data"><tbody>{attention.map((a, i) => (
              <tr key={i} className="clickable" onClick={() => a.kind === "objective_proposed" ? go({ view: "objectives" }) : a.kind === "tool_approval" ? go({ view: "tools" })
                : a.kind.startsWith("action_") ? go({ view: "connections" }) : a.mission_id && go({ view: "mission", missionId: a.mission_id })}>
                <td><Status value={a.kind.includes("failed") ? "failed" : "blocked"} label={a.kind.replace(/_/g, " ")} /></td>
                <td>{a.title}<div className="small muted">{a.detail}</div></td></tr>))}</tbody></table>)}
        </section>
      </div>
    </div>
  );
}
