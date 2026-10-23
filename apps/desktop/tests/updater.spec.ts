// Update flow on the packaged app, without installing anything: a temporary feed offers a newer
// version; NEXUS_UPDATE_DRYRUN records the installer command instead of running it. Opt-in: NEXUS_PACKAGED=1.
import { _electron as electron, expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const instDir = path.resolve(__dirname, "..", "installer");
const latest = fs.existsSync(path.join(instDir, "LATEST.txt")) ? fs.readFileSync(path.join(instDir, "LATEST.txt"), "utf8").trim() : "";
const exe = path.join(instDir, latest, "win-unpacked", "NEXUS.exe");
test.skip(process.env.NEXUS_PACKAGED !== "1", "set NEXUS_PACKAGED=1 after building the installer");

function makeFeed(root: string, version: string, tamper: boolean) {
  const feed = path.join(root, "feed");
  fs.mkdirSync(path.join(feed, "b1"), { recursive: true });
  const bytes = Buffer.from(`fake installer for ${version}`);
  fs.writeFileSync(path.join(feed, "b1", "NEXUS-Setup.exe"), bytes);
  const sha = createHash("sha256").update(tamper ? Buffer.from("something else") : bytes).digest("hex");
  fs.writeFileSync(path.join(feed, "latest.json"), JSON.stringify({ version, installer: "b1/NEXUS-Setup.exe", sha256: sha }));
  return feed;
}

async function launch(root: string, feed: string, extra: Record<string, string> = {}) {
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) if (v !== undefined && !k.startsWith("PYTHON") && k !== "VIRTUAL_ENV") env[k] = v;
  const log = path.join(root, "update-dryrun.json");
  const app = await electron.launch({ executablePath: exe, env: { ...env, NEXUS_TEST: "1", NEXUS_DATA_DIR: path.join(root, "data"),
    NEXUS_LOG_DIR: path.join(root, "logs"), NEXUS_SECRET_BACKEND: "memory", NEXUS_UPDATE_FEED: feed, NEXUS_UPDATE_DRYRUN: log, NEXUS_UPDATE_COUNTDOWN: "30", ...extra } });
  return { app, page: await app.firstWindow(), log };
}

test("newer version: banner, checksum verified, silent in-place install launched", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-upd-"));
  const { app, page, log } = await launch(root, makeFeed(root, "0.2.999", false));
  try {
    const banner = page.getByTestId("update-banner");
    await expect(banner).toContainText("NEXUS 0.2.999 is ready", { timeout: 30_000 });
    await expect(page.getByTestId("service-banner")).toHaveCount(0, { timeout: 30_000 }); // service up
    const bottom = await page.evaluate(() => {
      const e = document.elementFromPoint(600, innerHeight - 5) as HTMLElement;
      return e.closest(".svc-banner") ? "banner" : e.closest("main") || e.closest("aside") ? "content" : e.tagName;
    });
    expect(["content", "DIV"]).toContain(bottom); // no stray banner at the bottom edge (the popup overlay may cover it)
    await expect(page.getByTestId("update-popup")).toContainText("Restarting to update in");
    await page.getByTestId("update-popup").getByRole("button", { name: "Update now" }).click();
    await expect.poll(() => fs.existsSync(log), { timeout: 20_000 }).toBe(true);
    const rec = JSON.parse(fs.readFileSync(log, "utf8"));
    expect(rec.args).toEqual(["--updated", "/S", "--force-run"]);
    expect(rec.file.endsWith(path.join("b1", "NEXUS-Setup.exe"))).toBe(true);
  } finally {
    await app.close();
  }
});

test("tampered installer is refused; same version is up to date", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-upd-"));
  const { app, page, log } = await launch(root, makeFeed(root, "0.2.999", true));
  try {
    const popup = page.getByTestId("update-popup");
    await page.getByTestId("update-popup").getByRole("button", { name: "Update now" }).click({ timeout: 30_000 });
    await expect(popup).toContainText("checksum mismatch");
    expect(fs.existsSync(log)).toBe(false);
    await popup.getByRole("button", { name: "OK" }).click();
    await expect(popup).toHaveCount(0);
  } finally {
    await app.close();
  }
  const root2 = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-upd-"));
  const current = JSON.parse(fs.readFileSync(path.join(instDir, "latest.json"), "utf8")).version;
  const r2 = await launch(root2, makeFeed(root2, current, false));
  try {
    await r2.page.getByRole("button", { name: "Settings" }).click();
    await r2.page.getByRole("tab", { name: "Diagnostics" }).click();
    await expect(r2.page.getByText("up to date")).toBeVisible({ timeout: 30_000 });
    await expect(r2.page.getByText(current, { exact: true })).toBeVisible();
    await expect(r2.page.getByTestId("update-banner")).toHaveCount(0);
  } finally {
    await r2.app.close();
  }
});

test("a newly published build is noticed within seconds and installs itself after the countdown", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-upd-"));
  const current = JSON.parse(fs.readFileSync(path.join(instDir, "latest.json"), "utf8")).version;
  const feed = makeFeed(root, current, false);  // up to date at start
  const { app, page, log } = await launch(root, feed, { NEXUS_UPDATE_COUNTDOWN: "3" });
  try {
    await expect(page.getByRole("heading", { name: "What should NEXUS do?" })).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId("update-popup")).toHaveCount(0);
    makeFeed(root, "0.9.0", false);  // "publish" a new build into the watched folder
    await expect(page.getByTestId("update-popup")).toContainText("NEXUS 0.9.0 is ready", { timeout: 15_000 });
    await expect.poll(() => fs.existsSync(log), { timeout: 15_000 }).toBe(true);  // no click needed
    expect(JSON.parse(fs.readFileSync(log, "utf8")).args).toEqual(["--updated", "/S", "--force-run"]);
  } finally {
    await app.close();
  }
});

test("Not now skips the countdown for that version", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-upd-"));
  const { app, page, log } = await launch(root, makeFeed(root, "0.9.1", false), { NEXUS_UPDATE_COUNTDOWN: "4" });
  try {
    const popup = page.getByTestId("update-popup");
    await popup.getByRole("button", { name: "Not now" }).click({ timeout: 30_000 });
    await expect(popup).toHaveCount(0);
    await page.waitForTimeout(6000);
    expect(fs.existsSync(log)).toBe(false);
    await expect(page.getByTestId("update-banner")).toContainText("NEXUS 0.9.1 is ready");
  } finally {
    await app.close();
  }
});
