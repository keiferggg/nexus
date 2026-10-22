# PyInstaller spec: one-folder build of the NEXUS service (no Python needed on the target machine).
# Build:  services/.venv/Scripts/pyinstaller services/nexus/packaging/nexus_service.spec --noconfirm
#         --distpath services/nexus/dist --workpath services/nexus/build
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", "..", ".."))
datas, binaries, hiddenimports = [], [], []
for pkg in ("agent_framework", "agent_framework_orchestrations", "mcp_types", "uvicorn", "keyring", "tzdata", "pypdf"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# mcp.cli exits at import time without its optional CLI extras; NEXUS only needs the client/server.
hiddenimports += collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
datas += collect_data_files("mcp")
datas += collect_data_files("nexus", includes=["persistence/migrations/*.sql"])
hiddenimports += collect_submodules("nexus") + ["keyring.backends.Windows", "win32ctypes.core"]
datas += [(os.path.join(ROOT, "fixtures"), "fixtures")]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[os.path.join(ROOT, "services", "nexus")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="nexus-service", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="nexus-service")
