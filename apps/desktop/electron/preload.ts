// Narrow bridge: the renderer can call validated API paths through main and receive events.
// No Node APIs, tokens or credentials are exposed.
import { contextBridge, ipcRenderer } from "electron";

type Listener = (payload: unknown) => void;

function on(channel: string, fn: Listener) {
  const wrapped = (_e: unknown, payload: unknown) => fn(payload);
  ipcRenderer.on(channel, wrapped);
  return () => ipcRenderer.removeListener(channel, wrapped);
}

contextBridge.exposeInMainWorld("nexus", {
  request: (method: string, path: string, body?: unknown) => ipcRenderer.invoke("nexus:request", { method, path, body }),
  serviceState: () => ipcRenderer.invoke("nexus:serviceState"),
  restartService: () => ipcRenderer.invoke("nexus:restartService"),
  selectFolder: () => ipcRenderer.invoke("nexus:selectFolder"),
  selectSaveFile: (suggested: string) => ipcRenderer.invoke("nexus:selectSaveFile", suggested),
  appInfo: () => ipcRenderer.invoke("nexus:appInfo"),
  logTail: () => ipcRenderer.invoke("nexus:logTail"),
  revealLog: () => ipcRenderer.invoke("nexus:revealLog"),
  openActivity: () => ipcRenderer.invoke("nexus:openActivity"),
  onEvent: (fn: Listener) => on("nexus:event", fn),
  onServiceState: (fn: Listener) => on("nexus:serviceState", fn),
  onStream: (fn: Listener) => on("nexus:stream", fn),
  testKillService: () => ipcRenderer.invoke("nexus:test:killService"),
  updateState: () => ipcRenderer.invoke("nexus:updateState"),
  checkUpdates: () => ipcRenderer.invoke("nexus:checkUpdates"),
  applyUpdate: () => ipcRenderer.invoke("nexus:applyUpdate"),
  setAutoRestart: (on: boolean) => ipcRenderer.invoke("nexus:setAutoRestart", on),
  setAutoInstall: (on: boolean) => ipcRenderer.invoke("nexus:setAutoInstall", on),
  onUpdate: (fn: Listener) => on("nexus:update", fn),
});
