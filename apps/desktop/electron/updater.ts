// In-place updates for the installed app.
//
// Feed: a folder or https URL containing latest.json:
//   {"version": "0.2.7", "installer": "<relative path or URL>", "sha256": "...", "released_at": "..."}
// The default feed is baked in at build time (package.json "nexusUpdateFeed"); NEXUS_UPDATE_FEED overrides it.
// Applying an update runs the NSIS installer silently over the existing per-user install. User data
// (%APPDATA%/NEXUS: database, artifacts, settings) is not touched; credentials stay in Windows
// Credential Manager. Interrupted missions resume through the service's startup reconciliation.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import path from "node:path";

export type UpdateState =
  | { phase: "disabled"; reason: string; current: string }
  | { phase: "idle"; current: string; feed: string; checkedAt?: string; error?: string }
  | { phase: "available"; current: string; feed: string; version: string; installer: string; sha256: string; releasedAt?: string }
  | { phase: "applying"; current: string; version: string };

interface Manifest { version: string; installer: string; sha256: string; released_at?: string }

export function compareVersions(a: string, b: string): number {
  const pa = a.split(/[.+-]/).map((x) => parseInt(x, 10) || 0);
  const pb = b.split(/[.+-]/).map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

export class Updater extends EventEmitter {
  state: UpdateState;
  private timer: NodeJS.Timeout | null = null;

  constructor(private current: string, private feed: string | null, enabled: boolean, reason = "") {
    super();
    this.state = enabled && feed ? { phase: "idle", current, feed } : { phase: "disabled", current, reason: reason || "no update feed configured" };
  }

  private set(s: UpdateState) {
    this.state = s;
    this.emit("state", s);
  }

  private watcher: fs.FSWatcher | null = null;
  private debounce: NodeJS.Timeout | null = null;

  /** Checks now, every intervalMs, and, for a folder feed, as soon as latest.json changes (a new build is noticed
   *  within seconds of being published). */
  start(intervalMs = 2 * 60_000) {
    if (this.state.phase === "disabled") return;
    void this.check();
    this.timer = setInterval(() => void this.check(), intervalMs);
    if (this.feed && !this.isUrl(this.feed)) {
      try {
        this.watcher = fs.watch(this.feed, (_ev, name) => {
          if (name && name !== "latest.json") return;
          if (this.debounce) clearTimeout(this.debounce);
          this.debounce = setTimeout(() => void this.check(), 1500);  // latest.json is replaced by rename
        });
      } catch { /* folder missing or not watchable: polling still works */ }
    }
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    if (this.debounce) clearTimeout(this.debounce);
    this.watcher?.close();
  }

  private isUrl(p: string) {
    return /^https?:\/\//i.test(p);
  }

  private async readManifest(): Promise<Manifest> {
    const feed = this.feed!;
    if (this.isUrl(feed)) {
      const r = await fetch(feed.replace(/\/?$/, "/") + "latest.json", { signal: AbortSignal.timeout(20_000) });
      if (!r.ok) throw new Error(`feed HTTP ${r.status}`);
      return (await r.json()) as Manifest;
    }
    return JSON.parse(fs.readFileSync(path.join(feed, "latest.json"), "utf8")) as Manifest;
  }

  async check(): Promise<UpdateState> {
    if (this.state.phase === "disabled" || this.state.phase === "applying") return this.state;
    try {
      const m = await this.readManifest();
      if (!m.version || !m.installer || !/^[0-9a-f]{64}$/i.test(m.sha256 || "")) throw new Error("invalid latest.json");
      if (compareVersions(m.version, this.current) > 0) {
        this.set({ phase: "available", current: this.current, feed: this.feed!, version: m.version, installer: m.installer,
          sha256: m.sha256.toLowerCase(), releasedAt: m.released_at });
      } else {
        this.set({ phase: "idle", current: this.current, feed: this.feed!, checkedAt: new Date().toISOString() });
      }
    } catch (err) {
      this.set({ phase: "idle", current: this.current, feed: this.feed!, checkedAt: new Date().toISOString(),
        error: `could not check for updates: ${(err as Error).message}` });
    }
    return this.state;
  }

  /** Resolve the installer to a local file and verify its SHA-256 before it may run. */
  private async localInstaller(s: Extract<UpdateState, { phase: "available" }>, tempDir: string): Promise<string> {
    let file: string;
    if (this.isUrl(s.installer) || this.isUrl(this.feed!)) {
      const url = this.isUrl(s.installer) ? s.installer : this.feed!.replace(/\/?$/, "/") + s.installer;
      const r = await fetch(url, { signal: AbortSignal.timeout(15 * 60_000) });
      if (!r.ok) throw new Error(`download failed: HTTP ${r.status}`);
      file = path.join(tempDir, `NEXUS-Setup-${s.version}.exe`);
      fs.writeFileSync(file, Buffer.from(await r.arrayBuffer()));
    } else {
      file = path.resolve(this.feed!, s.installer);
      if (!file.startsWith(path.resolve(this.feed!))) throw new Error("installer path escapes the update folder");
    }
    const digest = createHash("sha256").update(fs.readFileSync(file)).digest("hex");
    if (digest !== s.sha256) throw new Error("installer checksum mismatch; update refused");
    return file;
  }

  /** Runs the installer silently over this install. relaunch=true restarts NEXUS afterwards. */
  async apply(tempDir: string, relaunch: boolean, dryRunLog?: string): Promise<void> {
    const s = this.state;
    if (s.phase !== "available") throw new Error("no update available");
    const file = await this.localInstaller(s, tempDir);
    const args = ["--updated", "/S", ...(relaunch ? ["--force-run"] : [])];
    this.set({ phase: "applying", current: this.current, version: s.version });
    if (dryRunLog) {  // tests: record instead of installing
      fs.writeFileSync(dryRunLog, JSON.stringify({ file, args }));
      return;
    }
    const child = spawn(file, args, { detached: true, stdio: "ignore", windowsHide: true });
    child.unref();
  }
}
