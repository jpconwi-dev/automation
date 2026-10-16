import json
import sqlite3
import threading
import time
from contextlib import closing

from .config import settings

_lock = threading.RLock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL);
"""


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(settings.storage_dir / "app.db", timeout=30)


def init_db() -> None:
    for d in (settings.projects_dir, settings.tmp_dir, settings.cache_dir):
        d.mkdir(parents=True, exist_ok=True)
    with _lock, closing(_conn()) as c:
        c.executescript(SCHEMA)
        c.commit()


def _get(table: str, id_: str) -> dict | None:
    with closing(_conn()) as c:
        row = c.execute(f"SELECT data FROM {table} WHERE id=?", (id_,)).fetchone()
    return json.loads(row[0]) if row else None


def save_project(project: dict) -> None:
    with _lock, closing(_conn()) as c:
        c.execute("INSERT INTO projects(id,data,created_at) VALUES(?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                  (project["id"], json.dumps(project), time.time()))
        c.commit()


def save_job(job: dict) -> None:
    with _lock, closing(_conn()) as c:
        c.execute("INSERT INTO jobs(id,project_id,data,created_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                  (job["id"], job["project_id"], json.dumps(job), time.time()))
        c.commit()


def get_project(pid: str) -> dict | None:
    return _get("projects", pid)


def get_job(jid: str) -> dict | None:
    return _get("jobs", jid)


def update_job(jid: str, **fields) -> dict:
    with _lock:
        job = get_job(jid) or {}
        job.update(fields)
        save_job(job)
        return job


def active_jobs() -> list[dict]:
    with closing(_conn()) as c:
        rows = c.execute("SELECT data FROM jobs").fetchall()
    return [j for j in (json.loads(r[0]) for r in rows) if j["status"] in ("queued", "running")]


def fail_stale_jobs() -> None:
    for j in active_jobs():
        j.update(status="failed", error="The server restarted while this job was running. Start it again.")
        save_job(j)
