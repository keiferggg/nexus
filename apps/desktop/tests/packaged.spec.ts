// Packaged-app smoke test: launches release/win-unpacked/NEXUS.exe (the files the installer installs),
// which runs the bundled standalone service (no Python, no dev tooling). Opt-in: NEXUS_PACKAGED=1.
import { _electron as electron, expect, test } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const instDir = path.resolve(__dirname, "..", "installer");
const latest = fs.existsSync(path.join(instDir, "LATEST.txt")) ? fs.readFileSync(path.join(instDir, "LATEST.txt"), "utf8").trim() : "";
const exe = path.join(instDir, latest, "win-unpacked", process.platform === "win32" ? "NEXUS.exe" : "nexus");

test.skip(process.env.NEXUS_PACKAGED !== "1", "set NEXUS_PACKAGED=1 after building the installer");

test("packaged app runs a demo mission with the bundled service", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-pkg-"));
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) if (v !== undefined && !k.startsWith("PYTHON") && k !== "VIRTUAL_ENV") env[k] = v;
  const app = await electron.launch({ executablePath: exe, args: [],
    env: { ...env, NEXUS_TEST: "1", NEXUS_DATA_DIR: path.join(root, "data"), NEXUS_LOG_DIR: path.join(root, "logs"),
           NEXUS_SECRET_BACKEND: "memory" } });
  const page = await app.firstWindow();
  try {
    await expect(page.getByRole("heading", { name: "Set up NEXUS" })).toBeVisible({ timeout: 60_000 });
    const info = await page.evaluate(() => (window as any).nexus.appInfo());
    expect(info.serviceKind).toBe("packaged");
    await page.getByRole("button", { name: "2. Workspace" }).click();
    await page.getByRole("button", { name: "Create research demo" }).click();
    await page.getByRole("button", { name: /New mission/ }).click();
    await page.getByRole("button", { name: "Create and start mission" }).click();
    await expect(page.locator(".page-head .badge[data-status]").first()).toHaveAttribute("data-status", "completed", { timeout: 90_000 });
  } finally {
    await app.close();
  }
});
