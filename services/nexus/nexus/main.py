"""NEXUS service entry point.

    nexus-service serve   --data-dir DIR [--port 0]      (token from env NEXUS_AUTH_TOKEN)
    nexus-service backup  --data-dir DIR --to FILE.zip   (service must be stopped)
    nexus-service restore --data-dir NEWDIR --from FILE.zip
    nexus-service verify-backup --from FILE.zip

On successful startup the service prints one line to stdout:
    NEXUS_READY {"port": <int>, "pid": <int>, "version": "..."}
The desktop app reads that line to discover the loopback port. The auth token is passed only
through the environment (never argv, never logged).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from pathlib import Path

from . import __version__

log = logging.getLogger("nexus")


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"another NEXUS service is already using {self.path.parent}") from None


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "NEXUS" / "data"


async def serve(data_dir: Path, port: int) -> None:
    import uvicorn

    from .api.app import create_app
    from .appctx import build_context

    token = os.environ.get("NEXUS_AUTH_TOKEN")
    if not token or len(token) < 24:
        raise SystemExit("NEXUS_AUTH_TOKEN (>= 24 chars) must be provided in the environment")
    os.environ.pop("NEXUS_AUTH_TOKEN", None)  # do not leak into child processes (git, sandbox)
    lock = InstanceLock(data_dir / "service.lock")
    lock.acquire()
    ctx = build_context(data_dir)
    app = create_app(ctx, token=token, allowed_origins=[o for o in os.environ.get("NEXUS_ALLOWED_ORIGINS", "").split(",")
                                                        if o])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) if os.name != "nt" else None
    sock.bind(("127.0.0.1", port))
    actual = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    server = uvicorn.Server(config)

    async def announce():
        while not server.started:
            await asyncio.sleep(0.05)
        print("NEXUS_READY " + json.dumps({"port": actual, "pid": os.getpid(), "version": __version__}), flush=True)

    asyncio.get_running_loop().create_task(announce())
    await server.serve(sockets=[sock])
    ctx.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nexus-service")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--data-dir", type=Path, default=default_data_dir())
    s.add_argument("--port", type=int, default=0)
    b = sub.add_parser("backup")
    b.add_argument("--data-dir", type=Path, default=default_data_dir())
    b.add_argument("--to", type=Path, required=True)
    r = sub.add_parser("restore")
    r.add_argument("--data-dir", type=Path, required=True)
    r.add_argument("--from", dest="src", type=Path, required=True)
    v = sub.add_parser("verify-backup")
    v.add_argument("--from", dest="src", type=Path, required=True)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "serve":
        asyncio.run(serve(args.data_dir, args.port))
        return 0
    if args.cmd == "backup":
        from .appctx import build_context
        from .backup import create_backup

        lock = InstanceLock(args.data_dir / "service.lock")
        lock.acquire()
        ctx = build_context(args.data_dir)
        print(json.dumps(create_backup(ctx, args.to)))
        ctx.close()
        return 0
    if args.cmd == "restore":
        from .backup import restore_backup

        print(json.dumps(restore_backup(args.src, args.data_dir), default=str))
        return 0
    if args.cmd == "verify-backup":
        from .backup import verify_backup

        res = verify_backup(args.src)
        print(json.dumps(res, default=str))
        return 0 if res["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
