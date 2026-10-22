// Renderer API layer. All calls go through the preload bridge to the main process, which holds
// the service token. There is deliberately NO fallback or fixture data: failures surface as errors.
import { useCallback, useEffect, useRef, useState } from "react";

export interface NexusEvent {
  seq: number;
  id: string;
  type: string;
  actor: string;
  mission_id: string | null;
  task_id: string | null;
  project_id: string | null;
  payload: Record<string, any>;
  created_at: string;
}

export type ServiceState =
  | { phase: "starting"; detail: string }
  | { phase: "ready"; port: number; pid: number; version: string }
  | { phase: "crashed"; detail: string; exitCode: number | null; logTail: string; restarts: number }
  | { phase: "failed"; detail: string; logTail: string; hint: string }
  | { phase: "stopped" };

interface Bridge {
  request(method: string, path: string, body?: unknown): Promise<{ ok: boolean; status: number; error: string | null; data: any }>;
  serviceState(): Promise<ServiceState>;
  restartService(): Promise<ServiceState>;
  selectFolder(): Promise<string | null>;
  selectSaveFile(suggested: string): Promise<string | null>;
  appInfo(): Promise<{ dataDir: string; logPath: string; version: string; platform: string; serviceKind: string }>;
  logTail(): Promise<string>;
  revealLog(): Promise<void>;
  openActivity(): Promise<string>;
  onEvent(fn: (e: NexusEvent) => void): () => void;
  onServiceState(fn: (s: ServiceState) => void): () => void;
  onStream(fn: (s: { connected: boolean }) => void): () => void;
  testKillService?(): Promise<boolean>;
  updateState(): Promise<any>;
  checkUpdates(): Promise<any>;
  applyUpdate(): Promise<{ ok: boolean; error?: string }>;
  setAutoInstall(on: boolean): Promise<any>;
  onUpdate(fn: (s: any) => void): () => void;
}

declare global {
  interface Window {
    nexus?: Bridge;
  }
}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

export function bridge(): Bridge {
  if (!window.nexus) throw new ApiError("NEXUS desktop bridge unavailable (open the app, not the web page)", 0);
  return window.nexus;
}

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await bridge().request(method, path, body);
  if (!r.ok) throw new ApiError(r.error || `request failed (${r.status})`, r.status);
  return r.data as T;
}
export const get = <T = any>(p: string) => api<T>("GET", p);
export const post = <T = any>(p: string, b?: unknown) => api<T>("POST", p, b ?? {});
export const put = <T = any>(p: string, b?: unknown) => api<T>("PUT", p, b ?? {});
export const del = <T = any>(p: string) => api<T>("DELETE", p);

// ---- event bus ---------------------------------------------------------------------------
type EvListener = (e: NexusEvent) => void;
const listeners = new Set<EvListener>();
let wired = false;
function wire() {
  if (wired || !window.nexus) return;
  wired = true;
  window.nexus.onEvent((e) => listeners.forEach((l) => l(e)));
}
export function onEvent(fn: EvListener): () => void {
  wire();
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Fetch `path`, refetching (debounced) whenever an event matching `match` arrives. */
export function useApi<T = any>(path: string | null, match?: (e: NexusEvent) => boolean, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path);
  const timer = useRef<number | null>(null);
  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await get<T>(path);
      setData(d);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setData(null); // never show stale data as if it were current
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    setLoading(!!path);
    setData(null);
    void load();
    if (!match) return;
    return onEvent((e) => {
      if (!match(e)) return;
      if (timer.current) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => void load(), 250);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, load, ...deps]);
  return { data, error, loading, reload: load };
}

export function fmtUsd(v: number | null | undefined, digits = 4) {
  if (v === null || v === undefined) return "—";
  return `$${v.toFixed(digits)}`;
}

export function shortId(id: string | null | undefined) {
  if (!id) return "—";
  const [p, rest] = id.split("_");
  return rest ? `${p}_${rest.slice(0, 6)}` : id.slice(0, 10);
}

export function timeAgo(iso: string | null | undefined) {
  if (!iso) return "—";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 60) return `${Math.max(0, Math.round(d))}s ago`;
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}
