from __future__ import annotations

import os
import pwd
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from danmaku_backend.settings import STATE_DB_PATH


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_records (
    analysis_id TEXT PRIMARY KEY,
    bvid TEXT NOT NULL,
    site TEXT,
    host TEXT,
    csv_filename TEXT,
    txt_filename TEXT,
    subtitle_filename TEXT,
    subtitle_original_filename TEXT,
    count INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    data_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_artifact_records_bvid_created
    ON artifact_records (bvid, created_at);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    lease_token TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON jobs (status, created_at);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_id_id
    ON job_events (job_id, id);

CREATE TABLE IF NOT EXISTS request_rate_limits (
    client_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_request_rate_limits_key_kind_ts
    ON request_rate_limits (client_key, kind, ts);

CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    date TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    category TEXT NOT NULL,
    status INTEGER NOT NULL,
    duration_ms REAL,
    ip_hash TEXT,
    ip_segment TEXT,
    user_agent_family TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    site TEXT,
    host TEXT,
    referer_domain TEXT,
    bvid TEXT,
    analysis_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_analytics_events_date_category
    ON analytics_events (date, category);

CREATE INDEX IF NOT EXISTS idx_analytics_events_status_date
    ON analytics_events (status, date);

CREATE TABLE IF NOT EXISTS llm_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    date TEXT NOT NULL,
    job_id TEXT,
    kind TEXT,
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated INTEGER NOT NULL DEFAULT 0,
    bvid TEXT,
    analysis_id TEXT,
    site TEXT,
    host TEXT,
    raw_usage_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_events_date
    ON llm_usage_events (date);

CREATE INDEX IF NOT EXISTS idx_llm_usage_events_job_id
    ON llm_usage_events (job_id);
"""


def ensure_state_db(db_path: Path = STATE_DB_PATH) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o755)
    with connect_state_db(path) as conn:
        conn.executescript(SCHEMA_SQL)
        _ensure_artifact_columns(conn)
        _ensure_job_columns(conn)
        _ensure_analytics_event_columns(conn)
        conn.execute(
            "INSERT OR REPLACE INTO state_meta(key, value) VALUES (?, ?)",
            ("schema_version", "2"),
        )
        _ensure_llm_usage_columns(conn)
    _harden_state_files(path)


def _ensure_artifact_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"]: row for row in conn.execute("PRAGMA table_info(artifact_records)").fetchall()}
    columns = {
        "site": "TEXT",
        "host": "TEXT",
        "csv_filename": "TEXT",
        "txt_filename": "TEXT",
        "subtitle_filename": "TEXT",
        "subtitle_original_filename": "TEXT",
        "count": "INTEGER",
        "updated_at": "TEXT",
        "data_json": "TEXT",
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE artifact_records ADD COLUMN {name} {ddl}")

    refreshed = {row["name"]: row for row in conn.execute("PRAGMA table_info(artifact_records)").fetchall()}
    data_json = refreshed.get("data_json")
    if data_json and int(data_json["notnull"] or 0):
        conn.executescript(
            """
            CREATE TABLE artifact_records_new (
                analysis_id TEXT PRIMARY KEY,
                bvid TEXT NOT NULL,
                site TEXT,
                host TEXT,
                csv_filename TEXT,
                txt_filename TEXT,
                subtitle_filename TEXT,
                subtitle_original_filename TEXT,
                count INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                data_json TEXT
            );

            INSERT INTO artifact_records_new (
                analysis_id, bvid, site, host, csv_filename, txt_filename, subtitle_filename,
                subtitle_original_filename, count, created_at, updated_at, data_json
            )
            SELECT analysis_id, bvid, site, host, csv_filename, txt_filename, subtitle_filename,
                   subtitle_original_filename, count, created_at, updated_at, data_json
            FROM artifact_records;

            DROP TABLE artifact_records;
            ALTER TABLE artifact_records_new RENAME TO artifact_records;
            CREATE INDEX IF NOT EXISTS idx_artifact_records_bvid_created
                ON artifact_records (bvid, created_at);
            """
        )


def _ensure_job_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    columns = {
        "lease_token": "TEXT",
        "lease_owner": "TEXT",
        "lease_expires_at": "TEXT",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")


def _ensure_analytics_event_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(analytics_events)").fetchall()}
    columns = {
        "site": "TEXT",
        "host": "TEXT",
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE analytics_events ADD COLUMN {name} {ddl}")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_analytics_events_site_date_category
            ON analytics_events (site, date, category)
        """
    )


def _ensure_llm_usage_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(llm_usage_events)").fetchall()}
    columns = {
        "date": "TEXT",
        "job_id": "TEXT",
        "kind": "TEXT",
        "provider": "TEXT",
        "model": "TEXT",
        "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
        "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
        "total_tokens": "INTEGER NOT NULL DEFAULT 0",
        "estimated": "INTEGER NOT NULL DEFAULT 0",
        "bvid": "TEXT",
        "analysis_id": "TEXT",
        "site": "TEXT",
        "host": "TEXT",
        "raw_usage_json": "TEXT",
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE llm_usage_events ADD COLUMN {name} {ddl}")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_usage_events_date
            ON llm_usage_events (date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_usage_events_job_id
            ON llm_usage_events (job_id)
        """
    )


def _harden_state_files(path: Path) -> None:
    targets = [path, Path(f"{path}-wal"), Path(f"{path}-shm")]
    uid_gid: tuple[int, int] | None = None
    try:
        if os.geteuid() == 0:
            user = pwd.getpwnam("www")
            uid_gid = (user.pw_uid, user.pw_gid)
    except Exception:
        uid_gid = None

    for target in targets:
        if not target.exists():
            continue
        try:
            if uid_gid:
                os.chown(target, uid_gid[0], uid_gid[1])
            os.chmod(target, 0o660)
        except Exception:
            pass


@contextmanager
def connect_state_db(db_path: Path = STATE_DB_PATH) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
