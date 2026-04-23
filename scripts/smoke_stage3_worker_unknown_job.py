from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from danmaku_backend.services.database import connect_state_db, ensure_state_db
from danmaku_backend.settings import STATE_DB_PATH


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def insert_unknown_job(db_path: Path) -> str:
    job_id = "0123456789abcdef0123456789abcdef"
    timestamp = now_iso()
    payload = {"note": "stage3-smoke"}
    with connect_state_db(db_path) as conn:
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM job_events")
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, kind, status, payload_json, result_json, error_json,
                created_at, updated_at, started_at, finished_at, attempts
            ) VALUES (?, ?, 'queued', ?, NULL, NULL, ?, ?, NULL, NULL, 0)
            """,
            (
                job_id,
                "analysis-dispatcher",
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
    return job_id


def run_worker_once(repo_root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo_root / "worker.py"), "--once"],
        cwd=str(repo_root),
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def wait_for_job_state(db_path: Path, job_id: str, timeout_seconds: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_job: dict[str, Any] | None = None
    while time.time() < deadline:
        with connect_state_db(db_path) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row:
            last_job = {
                "job_id": row["job_id"],
                "kind": row["kind"],
                "status": row["status"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "attempts": int(row["attempts"] or 0),
            }
            if last_job["status"] in {"failed", "succeeded", "cancelled"}:
                return last_job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not reach a terminal state; last={last_job}")


def assert_app_has_no_bootstrap_logic(app_path: Path) -> None:
    source = app_path.read_text(encoding="utf-8")
    assert_true("analysis-dispatcher" not in source, "app.py should not contain analysis-dispatcher startup logic")
    assert_true("before_request" not in source, "app.py should not contain before_request startup logic")


def build_env(temp_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "BILI_DANMAKU_PROJECT_ROOT": str(temp_root),
            "BILI_DANMAKU_STATE_DIR": str(temp_root / ".state"),
            "BILI_DANMAKU_STATE_DB": str(temp_root / ".state" / STATE_DB_PATH.name),
            "BILI_DANMAKU_JOB_DIR": str(temp_root / ".jobs"),
            "BILI_DANMAKU_DOWNLOAD_DIR": str(temp_root / "downloads"),
            "BILI_DANMAKU_SUBTITLE_DIR": str(temp_root / "subtitles"),
            "BILI_DANMAKU_STATIC_DIR": str(temp_root / "static"),
            "BILI_DANMAKU_TEMPLATE_DIR": str(temp_root / "templates"),
            "BILI_DANMAKU_LOG_FILE": str(temp_root / "app.log"),
            "BILI_DANMAKU_WORKER_LOG_FILE": str(temp_root / "worker.log"),
            "BILI_DANMAKU_MODEL_CONFIG_FILE": str(temp_root / "model_config.json"),
            "BILI_DANMAKU_SECRET_FILE": str(temp_root / "secrets.json"),
        }
    )
    return env


def main() -> None:
    repo_root = PROJECT_ROOT
    app_path = repo_root / "app.py"
    assert_true(app_path.exists(), f"missing app.py at {app_path}")

    with tempfile.TemporaryDirectory(prefix="stage3-worker-smoke-") as tmp:
        temp_root = Path(tmp)
        env = build_env(temp_root)
        db_path = Path(env["BILI_DANMAKU_STATE_DB"])

        ensure_state_db(db_path)
        job_id = insert_unknown_job(db_path)
        assert_app_has_no_bootstrap_logic(app_path)

        proc = run_worker_once(repo_root, env)
        job = wait_for_job_state(db_path, job_id)

        assert_true(job["status"] == "failed", f"expected failed job, got {job['status']}")
        assert_true(job["attempts"] >= 1, f"expected at least one attempt, got {job['attempts']}")
        assert_true(job["started_at"] is not None, "job should have a started_at timestamp")
        assert_true(job["finished_at"] is not None, "job should have a finished_at timestamp")
        assert_true(job["error"] is not None, "job should have error payload after failure")
        assert_true(
            "未知任务类型" in str(job["error"].get("message", "")),
            f"unexpected failure message: {job['error']}",
        )

        with connect_state_db(db_path) as conn:
            event_rows = conn.execute(
                "SELECT type, message FROM job_events WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
            ).fetchall()
        assert_true(len(event_rows) >= 2, f"expected claim and failure events, got {len(event_rows)}")
        assert_true(
            any(row["type"] == "error" for row in event_rows),
            f"expected error event for failed job, got {[(row['type'], row['message']) for row in event_rows]}",
        )

        print(f"worker return code: {proc.returncode}")
        print(f"worker stdout:\n{proc.stdout.rstrip()}")
        print(f"worker stderr:\n{proc.stderr.rstrip()}")
        print("OK: worker.py --once claimed an unknown queued job and marked it failed; app.py has no analysis-dispatcher/before_request bootstrap logic.")


if __name__ == "__main__":
    main()
