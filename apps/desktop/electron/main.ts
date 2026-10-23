// Electron main process. The renderer never sees the service token or provider keys: every
// request goes through a narrow, validated IPC proxy implemented here.
import { app, BrowserWindow, dialog, ipcMain, session, shell, WebContents } from "electron";
import path from "node:path";
import fs from "node:fs";
import { resolveServiceCommand, ServiceManager, ServiceState } from "./service";
import { Updater, UpdateState } from "./updater";

const isTest = process.env.NEXUS_TEST === "1";
const dataDir = process.env.NEXUS_DATA_DIR || path.join(app.getPath("userData"), "data");
const logPath = path.join(process.env.NEXUS_LOG_DIR || path.join(app.getPath("userData"), "logs"), "service.log");
let win: BrowserWindow | null = null;
let svc: ServiceManager;
let updater: Updater;
const updatePrefsPath = () => path.join(app.getPath("userData"), "update-prefs.json");
interface UpdatePrefs { autoInstallOnQuit: boolean; autoRestart: boolean }
const DEFAULT_PREFS: UpdatePrefs = { autoInstallOnQuit: true, autoRestart: true };
function updatePrefs(): UpdatePrefs {
  try { return { ...DEFAULT_PREFS, ...JSON.parse(fs.readFileSync(updatePrefsPath(), "utf8")) }; } catch { return { ...DEFAULT_PREFS }; }
}
function saveUpdatePrefs(patch: Partial<UpdatePrefs>): UpdatePrefs {
  const next = { ...updatePrefs(), ...patch };
  fs.writeFileSync(updatePrefsPath(), JSON.stringify(next));
  return next;
}
// Seconds the "restarting to update" popup counts down before installing (tests shorten it).
const UPDATE_COUNTDOWN_S = Math.max(1, Number(process.env.NEXUS_UPDATE_COUNTDOWN || 10));
const withPrefs = (s: UpdateState) => ({ ...s, ...updatePrefs(), countdownSeconds: UPDATE_COUNTDOWN_S });

const ALLOWED_METHODS = new Set(["GET", "POST", "PUT", "DELETE"]);
function validPath(p: unknown): p is string {
  return typeof p === "string" && p.startsWith("/api/") && !p.includes("..") && !p.includes("//") && p.length < 2048
    && !p.startsWith("/api/events/stream");
}

async function proxy(method: string, p: string, body: unknown) {
  if (svc.state.phase !== "ready") {
    return { ok: false, status: 0, error: `service ${svc.state.phase}`, data: null };
  }
  try {
    const res = await fetch(svc.url(p), {
      method,
      headers: { ...svc.authHeader(), "content-type": "application/json" },
      body: method === "GET" || body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(60_000),
    });
    const text = await res.text();
    let data: unknown = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    const error = res.ok ? null : (data as { error?: string })?.error ?? `HTTP ${res.status}`;
    return { ok: res.ok, status: res.status, error, data };
  } catch (err) {
    return { ok: false, status: 0, error: `service unreachable: ${(err as Error).message}`, data: null };
  }
}

// ---- event stream: one SSE connection in main, forwarded to the renderer -------------------
let streamAbort: AbortController | null = null;
let lastEventId = 0;
const subscribers = new Set<WebContents>();

async function runStream() {
  streamAbort?.abort();
  const ctrl = new AbortController();
  streamAbort = ctrl;
  while (!ctrl.signal.aborted) {
    if (svc.state.phase !== "ready") {
      await new Promise((r) => setTimeout(r, 500));
      continue;
    }
    try {
      const res = await fetch(svc.url(`/api/events/stream?after=${lastEventId}`), {
        headers: { ...svc.authHeader(), "last-event-id": String(lastEventId) }, signal: ctrl.signal,
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      broadcast("nexus:stream", { connected: true });
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const data = block.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
          const id = block.split("\n").find((l) => l.startsWith("id: "));
          if (id) lastEventId = Number(id.slice(4));
          if (data) broadcast("nexus:event", JSON.parse(data));
        }
      }
    } catch {
      if (ctrl.signal.aborted) return;
    }
    broadcast("nexus:stream", { connected: false });
    await new Promise((r) => setTimeout(r, 1000));
  }
}

