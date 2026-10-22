// Builds the standalone NEXUS service (PyInstaller one-folder) that the installer bundles.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..", "..", "..");
const svc = path.join(repo, "services", "nexus");
const py = process.platform === "win32" ? path.join(repo, "services", ".venv", "Scripts", "pyinstaller.exe")
  : path.join(repo, "services", ".venv", "bin", "pyinstaller");
execFileSync(py, [path.join(svc, "packaging", "nexus_service.spec"), "--noconfirm", "--distpath", path.join(svc, "dist"),
  "--workpath", path.join(svc, "build"), "--log-level", "WARN"], { stdio: "inherit", cwd: svc });
