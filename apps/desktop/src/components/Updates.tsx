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
            <div><Button size="sm" disabled={checking} onClick={async () => { setChecking(true); setS({ ...(await bridge().checkUpdates()), autoInstallOnQuit: s.autoInstallOnQuit }); setChecking(false); }}>
              {checking ? "Checking…" : "Check for updates"}</Button></div>
            <div className="small faint">Updates are verified by SHA-256 before they run. Your data (%APPDATA%\NEXUS) and saved keys are never touched by an update.</div>
          </>
        )}
      </div>
    </Card>
  );
}
