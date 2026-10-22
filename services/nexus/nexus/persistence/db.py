"""SQLite access: WAL mode, foreign keys, migrations, and a single serialized write path.

All writes go through ``Database.tx()`` which takes a process-wide lock and opens a
``BEGIN IMMEDIATE`` transaction, so concurrent workers (threads or tasks) cannot interleave
read-check-write sequences. Transactions are expected to be short; never await inside one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


class ConflictError(Exception):
    """Optimistic version check or invariant violation detected inside a transaction."""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._writer = self._connect()
        self._commit_hooks: list = []
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ---- reads -------------------------------------------------------------------------
    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "reader", None)
        if conn is None:
            conn = self._writer if self.path == ":memory:" else self._connect()
            self._local.reader = conn
            if conn is not self._writer:
                self._readers.append(conn)
        return conn

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self._reader().execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self._reader().execute(sql, params).fetchone()

    # ---- writes ------------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        with self._write_lock:
            cur = self._writer.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            else:
                cur.execute("COMMIT")
            finally:
                cur.close()
        for hook in list(self._commit_hooks):
            try:
                hook()
            except Exception:
                pass

    def add_commit_hook(self, hook) -> None:
        self._commit_hooks.append(hook)

    # ---- migrations --------------------------------------------------------------------
    def migrate(self) -> list[str]:
        applied: list[str] = []
        with self._write_lock:
            self._writer.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            done = {r[0] for r in self._writer.execute("SELECT version FROM schema_migrations")}
            files = sorted(
                (p for p in resources.files("nexus.persistence.migrations").iterdir() if p.name.endswith(".sql")),
                key=lambda p: p.name,
            )
            for f in files:
                version = f.name.split("_", 1)[0]
                if version in done:
                    continue
                sql = f.read_text(encoding="utf-8")
                # Table rebuilds need FKs off (a DROP TABLE with FKs on would cascade-delete
                # dependents). The pragma cannot change inside a transaction, so toggle around it
                # and verify integrity with foreign_key_check before committing.
                self._writer.execute("PRAGMA foreign_keys=OFF")
                try:
                    self._writer.executescript(f"BEGIN IMMEDIATE;\n{sql}\nINSERT INTO schema_migrations VALUES ('{version}', '{now_iso()}');")
                    bad = self._writer.execute("PRAGMA foreign_key_check").fetchall()
                    if bad:
                        self._writer.execute("ROLLBACK")
                        raise RuntimeError(f"migration {f.name} would break foreign keys: {[tuple(b) for b in bad[:5]]}")
                    self._writer.execute("COMMIT")
                finally:
                    self._writer.execute("PRAGMA foreign_keys=ON")
                applied.append(f.name)
        return applied

    def close(self) -> None:
        for conn in [*self._readers, self._writer]:  # readers too, or Windows keeps the file locked
            try:
                conn.close()
            except Exception:
                pass
        self._readers.clear()
        self._local = threading.local()

    def backup_to(self, dest: Path) -> None:
        with self._write_lock:
            target = sqlite3.connect(str(dest))
            try:
                self._writer.backup(target)
            finally:
                target.close()


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)
