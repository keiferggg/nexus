// Builds the Windows installer and publishes it to the update feed.
//  * every build gets the next version 0.2.<n> (installer/BUILD_NUMBER), so installed copies see it as newer;
//  * output goes to a fresh timestamped folder (a lock on an earlier build can never block a rebuild);
//  * installer/latest.json is rewritten last (version, relative installer path, SHA-256), so an app
//    checking mid-build never sees a half-written release.
// The feed location is baked into the app (package.json "nexusUpdateFeed"); installed copies check it
// at startup and every 30 minutes.
import { execSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const app = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const feedDir = path.join(app, "installer");
fs.mkdirSync(feedDir, { recursive: true });
const counter = path.join(feedDir, "BUILD_NUMBER");
const n = (fs.existsSync(counter) ? parseInt(fs.readFileSync(counter, "utf8"), 10) || 0 : 0) + 1;
const version = `0.2.${n}`;
const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "").replace("T", "-");
const out = path.join("installer", stamp);
const feed = feedDir.replace(/\\/g, "/");
execSync(`npx electron-builder --win --config electron-builder.yml -c.directories.output=${out}` +
  ` -c.extraMetadata.version=${version} -c.extraMetadata.nexusUpdateFeed="${feed}"`, { cwd: app, stdio: "inherit" });
const exe = `NEXUS-Setup-${version}.exe`;
const exePath = path.join(app, out, exe);
const sha256 = createHash("sha256").update(fs.readFileSync(exePath)).digest("hex");
fs.writeFileSync(counter, String(n));
fs.writeFileSync(path.join(feedDir, "LATEST.txt"), stamp + "\n");
const manifest = { version, installer: `${stamp}/${exe}`, sha256, released_at: new Date().toISOString() };
fs.writeFileSync(path.join(feedDir, "latest.json.tmp"), JSON.stringify(manifest, null, 2));
fs.renameSync(path.join(feedDir, "latest.json.tmp"), path.join(feedDir, "latest.json"));
console.log(`published ${version}: ${exePath}`);
