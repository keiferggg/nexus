// End-to-end UI tests against the real Electron app and real local service (offline fake provider).
import { _electron as electron, ElectronApplication, expect, Page, test } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const appDir = path.resolve(__dirname, "..");
const repo = path.resolve(appDir, "..", "..");
const shots = path.join(repo, "docs", "screenshots");
fs.mkdirSync(shots, { recursive: true });

async function launch(dataDir: string): Promise<{ app: ElectronApplication; page: Page }> {
  const app = await electron.launch({
    args: ["."], cwd: appDir,
    env: { ...process.env, NEXUS_TEST: "1", NEXUS_DATA_DIR: dataDir, NEXUS_LOG_DIR: path.join(dataDir, "..", "logs"),
           NEXUS_SECRET_BACKEND: "memory" },
  });
  const page = await app.firstWindow();
  await page.setViewportSize({ width: 1440, height: 920 });
  return { app, page };
}

const missionStatus = (page: Page) => page.locator(".page-head .badge[data-status]").first();

async function demoProject(page: Page) {
  await expect(page.getByRole("heading", { name: "Set up NEXUS" })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("button", { name: "2. Workspace" }).click();
  await page.getByRole("button", { name: "Create research demo" }).click();
  await expect(page.getByRole("heading", { name: "Add a provider" })).toBeVisible();
}

test("demo mission end-to-end, service crash truthfulness, restart persistence", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-ui-"));
  const dataDir = path.join(root, "data");
  let { app, page } = await launch(dataDir);

  await expect(page.getByRole("heading", { name: "Set up NEXUS" })).toBeVisible({ timeout: 60_000 });
  await page.screenshot({ path: path.join(shots, "01-onboarding.png") });
  await page.getByRole("button", { name: "2. Workspace" }).click();
  await page.getByRole("button", { name: "Create research demo" }).click();
  await expect(page.getByRole("heading", { name: "Add a provider" })).toBeVisible();

  await page.getByRole("button", { name: /New mission/ }).click();
  await expect(page.getByRole("heading", { name: "New mission" })).toBeVisible();
  await page.getByRole("button", { name: "Create and start mission" }).click();

  await expect(missionStatus(page)).toHaveAttribute("data-status", "completed", { timeout: 90_000 });
  await expect(page.getByText("DEMO · offline fake").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Accepted final deliverable" })).toBeVisible();
  await page.screenshot({ path: path.join(shots, "02-mission-control.png"), fullPage: true });

  // Task drawer: causal chain and reviews (research was rejected once for shared lineage).
  await page.locator("[data-testid^=task-research]").click();
  await expect(page.getByRole("heading", { name: "Why is this running?" })).toBeVisible();
  await expect(page.locator(".drawer [data-status=rejected]").first()).toBeVisible();
  await page.screenshot({ path: path.join(shots, "03-task-drawer.png") });
  await page.getByRole("button", { name: "Close" }).click();

  await page.getByRole("tab", { name: /Agent room/ }).click();
  await page.getByRole("button", { name: /Write a one-page/ }).click();
  await expect(page.locator("[data-testid=room-message]").first()).toBeVisible();
  await page.screenshot({ path: path.join(shots, "04-agent-room.png") });

  await page.getByRole("tab", { name: /Shared understanding/ }).click();
  await expect(page.getByText("1 independent source").first()).toBeVisible();
  await page.screenshot({ path: path.join(shots, "05-understanding.png"), fullPage: true });

  await page.getByRole("tab", { name: /Artifacts/ }).click();
  await page.locator("[data-testid=artifact-row]").first().click();
  await expect(page.getByText("sha256 verified")).toBeVisible();
  await page.screenshot({ path: path.join(shots, "06-artifacts.png") });

  await page.getByRole("tab", { name: /Timeline/ }).click();
  await expect(page.locator("[data-testid=timeline-row]").first()).toBeVisible();

  for (const [nav, shot] of [["Connected services", "07-connections"], ["Goals & objectives", "08-objectives"],
    ["Tool workshop", "09-tools"], ["Agent studio", "10-studio"]] as const) {
    await page.getByRole("button", { name: nav }).click();
    await expect(page.getByRole("heading", { name: nav, exact: true })).toBeVisible();
    await page.screenshot({ path: path.join(shots, `${shot}.png`), fullPage: true });
  }

  // Internet access: every agent's tools are listed; pre-approving writes shows an explicit warning.
  await page.getByRole("button", { name: "Connected services" }).click();
  await expect(page.getByRole("heading", { name: "Internet access (all agents)" })).toBeVisible();
  await page.getByLabel("Web writes (POST, forms)").selectOption("auto");
  await expect(page.getByText("Pre-approved writes let agents post")).toBeVisible();
  await page.getByLabel("Contact for the User-Agent").fill("owner@example.org");
  await page.getByRole("button", { name: "Save internet settings" }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await expect(page.getByTestId("connector-web").getByText("send_request")).toBeVisible();
  await page.screenshot({ path: path.join(shots, "14-internet-access.png"), fullPage: true });

  // UI truthfulness: kill the service; the app must say so instead of showing stale data as live.
  await page.evaluate(() => (window as any).nexus.testKillService());
  await expect(page.getByTestId("service-banner")).toBeVisible({ timeout: 15_000 });
  await page.screenshot({ path: path.join(shots, "11-service-down.png") });
  await expect(page.getByTestId("service-banner")).toBeHidden({ timeout: 60_000 }); // auto-restart

  // Restart the whole app on the same data folder: identities and mission state persist.
  await app.close();
  ({ app, page } = await launch(dataDir));
  await expect(page.getByRole("button", { name: /Larkspur bike-share brief/ })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("button", { name: /Larkspur bike-share brief/ }).click();
  await expect(missionStatus(page)).toHaveAttribute("data-status", "completed");
  await app.close();
});

test("approved real action through an MCP connector, then objective discovery", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-ui-"));
  const { app, page } = await launch(path.join(root, "data"));
  await demoProject(page);

  await page.getByRole("button", { name: "Connected services" }).click();
  await page.getByLabel("Kind").selectOption("mcp");
  const py = path.join(repo, "services", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const server = path.join(repo, "fixtures", "mcp-demo", "notes_server.py");
  const store = path.join(root, "notes.json");
  await page.getByLabel("Configuration (JSON)").fill(JSON.stringify({ transport: "stdio", command: py, args: [server, store] }));
  await page.getByLabel("Label").fill("Team notes");
  await page.getByRole("button", { name: "Connect and discover actions" }).click();
  await expect(page.getByTestId("connector-mcp").getByText("add_note")).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: /New mission/ }).click();
  await page.getByLabel("Title").fill("Tell the team");
  await page.getByLabel("Objective").fill("Share resident-demand findings with the team");
  const sc = page.getByRole("textbox", { name: "Success conditions (measurable)" });
  await sc.first().fill("Research resident demand using workspace sources");
  await page.getByRole("button", { name: "＋ Add" }).first().click();
  await sc.nth(1).fill("Notify the team by posting a note with the findings");
  await page.getByRole("button", { name: "Create and start mission" }).click();

  const item = page.locator("[data-testid=attention-item]").filter({ hasText: "add_note" });
  await expect(item).toBeVisible({ timeout: 90_000 });
  expect(fs.existsSync(store)).toBe(false); // nothing happened before approval
  await page.screenshot({ path: path.join(shots, "12-action-approval.png"), fullPage: true });
  await item.getByRole("button", { name: "Approve & run" }).click();
  await expect(missionStatus(page)).toHaveAttribute("data-status", "completed", { timeout: 90_000 });
  expect(JSON.parse(fs.readFileSync(store, "utf8")).length).toBe(1);

  await page.getByRole("button", { name: "Goals & objectives" }).click();
  await page.getByLabel("One goal per line").fill("Help Larkspur decide on bike-share with sound evidence");
  await page.getByRole("button", { name: "Save goals" }).click();
  await expect(page.locator("section", { has: page.getByRole("heading", { name: "Standing goals" }) })
    .getByText("G1", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Run discovery (offline)" }).click();
  await expect(missionStatus(page)).toHaveAttribute("data-status", "completed", { timeout: 90_000 });
  await page.getByRole("button", { name: "Goals & objectives" }).click();
  await expect(page.locator("[data-testid=objective-row]").first()).toBeVisible();
  await page.screenshot({ path: path.join(shots, "13-objectives.png"), fullPage: true });
  await page.getByRole("button", { name: "Adopt (offline)" }).first().click();
  await expect(page.getByRole("heading", { name: /Close the evidence gap/ })).toBeVisible({ timeout: 30_000 });
  await app.close();
});

test("unattended mission acts on its own and reports in the digest", async () => {
  const http = await import("node:http");
  const received: string[] = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => { if (req.method === "POST") received.push(body); res.writeHead(201, { "content-type": "application/json" }); res.end("{}"); });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  const port = (server.address() as any).port;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-ui-"));
  const { app, page } = await launch(path.join(root, "data"));
  try {
    await demoProject(page);
    await page.getByRole("button", { name: "Connected services" }).click();
    await page.getByLabel(/Unattended/).check();
    await expect(page.getByText("A web page cannot add a destination")).toBeVisible();
    await page.getByRole("button", { name: "Save autonomy" }).click();
    await expect(page.getByText("Saved", { exact: true }).first()).toBeVisible();
    await page.getByLabel("Allowed local-network hosts").fill("127.0.0.1"); // test server is on loopback
    await page.getByRole("button", { name: "Save internet settings" }).click();
    await page.screenshot({ path: path.join(shots, "15-autonomy.png"), fullPage: true });

    await page.getByRole("button", { name: /New mission/ }).click();
    await expect(page.getByRole("heading", { name: "Unattended destinations" })).toBeVisible();
    await page.getByLabel("Title").fill("Unattended brief");
    await page.getByLabel("Objective").fill(`Post the resident-demand findings to http://127.0.0.1:${port}/notes`);
    const sc = page.getByRole("textbox", { name: "Success conditions (measurable)" });
    await sc.first().fill("Research resident demand using workspace sources");
    await page.getByRole("button", { name: "＋ Add" }).first().click();
    await sc.nth(1).fill(`Post the findings to http://127.0.0.1:${port}/notes`);
    await page.getByRole("button", { name: "Create and start mission" }).click();
    await expect(missionStatus(page)).toHaveAttribute("data-status", "completed", { timeout: 90_000 });
    expect(received.length).toBe(1);

    // No approval was requested; the overview digest reports what the agents did.
    await page.getByRole("button", { name: "Overview" }).click();
    const digest = page.getByTestId("digest");
    await expect(digest.getByText("1 actions taken")).toBeVisible();
    await expect(digest.getByText("0 waiting for you")).toBeVisible();
    await page.screenshot({ path: path.join(shots, "16-digest.png"), fullPage: true });
  } finally {
    await app.close();
    server.close();
  }
});

test("OpenAI-compatible provider: save, discover models, capability check", async () => {
  const http = await import("node:http");
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      res.setHeader("content-type", "application/json");
      if (req.method === "GET") { res.end(JSON.stringify({ data: [{ id: "local-model-7b" }] })); return; }
      const b = JSON.parse(raw);
      const toolMsg = b.messages.find((m: any) => m.role === "tool");
      let message: any;
      if (b.tools && !toolMsg) message = { role: "assistant", content: null, tool_calls: [{ id: "c1", type: "function", function: { name: "get_secret_number", arguments: "{\"label\":\"alpha\"}" } }] };
      else if (toolMsg) message = { role: "assistant", content: JSON.stringify({ secret: JSON.parse(toolMsg.content).secret }) };
      else message = { role: "assistant", content: "{\"answer\": 4}" };
      res.end(JSON.stringify({ id: "x", model: b.model, choices: [{ index: 0, message, finish_reason: "stop" }], usage: { prompt_tokens: 20, completion_tokens: 5 } }));
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  const port = (server.address() as any).port;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-ui-"));
  const { app, page } = await launch(path.join(root, "data"));
  try {
    await demoProject(page);
    await page.getByLabel("Provider", { exact: true }).selectOption("openai");
    await page.getByLabel("Label", { exact: true }).fill("Local OpenAI-compatible");
    await page.getByLabel("Base URL").fill(`http://127.0.0.1:${port}`);
    await page.getByLabel("API key").fill("local-dummy-key");
    await page.getByRole("button", { name: "Save and test connection" }).click();
    const card = page.getByTestId("provider-openai");
    await expect(card.getByText("1 model(s) discovered")).toBeVisible({ timeout: 20_000 });
    await card.getByLabel(/model to check/).selectOption("local-model-7b");
    await card.getByRole("button", { name: "Check capabilities" }).click();
    await expect(card.getByTestId("capability-result")).toContainText("Ready for NEXUS", { timeout: 30_000 });
    await page.screenshot({ path: path.join(shots, "17-capability-check.png"), fullPage: true });
    await page.getByRole("button", { name: "4. Models" }).click();
    await page.getByLabel("Default provider").selectOption({ label: "Local OpenAI-compatible" });
    await page.getByLabel("Default model").selectOption("local-model-7b");
    await expect(page.getByText("Saved", { exact: true })).toBeVisible();
    await expect(page.getByText("has no price yet")).toBeVisible(); // asked right where the model is chosen

    // New mission: Live stays disabled and shows exactly what's missing until the model is priced.
    await page.getByRole("button", { name: /New mission/ }).click();
    const live = page.getByRole("radio", { name: "Live models" });
    await expect(live).toBeDisabled();
    const needs = page.getByTestId("live-readiness");
    await expect(needs).toContainText("Price needed for local-model-7b");
    await needs.getByLabel("input price for local-model-7b").fill("0.5");
    await needs.getByLabel("output price for local-model-7b").fill("1.5");
    await needs.getByRole("button", { name: "Save price" }).click();
    await expect(live).toBeEnabled({ timeout: 10_000 });
    await page.screenshot({ path: path.join(shots, "18-live-ready.png"), fullPage: true });
  } finally {
    await app.close();
    server.close();
  }
});

test("setup wizard: choosing Unattended and Read and write sticks without pressing Save", async () => {
  // Seen on the owner's machine: picking Unattended in the wizard and pressing Continue silently dropped it.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-ui-"));
  const { app, page } = await launch(path.join(root, "data"));
  try {
    await demoProject(page);
    await page.getByRole("button", { name: "6. Internet & autonomy" }).click();
    await page.getByLabel("Internet mode").selectOption("read_write");
    await page.getByLabel(/Unattended/).check();
    await expect(page.getByText("Saved", { exact: true }).first()).toBeVisible();
    await page.getByRole("button", { name: "Continue" }).click(); // no Save click
    await page.getByRole("button", { name: /New mission/ }).click();
    await expect(page.getByTestId("autonomy-mode")).toContainText("Unattended");
    await page.getByRole("button", { name: "Connected services" }).click();
    await expect(page.getByLabel("Internet mode")).toHaveValue("read_write");
    await expect(page.getByLabel(/Unattended/)).toBeChecked();
    // Free-text fields still need Save, and say so.
    await page.getByLabel("Requests per minute").fill("30");
    await expect(page.getByText("Unsaved changes").first()).toBeVisible();
  } finally {
    await app.close();
  }
});

test("code sandbox profile: bad image refused, valid profile saved per project", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-ui-"));
  const { app, page } = await launch(path.join(root, "data"));
  try {
    await demoProject(page);
    await page.getByRole("button", { name: "Connected services" }).click();
    const card = page.getByRole("region", { name: "Code sandbox" });
    await card.getByLabel("Image").fill("--privileged");
    await card.getByRole("button", { name: "Save sandbox" }).click();
    await expect(card.getByRole("alert")).toContainText("invalid image name");
    await card.getByLabel("Image").fill("nexus-sandbox:py312");
    await card.getByLabel("Time limit per run (s)").fill("900");
    await expect(card.getByText("Unsaved changes")).toBeVisible();
    await card.getByRole("button", { name: "Save sandbox" }).click();
    await expect(card.getByText("Saved", { exact: true })).toBeVisible();
    await expect(card.getByText("nexus-sandbox:py312", { exact: true })).toBeVisible();
  } finally {
    await app.close();
  }
});
