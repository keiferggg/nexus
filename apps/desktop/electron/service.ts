// Service lifecycle: start/stop the local NEXUS service, discover its authenticated loopback
// port, readiness, crash detection with bounded auto-restart, and log capture.
import { ChildProcess, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import path from "node:path";

export type ServiceState =
  | { phase: "starting"; detail: string }
  | { phase: "ready"; port: number; pid: number; version: string }
  | { phase: "crashed"; detail: string; exitCode: number | null; logTail: string; restarts: number }
  | { phase: "failed"; detail: string; logTail: string; hint: string }
  | { phase: "stopped" };

export interface ServiceCommand {
  command: string;
  args: string[];
  cwd?: string;
  kind: "packaged" | "development";
}

export class ServiceManager extends EventEmitter {
  private proc: ChildProcess | null = null;
  private token = randomBytes(32).toString("base64url");
  private port = 0;
  private restarts = 0;
  private stopping = false;
  private logStream: fs.WriteStream;
  state: ServiceState = { phase: "stopped" };

  constructor(private cmd: ServiceCommand, private dataDir: string, private logPath: string,
              private extraEnv: Record<string, string> = {}) {
    super();
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    this.logStream = fs.createWriteStream(logPath, { flags: "a" });
  }

  private setState(s: ServiceState) {
    this.state = s;
    this.emit("state", s);
  }

  logTail(bytes = 6000): string {
    try {
      const st = fs.statSync(this.logPath);
      const fd = fs.openSync(this.logPath, "r");
      const len = Math.min(bytes, st.size);
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, st.size - len);
      fs.closeSync(fd);
      return buf.toString("utf8");
    } catch {
      return "";
    }
  }

  start(): Promise<ServiceState> {
    this.stopping = false;
    this.setState({ phase: "starting", detail: `launching ${this.cmd.kind} service` });
    return new Promise((resolve) => {
      // Token travels only via the environment of this child, never argv or logs.
      const env = { ...process.env, ...this.extraEnv, NEXUS_AUTH_TOKEN: this.token, PYTHONUNBUFFERED: "1" };
      let settled = false;
      const done = (s: ServiceState) => {
        if (!settled) {
          settled = true;
          resolve(s);
        }
      };
      let proc: ChildProcess;
      try {
        proc = spawn(this.cmd.command, [...this.cmd.args, "serve", "--data-dir", this.dataDir], {
          cwd: this.cmd.cwd, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
        });
      } catch (err) {
        const s: ServiceState = { phase: "failed", detail: `could not launch service: ${String(err)}`, logTail: "",
          hint: "Reinstall NEXUS, or in development create services/.venv (see README)." };
        this.setState(s);
        return done(s);
      }
      this.proc = proc;
      const timer = setTimeout(() => {
        const s: ServiceState = { phase: "failed", detail: "service did not become ready within 45 s",
          logTail: this.logTail(), hint: "See the service log for the startup error." };
        this.setState(s);
        proc.kill();
        done(s);
      }, 45_000);
      let buf = "";
      proc.stdout!.on("data", (chunk: Buffer) => {
        this.logStream.write(chunk);
        buf += chunk.toString("utf8");
        let idx: number;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (line.startsWith("NEXUS_READY ")) {
            clearTimeout(timer);
            const info = JSON.parse(line.slice(12));
            this.port = info.port;
            const s: ServiceState = { phase: "ready", port: info.port, pid: info.pid, version: info.version };
            this.setState(s);
            done(s);
          }
        }
      });
      // Always drain stderr; a full pipe would block the service.
      proc.stderr!.on("data", (chunk: Buffer) => this.logStream.write(chunk));
      proc.on("error", (err) => {
        clearTimeout(timer);
        const s: ServiceState = { phase: "failed", detail: `service process error: ${err.message}`,
          logTail: this.logTail(), hint: "The service executable may be missing or blocked by security software." };
        this.setState(s);
        done(s);
      });
      proc.on("exit", (code) => {
        clearTimeout(timer);
        this.proc = null;
        if (this.stopping) {
          this.setState({ phase: "stopped" });
          return done(this.state);
        }
        const tail = this.logTail();
        const locked = /already using/.test(tail);
        if (!settled) {
          const s: ServiceState = { phase: "failed", detail: `service exited during startup (code ${code})`,
            logTail: tail, hint: locked ? "Another NEXUS instance is using this data folder. Close it and retry."
              : "See the service log; fix the cause, then use Restart service." };
          this.setState(s);
          return done(s);
        }
        this.setState({ phase: "crashed", detail: `service exited unexpectedly (code ${code})`, exitCode: code,
          logTail: tail, restarts: this.restarts });
        if (this.restarts < 3) {
          this.restarts += 1;
          setTimeout(() => void this.start(), 1000 * this.restarts);
        }
      });
    });
  }

  async stop(): Promise<void> {
    this.stopping = true;
    const p = this.proc;
    if (!p) return;
    await new Promise<void>((resolve) => {
      const t = setTimeout(() => { p.kill("SIGKILL"); resolve(); }, 8000);
      p.once("exit", () => { clearTimeout(t); resolve(); });
      p.kill();
    });
  }

  async restart(): Promise<ServiceState> {
    this.restarts = 0;
    await this.stop();
    return this.start();
  }

  /** Test hook: kill the service abruptly (used by UI truthfulness tests). */
  killForTest() {
    this.proc?.kill("SIGKILL");
  }

  url(p: string) {
    return `http://127.0.0.1:${this.port}${p}`;
  }

  authHeader() {
    return { authorization: `Bearer ${this.token}` };
  }
}

export function resolveServiceCommand(appRoot: string, resourcesPath: string, isPackaged: boolean): ServiceCommand {
  if (process.env.NEXUS_SERVICE_CMD) {
    const parts = JSON.parse(process.env.NEXUS_SERVICE_CMD) as string[];
    return { command: parts[0], args: parts.slice(1), kind: "development" };
  }
  const packaged = path.join(resourcesPath, "nexus-service", process.platform === "win32" ? "nexus-service.exe" : "nexus-service");
  if (isPackaged || fs.existsSync(packaged)) {
    return { command: packaged, args: [], kind: "packaged" };
  }
  const repo = path.resolve(appRoot, "..", "..");
  const py = process.platform === "win32" ? path.join(repo, "services", ".venv", "Scripts", "python.exe")
    : path.join(repo, "services", ".venv", "bin", "python");
  return { command: py, args: ["-m", "nexus"], cwd: path.join(repo, "services", "nexus"), kind: "development" };
}
