from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from danmaku_backend.services.database import connect_state_db, ensure_state_db
from danmaku_backend.settings import CLEANUP_INTERVAL_SECONDS, JOB_DIR, JOB_RETENTION_SECONDS, STATE_DB_PATH


JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class JobStore:
    def __init__(self, job_dir: Path = JOB_DIR, db_path: Path = STATE_DB_PATH):
        self.job_dir = Path(job_dir)
        self.db_path = Path(db_path)

    def ensure(self) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.job_dir, 0o755)
        ensure_state_db(self.db_path)
        self._migrate_legacy_jobs_once()

    def create(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure()
        job_id = uuid.uuid4().hex
        now = self._now()
        job = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "payload": payload,
            "result": None,
            "error": None,
        }
        self._upsert_job(job)
        self.add_event(job_id, "info", "任务已创建，等待执行")
        self.cleanup_if_due()
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        self.validate_job_id(job_id)
        self.ensure()
        with connect_state_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def update(self, job_id: str, **updates: Any) -> dict[str, Any]:
        job = self.get(job_id)
        if not job:
            raise ValueError("job not found")
        job.update(updates)
        job["updated_at"] = self._now()
        self._upsert_job(job)
        return job

    def claim_next(
        self,
        max_running: int,
        *,
        owner_id: str = "",
        lease_seconds: int = 1800,
        kind_limits: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        self.ensure()
        max_running = max(1, int(max_running or 1))
        now = self._now()
        lease_seconds = max(60, int(lease_seconds or 60))
        lease_expires_at = (
            datetime.now().astimezone() + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="seconds")
        lease_token = uuid.uuid4().hex
        with connect_state_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                stale_rows = conn.execute(
                    """
                    SELECT job_id
                    FROM jobs
                    WHERE status = 'running'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < ?
                    """,
                    (now,),
                ).fetchall()
                for stale in stale_rows:
                    error = {"message": "任务执行超时，已自动结束"}
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed', error_json = ?, finished_at = ?, updated_at = ?,
                            lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
                        WHERE job_id = ?
                        """,
                        (
                            json.dumps(error, ensure_ascii=False, sort_keys=True),
                            now,
                            now,
                            stale["job_id"],
                        ),
                    )
                    self._insert_event(conn, stale["job_id"], "error", error["message"], now)

                running_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM jobs WHERE status = 'running'"
                ).fetchone()["count"]
                if running_count >= max_running:
                    conn.execute("ROLLBACK")
                    return None
                kind_limits = {
                    str(kind): max(0, int(limit))
                    for kind, limit in (kind_limits or {}).items()
                }
                running_by_kind_rows = conn.execute(
                    """
                    SELECT kind, COUNT(*) AS count
                    FROM jobs
                    WHERE status = 'running'
                    GROUP BY kind
                    """
                ).fetchall()
                running_by_kind = {
                    str(item["kind"]): int(item["count"] or 0) for item in running_by_kind_rows
                }
                queued_rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC"
                ).fetchall()
                row = None
                for candidate in queued_rows:
                    candidate_kind = str(candidate["kind"] or "")
                    limit = kind_limits.get(candidate_kind)
                    if limit is not None and running_by_kind.get(candidate_kind, 0) >= limit:
                        continue
                    row = candidate
                    break
                if not row:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'running',
                        started_at = COALESCE(started_at, ?),
                        updated_at = ?,
                        lease_token = ?,
                        lease_owner = ?,
                        lease_expires_at = ?,
                        attempts = attempts + 1
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (now, now, lease_token, owner_id, lease_expires_at, row["job_id"]),
                )
                self._insert_event(conn, row["job_id"], "info", "任务开始执行", now)
                claimed = conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                ).fetchone()
                conn.execute("COMMIT")
                return self._row_to_job(claimed)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def queued_count(self) -> int:
        self.ensure()
        with connect_state_db(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM jobs WHERE status = 'queued'").fetchone()
        return int(row["count"])

    def running_count(self) -> int:
        self.ensure()
        with connect_state_db(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM jobs WHERE status = 'running'").fetchone()
        return int(row["count"])

    def recover_stale_running(self, timeout_seconds: int) -> int:
        self.ensure()
        timeout_seconds = max(60, int(timeout_seconds or 60))
        cutoff = datetime.now().astimezone() - timedelta(seconds=timeout_seconds)
        cutoff_text = cutoff.isoformat(timespec="seconds")
        now = self._now()
        with connect_state_db(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT job_id
                FROM jobs
                WHERE status = 'running'
                  AND (
                    (lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                    OR (lease_expires_at IS NULL AND COALESCE(updated_at, started_at, created_at) < ?)
                  )
                """,
                (now, cutoff_text),
            ).fetchall()
            job_ids = [row["job_id"] for row in rows]
            for job_id in job_ids:
                error = {"message": "任务执行超时，已自动结束"}
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', error_json = ?, finished_at = ?, updated_at = ?,
                        lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
                    WHERE job_id = ?
                    """,
                    (json.dumps(error, ensure_ascii=False, sort_keys=True), now, now, job_id),
                )
                self._insert_event(conn, job_id, "error", error["message"], now)
        return len(job_ids)

    def recover_orphaned_running(self, hostname: str) -> int:
        self.ensure()
        now = self._now()
        reclaimed = 0
        with connect_state_db(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT job_id, lease_owner
                FROM jobs
                WHERE status = 'running'
                  AND lease_owner IS NOT NULL
                  AND lease_owner != ''
                """
            ).fetchall()
            for row in rows:
                owner = str(row["lease_owner"] or "")
                host, separator, pid_text = owner.rpartition(":")
                if not separator or host != hostname:
                    continue
                try:
                    pid = int(pid_text)
                except ValueError:
                    pid = -1
                if self._pid_is_alive(pid):
                    continue
                reclaimed += 1
                error = {"message": "任务在服务重启时中断，已自动结束"}
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', error_json = ?, finished_at = ?, updated_at = ?,
                        lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
                    WHERE job_id = ?
                    """,
                    (json.dumps(error, ensure_ascii=False, sort_keys=True), now, now, row["job_id"]),
                )
                self._insert_event(conn, row["job_id"], "error", error["message"], now)
        return reclaimed

    def mark_running(self, job_id: str) -> None:
        self.update(job_id, status="running", started_at=self._now())
        self.add_event(job_id, "info", "任务开始执行")

    def renew_lease(self, job_id: str, lease_token: str, lease_seconds: int) -> bool:
        self.validate_job_id(job_id)
        lease_seconds = max(60, int(lease_seconds or 60))
        lease_expires_at = (
            datetime.now().astimezone() + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="seconds")
        with connect_state_db(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_token = ?
                """,
                (lease_expires_at, self._now(), job_id, lease_token),
            )
        return cursor.rowcount == 1

    def mark_succeeded(
        self,
        job_id: str,
        result: dict[str, Any],
        lease_token: str | None = None,
    ) -> None:
        if lease_token:
            self._finish_with_lease(
                job_id,
                "succeeded",
                result,
                None,
                lease_token,
                "success",
                "任务执行完成",
            )
            return
        self._finish_without_lease(job_id, "succeeded", result, None, "success", "任务执行完成")

    def mark_failed(self, job_id: str, message: str, lease_token: str | None = None) -> None:
        error = {"message": message}
        if lease_token:
            self._finish_with_lease(job_id, "failed", None, error, lease_token, "error", message)
            return
        self._finish_without_lease(job_id, "failed", None, error, "error", message)

    def add_event(
        self,
        job_id: str,
        kind: str,
        message: str,
        lease_token: str | None = None,
    ) -> bool:
        self.validate_job_id(job_id)
        self.ensure()
        with connect_state_db(self.db_path) as conn:
            if lease_token:
                cursor = conn.execute(
                    """
                    INSERT INTO job_events(job_id, type, message, ts)
                    SELECT ?, ?, ?, ?
                    WHERE EXISTS (
                        SELECT 1
                        FROM jobs
                        WHERE job_id = ? AND status = 'running' AND lease_token = ?
                    )
                    """,
                    (job_id, kind, str(message), self._now(), job_id, lease_token),
                )
                return cursor.rowcount == 1
            self._insert_event(conn, job_id, kind, str(message), self._now())
            return True

    def read_events(self, job_id: str, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        self.validate_job_id(job_id)
        self.ensure()
        try:
            last_id = max(0, int(offset or 0))
        except (TypeError, ValueError):
            last_id = 0
        with connect_state_db(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, type, message, ts
                FROM job_events
                WHERE job_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (job_id, last_id),
            ).fetchall()
        events = [dict(row) for row in rows]
        next_offset = int(events[-1]["id"]) if events else last_id
        return events, next_offset

    @staticmethod
    def validate_job_id(job_id: str) -> str:
        if not JOB_ID_RE.fullmatch(job_id or ""):
            raise ValueError("invalid job_id")
        return job_id

    def cleanup_if_due(
        self,
        retention_seconds: int = JOB_RETENTION_SECONDS,
        interval_seconds: int = CLEANUP_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        self.ensure()
        now = datetime.now().astimezone()
        last_run = self._meta_get("jobs_last_cleanup")
        if last_run:
            try:
                parsed = datetime.fromisoformat(last_run)
                if (now - parsed).total_seconds() < interval_seconds:
                    return {"ran": False, "deleted_jobs": 0}
            except Exception:
                pass
        result = self.cleanup(retention_seconds)
        self._meta_set("jobs_last_cleanup", now.isoformat(timespec="seconds"))
        return {"ran": True, **result}

    def cleanup(self, retention_seconds: int = JOB_RETENTION_SECONDS) -> dict[str, Any]:
        self.ensure()
        cutoff = datetime.now().astimezone() - timedelta(seconds=retention_seconds)
        cutoff_text = cutoff.isoformat(timespec="seconds")
        with connect_state_db(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT job_id
                FROM jobs
                WHERE status IN ('succeeded', 'failed', 'cancelled')
                  AND COALESCE(finished_at, updated_at) < ?
                """,
                (cutoff_text,),
            ).fetchall()
            job_ids = [row["job_id"] for row in rows]
            for job_id in job_ids:
                conn.execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
                conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        self._cleanup_legacy_job_dirs()
        return {"deleted_jobs": len(job_ids)}

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _upsert_job(self, job: dict[str, Any]) -> None:
        self.validate_job_id(job["job_id"])
        with connect_state_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, status, payload_json, result_json, error_json,
                    created_at, updated_at, started_at, finished_at,
                    lease_token, lease_owner, lease_expires_at, attempts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    kind = excluded.kind,
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    result_json = excluded.result_json,
                    error_json = excluded.error_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    lease_token = excluded.lease_token,
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at,
                    attempts = excluded.attempts
                """,
                (
                    job["job_id"],
                    job.get("kind") or "",
                    job.get("status") or "queued",
                    json.dumps(job.get("payload") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(job.get("result"), ensure_ascii=False, sort_keys=True)
                    if job.get("result") is not None
                    else None,
                    json.dumps(job.get("error"), ensure_ascii=False, sort_keys=True)
                    if job.get("error") is not None
                    else None,
                    job.get("created_at") or self._now(),
                    job.get("updated_at") or self._now(),
                    job.get("started_at"),
                    job.get("finished_at"),
                    job.get("lease_token"),
                    job.get("lease_owner"),
                    job.get("lease_expires_at"),
                    int(job.get("attempts") or 0),
                ),
            )

    def _row_to_job(self, row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"] or "{}")
        result = json.loads(row["result_json"]) if row["result_json"] else None
        error = json.loads(row["error_json"]) if row["error_json"] else None
        job = {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "payload": payload,
            "result": result,
            "error": error,
        }
        if row["started_at"]:
            job["started_at"] = row["started_at"]
        if row["finished_at"]:
            job["finished_at"] = row["finished_at"]
        if row["lease_token"]:
            job["lease_token"] = row["lease_token"]
        if row["lease_owner"]:
            job["lease_owner"] = row["lease_owner"]
        if row["lease_expires_at"]:
            job["lease_expires_at"] = row["lease_expires_at"]
        job["attempts"] = int(row["attempts"] or 0)
        return job

    def _finish_with_lease(
        self,
        job_id: str,
        status: str,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
        lease_token: str,
        event_kind: str,
        event_message: str,
    ) -> bool:
        self.validate_job_id(job_id)
        now = self._now()
        with connect_state_db(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?, result_json = ?, error_json = ?, finished_at = ?, updated_at = ?,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
                WHERE job_id = ? AND status = 'running' AND lease_token = ?
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False, sort_keys=True)
                    if result is not None
                    else None,
                    json.dumps(error, ensure_ascii=False, sort_keys=True)
                    if error is not None
                    else None,
                    now,
                    now,
                    job_id,
                    lease_token,
                ),
            )
            if cursor.rowcount == 1:
                self._insert_event(conn, job_id, event_kind, event_message, now)
        return cursor.rowcount == 1

    def _finish_without_lease(
        self,
        job_id: str,
        status: str,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
        event_kind: str,
        event_message: str,
    ) -> None:
        self.validate_job_id(job_id)
        now = self._now()
        with connect_state_db(self.db_path) as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, result_json = ?, error_json = ?, finished_at = ?, updated_at = ?,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False, sort_keys=True)
                    if result is not None
                    else None,
                    json.dumps(error, ensure_ascii=False, sort_keys=True)
                    if error is not None
                    else None,
                    now,
                    now,
                    job_id,
                ),
            )
            self._insert_event(conn, job_id, event_kind, event_message, now)

    @staticmethod
    def _insert_event(conn, job_id: str, kind: str, message: str, ts: str) -> None:
        conn.execute(
            "INSERT INTO job_events(job_id, type, message, ts) VALUES (?, ?, ?, ?)",
            (job_id, kind, message, ts),
        )

    def _migrate_legacy_jobs_once(self) -> None:
        if self._meta_get("legacy_jobs_migrated") == "1":
            return
        if self.job_dir.exists():
            for path in self.job_dir.iterdir():
                if not path.is_dir() or not JOB_ID_RE.fullmatch(path.name):
                    continue
                status_path = path / "status.json"
                if not status_path.exists():
                    continue
                try:
                    job = json.loads(status_path.read_text(encoding="utf-8"))
                    self._upsert_job(job)
                except Exception:
                    continue
                events_path = path / "events.jsonl"
                if events_path.exists():
                    for line in events_path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        with connect_state_db(self.db_path) as conn:
                            self._insert_event(
                                conn,
                                path.name,
                                str(event.get("type") or "info"),
                                str(event.get("message") or ""),
                                str(event.get("ts") or self._now()),
                            )
        self._meta_set("legacy_jobs_migrated", "1")

    def _cleanup_legacy_job_dirs(self) -> None:
        if not self.job_dir.exists():
            return
        cutoff = datetime.now().astimezone() - timedelta(seconds=JOB_RETENTION_SECONDS)
        for path in self.job_dir.iterdir():
            if not path.is_dir() or not JOB_ID_RE.fullmatch(path.name):
                continue
            status_path = path / "status.json"
            if not status_path.exists():
                continue
            try:
                job = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if job.get("status") not in TERMINAL_STATUSES:
                continue
            finished_at = self._parse_time(job.get("finished_at") or job.get("updated_at"))
            if finished_at and finished_at < cutoff:
                self._remove_tree(path)

    def _meta_get(self, key: str) -> str | None:
        with connect_state_db(self.db_path) as conn:
            row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def _meta_set(self, key: str, value: str) -> None:
        with connect_state_db(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO state_meta(key, value) VALUES (?, ?)",
                (key, value),
            )

    @staticmethod
    def _parse_time(raw: Any) -> datetime | None:
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.astimezone()

    @staticmethod
    def _remove_tree(path: Path) -> None:
        for child in path.iterdir():
            if child.is_dir():
                JobStore._remove_tree(child)
            else:
                child.unlink(missing_ok=True)
        path.rmdir()


default_job_store = JobStore()
