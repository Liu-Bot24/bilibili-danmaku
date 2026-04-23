from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def prepare_env(root: Path) -> None:
    os.environ["BILI_DANMAKU_PROJECT_ROOT"] = str(root)
    os.environ["BILI_DANMAKU_DOWNLOAD_DIR"] = str(root / "downloads")
    os.environ["BILI_DANMAKU_SUBTITLE_DIR"] = str(root / "subtitles")
    os.environ["BILI_DANMAKU_STATE_DIR"] = str(root / ".state")
    os.environ["BILI_DANMAKU_STATE_DB"] = str(root / ".state" / "state.sqlite3")
    os.environ["BILI_DANMAKU_JOB_DIR"] = str(root / ".jobs")
    os.environ["BILI_DANMAKU_LOG_FILE"] = str(root / "app.log")
    os.environ["BILI_DANMAKU_SECRET_FILE"] = str(root / "secrets.json")
    os.environ["BILI_DANMAKU_MODEL_CONFIG_FILE"] = str(root / "model_config.json")
    (root / "secrets.json").write_text(
        json.dumps({"BILI_DANMAKU_APP_TOKEN": "operator-secret"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "model_config.json").write_text(
        json.dumps(
            {
                "active_provider": "missing-provider",
                "fallback_order": ["openai-compatible-primary"],
                "analysis": {"max_concurrent_jobs": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def assert_old_artifact_schema_migrates(root: Path) -> None:
    from danmaku_backend.services.artifacts import ArtifactStore
    from danmaku_backend.services.database import ensure_state_db

    db_path = root / ".state" / "legacy-artifacts.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE artifact_records (
                analysis_id TEXT PRIMARY KEY,
                bvid TEXT NOT NULL,
                csv_filename TEXT,
                txt_filename TEXT,
                subtitle_filename TEXT,
                subtitle_original_filename TEXT,
                count INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                data_json TEXT NOT NULL
            );
            """
        )

    ensure_state_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("PRAGMA table_info(artifact_records)").fetchall()[-1]
        assert_true(row[1] == "data_json" and row[3] == 0, "data_json should be nullable after migration")

    store = ArtifactStore(root / "downloads", root / "subtitles", db_path)
    record = store.save_danmaku_files(
        "BV1xx411c7mD",
        [
            {
                "text": "hello",
                "appear_time": 1,
                "send_time": "2026-04-21 10:00:00",
                "type": "scroll",
                "size": 25,
                "color": "#ffffff",
                "sender": "user",
            }
        ],
    )
    assert_true(store.latest_danmaku_txt("BV1xx411c7mD", record.analysis_id) is not None, "saved TXT should be indexed")

    orphan = root / "downloads" / "danmaku_BV1xx411c7mD_20000101_000000_orphan.txt"
    orphan.write_text("orphan", encoding="utf-8")
    old_time = datetime.now().timestamp() - 10_000
    os.utime(orphan, (old_time, old_time))
    cleanup = store.cleanup(retention_seconds=1)
    assert_true(cleanup["deleted_files"] >= 1 and not orphan.exists(), "old orphan artifact should be cleaned")


def assert_job_lease_rules(root: Path) -> None:
    from danmaku_backend.services.database import connect_state_db
    from danmaku_backend.services.jobs import JobStore

    store = JobStore(root / ".jobs", root / ".state" / "jobs.sqlite3")
    queued = store.create("content_analysis", {"bvid": "BV1xx411c7mD", "analysis_id": None})
    job = store.claim_next(1, owner_id="smoke", lease_seconds=3600)
    assert_true(job and job["job_id"] == queued["job_id"], "queued job should be claimed")

    future_lease = (datetime.now().astimezone() + timedelta(hours=1)).isoformat(timespec="seconds")
    old_start = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
    with connect_state_db(store.db_path) as conn:
        conn.execute(
            "UPDATE jobs SET started_at = ?, updated_at = ?, lease_expires_at = ? WHERE job_id = ?",
            (old_start, datetime.now().astimezone().isoformat(timespec="seconds"), future_lease, job["job_id"]),
        )
    assert_true(store.recover_stale_running(60) == 0, "active lease should not be recovered as stale")
    assert_true(not store.add_event(job["job_id"], "info", "wrong lease", lease_token="wrong"), "wrong lease must not write events")
    assert_true(
        store.add_event(job["job_id"], "info", "right lease", lease_token=job["lease_token"]),
        "current lease should write events",
    )

    expired_lease = (datetime.now().astimezone() - timedelta(seconds=5)).isoformat(timespec="seconds")
    with connect_state_db(store.db_path) as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE job_id = ?", (expired_lease, job["job_id"]))
    assert_true(store.recover_stale_running(60) == 1, "expired lease should be recovered")
    assert_true(not store.add_event(job["job_id"], "info", "after recovery", lease_token=job["lease_token"]), "expired lease must not write events")


def assert_web_auth_contract() -> None:
    from app import app
    from danmaku_backend.services.database import connect_state_db
    from danmaku_backend.services.jobs import default_job_store
    from danmaku_backend.settings import STATE_DB_PATH

    anonymous = app.test_client()
    protected = anonymous.post("/api/v2/maintenance/cleanup")
    assert_true(protected.status_code == 403, "maintenance cleanup should require operator token")

    page = anonymous.get("/")
    html = page.get_data(as_text=True)
    assert_true("operator-secret" not in html, "operator token must not be rendered in public HTML")
    assert_true("X-Bili-Danmaku-CSRF" in html, "frontend should use CSRF header")

    authorized = anonymous.post(
        "/api/v2/maintenance/cleanup",
        headers={"X-Bili-Danmaku-Token": "operator-secret"},
    )
    assert_true(authorized.status_code == 200, "operator token should allow maintenance cleanup")

    queued = default_job_store.create("content_analysis", {"bvid": "BV1xx411c7mD", "analysis_id": None})
    claimed = default_job_store.claim_next(1, owner_id="web-smoke", lease_seconds=3600)
    assert_true(claimed and claimed["job_id"] == queued["job_id"], "web smoke job should be claimed")
    job_response = anonymous.get(f"/api/v2/jobs/{claimed['job_id']}")
    assert_true(job_response.status_code == 200, "same-site client should read job status")
    job_data = job_response.get_json()["data"]
    assert_true("lease_token" not in job_data and "payload" not in job_data, "public job status must hide internals")
    with connect_state_db(STATE_DB_PATH) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert_true("request_rate_limits" in tables, "rate limits should be backed by SQLite")


def assert_model_config_fallback() -> None:
    from config import get_model_runtime_config

    runtime = get_model_runtime_config()
    assert_true(runtime["active_provider"] == "openai-compatible-primary", "invalid active provider should fall back")
    assert_true(runtime["fallback_order"][0] == "openai-compatible-primary", "fallback order should start with valid fallback")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="backend-hardening-smoke-") as tmp:
        root = Path(tmp)
        prepare_env(root)
        assert_model_config_fallback()
        assert_old_artifact_schema_migrates(root)
        assert_job_lease_rules(root)
        assert_web_auth_contract()
    print("OK: backend auth, lease recovery, artifact schema migration, orphan cleanup, and model fallback verified.")


if __name__ == "__main__":
    main()
