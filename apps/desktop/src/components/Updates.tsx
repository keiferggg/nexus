import { useEffect, useState } from "react";
import { bridge, timeAgo } from "../api";
import { Button, Card, KV, Mono, Status } from "./ui";

function useUpdate() {
  const [s, setS] = useState<any>(null);
  useEffect(() => {
    let off = () => {};
    try {
      void bridge().updateState().then(setS);
      off = bridge().onUpdate(setS);
    } catch { /* no bridge */ }
    return () => off();
  }, []);
  return [s, setS] as const;
}

export function UpdateBanner() {
  const [s] = useUpdate();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!s || s.phase !== "available") return null;
  return (
    <div className="banner">
      <div className="svc-banner info" role="status" data-testid="update-banner">
        <span>NEXUS {s.version} is ready</span>
        <span className="small" style={{ fontWeight: 400 }}>
          You have {s.current}. {s.autoInstallOnQuit ? "It installs automatically when you close NEXUS, or" : "Install it"} now — your data and keys are kept and running missions resume.
        </span>
        <span className="grow" />
        {err && <span className="small" style={{ color: "var(--bad)" }}>{err}</span>}
        <Button size="sm" variant="primary" disabled={busy} onClick={async () => {
          setBusy(true); setErr(null);
          const r = await bridge().applyUpdate();
          if (!r.ok) { setErr(r.error ?? "update failed"); setBusy(false); }
        }}>{busy ? "Updating…" : "Restart to update"}</Button>
      </div>
    </div>
  );
}

export function UpdatesPanel() {
  const [s, setS] = useUpdate();
  const [checking, setChecking] = useState(false);
  if (!s) return null;
  return (
    <Card title="Updates" id="updates" actions={<Status value={s.phase === "available" ? "running" : s.phase === "disabled" ? "canceled" : "ok"}
      label={s.phase === "available" ? `update ${s.version} ready` : s.phase === "disabled" ? "not available" : "up to date"} />}>
      <div className="col">
        <KV items={[["Installed version", <Mono>{s.current}</Mono>], ["Update source", s.feed ? <Mono>{s.feed}</Mono> : (s.reason ?? "—")],
          ["Last checked", s.checkedAt ? timeAgo(s.checkedAt) : "—"]]} />
        {s.error && <div className="callout warn small">{s.error}</div>}
        {s.phase === "disabled" && <div className="small muted">{s.reason}</div>}
        {s.phase !== "disabled" && (
          <>
            <label className="row small"><input type="checkbox" checked={!!s.autoInstallOnQuit}
              onChange={async (e) => { await bridge().setAutoInstall(e.target.checked); setS({ ...s, autoInstallOnQuit: e.target.checked }); }} />
              Install updates automatically when I close NEXUS</label>
            <label className="row small"><input type="checkbox" checked={!!s.autoRestart}
              onChange={async (e) => { await bridge().setAutoRestart(e.target.checked); setS({ ...s, autoRestart: e.target.checked }); }} />
              When an update is ready, restart and install it automatically after a {s.countdownSeconds ?? 10}-second countdown</label>
            <div><Button size="sm" disabled={checking} onClick={async () => { setChecking(true); setS(await bridge().checkUpdates()); setChecking(false); }}>
              {checking ? "Checking…" : "Check for updates"}</Button></div>
            <div className="small faint">Updates are verified by SHA-256 before they run. Your data (%APPDATA%\NEXUS) and saved keys are never touched by an update.</div>
          </>
        )}
      </div>
    </Card>
  );
}

/** When an update is published: a popup counts down, then NEXUS installs it and restarts itself. "Not now" skips
 *  this version until the next start (it still installs on quit if that is on). Running missions resume. */
export function UpdateCountdown() {
  const [s] = useUpdate();
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [left, setLeft] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const version = s?.phase === "available" ? s.version : null;
  const show = !!version && !!s?.autoRestart && dismissed !== version && !err;

  useEffect(() => { setLeft(show ? Number(s?.countdownSeconds ?? 10) : null); }, [show, version]);  // restart only for a new version
  useEffect(() => {
    if (left === null || applying) return;
    if (left <= 0) {
      setApplying(true);
      void bridge().applyUpdate().then((r) => { if (!r.ok) { setErr(r.error ?? "update failed"); setApplying(false); } });
      return;
    }
    const t = window.setTimeout(() => setLeft(left - 1), 1000);
    return () => window.clearTimeout(t);
  }, [left, applying]);

  if (err && version) {
    return (
      <div className="update-overlay" role="alertdialog" aria-label="Update failed" data-testid="update-popup">
        <div className="update-popup"><h3>Update {version} could not be installed</h3>
          <div className="small">{err}</div>
          <div className="row"><Button onClick={() => { setDismissed(version); setErr(null); }}>OK</Button></div></div>
      </div>
    );
  }
  if (!show || left === null) return null;
  const total = Number(s?.countdownSeconds ?? 10);
  return (
    <div className="update-overlay" role="alertdialog" aria-label="Update ready" data-testid="update-popup">
      <div className="update-popup">
        <h3>NEXUS {version} is ready</h3>
        <div className="small muted">You have {s.current}. {applying ? "Installing — NEXUS will restart by itself." :
          `Restarting to update in ${left} second${left === 1 ? "" : "s"}.`} Your chats, data and keys are kept; running missions pick up where they left off.</div>
        <div className="progress-bar"><div style={{ width: `${applying ? 100 : (100 * (total - left)) / total}%` }} /></div>
        <div className="row">
          <Button variant="primary" disabled={applying} onClick={() => setLeft(0)}>{applying ? "Installing…" : "Update now"}</Button>
          <Button disabled={applying} onClick={() => setDismissed(version)}>Not now</Button>
        </div>
      </div>
    </div>
  );
}