function broadcast(channel: string, payload: unknown) {
  for (const wc of subscribers) {
    if (!wc.isDestroyed()) wc.send(channel, payload);
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 920, minWidth: 1024, minHeight: 680, title: "NEXUS", backgroundColor: "#0f1115",
    show: !isTest || process.env.NEXUS_TEST_SHOW === "1",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      spellcheck: false,
    },
  });
  const wc = win.webContents;
  subscribers.add(wc);
  wc.on("destroyed", () => subscribers.delete(wc));
  wc.setWindowOpenHandler(({ url }) => {
    // Links in answers and results open in the system browser; nothing else ever opens a window.
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  wc.on("will-navigate", (e, url) => {
    if (!url.startsWith("file://") && !url.startsWith(process.env.VITE_DEV_SERVER_URL || "\0")) e.preventDefault();
  });
  if (process.env.VITE_DEV_SERVER_URL) void win.loadURL(process.env.VITE_DEV_SERVER_URL);
  else void win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
}

function registerIpc() {
  ipcMain.handle("nexus:request", async (e, req: { method: string; path: string; body?: unknown }) => {
    if (!subscribers.has(e.sender)) return { ok: false, status: 0, error: "unknown sender", data: null };
    const method = String(req?.method || "GET").toUpperCase();
    if (!ALLOWED_METHODS.has(method) || !validPath(req?.path)) {
      return { ok: false, status: 0, error: "request rejected by IPC policy", data: null };
    }
    return proxy(method, req.path, req.body);
  });
  ipcMain.handle("nexus:serviceState", () => svc.state);
  ipcMain.handle("nexus:restartService", async () => svc.restart());
  ipcMain.handle("nexus:selectFolder", async () => {
    const r = await dialog.showOpenDialog(win!, { properties: ["openDirectory", "createDirectory"] });
    return r.canceled ? null : r.filePaths[0];
  });
  ipcMain.handle("nexus:selectSaveFile", async (_e, suggested: string) => {
    const r = await dialog.showSaveDialog(win!, { defaultPath: suggested, filters: [{ name: "Backup", extensions: ["zip"] }] });
    return r.canceled ? null : r.filePath;
  });
  ipcMain.handle("nexus:appInfo", () => ({ dataDir, logPath, version: app.getVersion(), platform: process.platform,
    serviceKind: resolveServiceCommand(app.getAppPath(), process.resourcesPath, app.isPackaged).kind }));
  ipcMain.handle("nexus:logTail", () => svc.logTail(20000));
  ipcMain.handle("nexus:revealLog", () => shell.showItemInFolder(logPath));
  ipcMain.handle("nexus:openActivity", () => shell.openPath(process.env.NEXUS_ACTIVITY_DIR || path.join(dataDir, "..", "activity")));
  ipcMain.handle("nexus:updateState", () => withPrefs(updater.state));
  ipcMain.handle("nexus:checkUpdates", async () => withPrefs(await updater.check()));
  ipcMain.handle("nexus:setAutoInstall", (_e, on: boolean) => saveUpdatePrefs({ autoInstallOnQuit: !!on }));
  ipcMain.handle("nexus:setAutoRestart", (_e, on: boolean) => saveUpdatePrefs({ autoRestart: !!on }));
  ipcMain.handle("nexus:applyUpdate", async () => {
    // Stop the service cleanly first (missions resume after restart via reconciliation).
    await svc.stop();
    try {
      await updater.apply(app.getPath("temp"), true, process.env.NEXUS_UPDATE_DRYRUN);
    } catch (err) {
      void svc.start();
      return { ok: false, error: (err as Error).message };
    }
    if (!process.env.NEXUS_UPDATE_DRYRUN) setTimeout(() => app.exit(0), 300);
    return { ok: true };
  });
  if (isTest) {
    ipcMain.handle("nexus:test:killService", () => { svc.killForTest(); return true; });
  }
}

app.whenReady().then(async () => {
  // Deny every permission request (camera, notifications, ...); NEXUS needs none.
  session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
  session.defaultSession.webRequest.onHeadersReceived((details, cb) => {
    cb({ responseHeaders: { ...details.responseHeaders, "Content-Security-Policy": [
      "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    ] } });
  });
  const cmd = resolveServiceCommand(app.getAppPath(), process.resourcesPath, app.isPackaged);
  const extraEnv: Record<string, string> = {};
  if (process.env.NEXUS_SECRET_BACKEND) extraEnv.NEXUS_SECRET_BACKEND = process.env.NEXUS_SECRET_BACKEND;
  svc = new ServiceManager(cmd, dataDir, logPath, extraEnv);
  svc.on("state", (s: ServiceState) => broadcast("nexus:serviceState", s));
  let feed: string | null = process.env.NEXUS_UPDATE_FEED || null;
  try {
    feed = feed || JSON.parse(fs.readFileSync(path.join(app.getAppPath(), "package.json"), "utf8")).nexusUpdateFeed || null;
  } catch { /* no packaged metadata */ }
  updater = new Updater(app.getVersion(), feed, app.isPackaged || !!process.env.NEXUS_UPDATE_FEED,
    app.isPackaged ? "" : "running from source: updates come from your working copy");
  updater.on("state", (s: UpdateState) => broadcast("nexus:update", withPrefs(s)));
  registerIpc();
  createWindow();
  void svc.start();
  void runStream();
  updater.start();
});

let quitting = false;
app.on("before-quit", (e) => {
  if (quitting) return;
  e.preventDefault();
  quitting = true;
  streamAbort?.abort();
  updater?.stop();
  void svc.stop().finally(async () => {
    // Install a pending update on exit (silent, no relaunch) unless the owner turned that off.
    if (updater?.state.phase === "available" && updatePrefs().autoInstallOnQuit && !process.env.NEXUS_UPDATE_DRYRUN) {
      try { await updater.apply(app.getPath("temp"), false); } catch { /* keep the current version */ }
    }
    app.quit();
  });
});
app.on("window-all-closed", () => app.quit());
