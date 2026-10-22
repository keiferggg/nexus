"""Upgrading a populated 0001 database to 0002 rebuilds `tasks` without losing dependent rows."""
import sqlite3
from importlib import resources

from nexus.persistence.db import Database


def test_upgrade_preserves_rows(tmp_path):
    path = tmp_path / "old.db"
    sql = resources.files("nexus.persistence.migrations").joinpath("0001_initial.sql").read_text(encoding="utf-8")
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(sql + "\nCREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);"
                    "INSERT INTO schema_migrations VALUES ('0001', 'x');")
    c.execute("INSERT INTO projects (id, name, workspace_root, created_at) VALUES ('p', 'p', '.', 'x')")
    c.execute("INSERT INTO missions (id, project_id, title, status, execution_mode, created_at, updated_at)"
              " VALUES ('m', 'p', 't', 'running', 'offline_fake', 'x', 'x')")
    for t in ("a", "b"):
        c.execute("INSERT INTO tasks (id, mission_id, project_id, key, title, kind, description, status, mission_revision,"
                  " role, created_at, updated_at) VALUES (?, 'm', 'p', ?, 't', 'write', 'd', 'draft', 1, 'writer', 'x', 'x')",
                  (t, t))
    c.execute("INSERT INTO dependencies (task_id, depends_on_task_id) VALUES ('b', 'a')")
    c.execute("INSERT INTO task_attempts (id, task_id, attempt_no, fencing_token, epoch, mission_revision, status,"
              " workflow_name, started_at) VALUES ('at', 'a', 1, 1, 1, 1, 'completed', 'w', 'x')")
    c.commit()
    c.close()

    db = Database(path)
    assert [r["version"] for r in db.query("SELECT version FROM schema_migrations ORDER BY version")] [:2] == ["0001", "0002"] and len(db.query("SELECT 1 FROM schema_migrations")) >= 3
    assert db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM dependencies")["n"] == 1, "dependents must survive the rebuild"
    assert db.one("SELECT COUNT(*) AS n FROM task_attempts")["n"] == 1
    with db.tx() as cur:
        cur.execute("UPDATE tasks SET kind = 'act' WHERE id = 'a'")  # new kind admitted
    assert db.one("PRAGMA foreign_keys")[0] == 1
    db.close()
