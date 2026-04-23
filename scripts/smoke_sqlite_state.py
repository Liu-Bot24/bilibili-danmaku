from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_records (
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
    finished_at TEXT
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
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_job_id(seed: int) -> str:
    return f"{seed:032x}"[-32:]


def make_analysis_id(seed: int) -> str:
    return f"{seed:032x}"[-32:]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def connect_state_db(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def ensure_state_db(db_path: Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o755)
    with connect_state_db(path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT OR REPLACE INTO state_meta(key, value) VALUES (?, ?)", ("schema_version", "1"))


def build_legacy_state(root: Path) -> dict[str, Any]:
    download_dir = root / "downloads"
    subtitle_dir = root / "subtitles"
    artifact_index = root / ".artifact-index"
    job_dir = root / ".jobs"

    artifact_index.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    job_dir.mkdir(parents=True, exist_ok=True)

    analysis_id = make_analysis_id(1)
    bvid = "BV1xx411c7mD"
    csv_filename = "danmaku_BV1xx411c7mD_20260421_120000.csv"
    txt_filename = "danmaku_BV1xx411c7mD_20260421_120000.txt"

    (download_dir / csv_filename).write_text("弹幕内容,出现时间(秒)\nhello,1\n", encoding="utf-8")
    (download_dir / txt_filename).write_text("[00:01] hello\n", encoding="utf-8")
    (subtitle_dir / "subtitle_BV1xx411c7mD_20260421_120000.txt").write_text("subtitle", encoding="utf-8")

    manifest_path = artifact_index / "manifest.jsonl"
    manifest_records = [
        {
            "analysis_id": analysis_id,
            "bvid": bvid,
            "csv_filename": csv_filename,
            "txt_filename": txt_filename,
            "count": 1,
            "created_at": "2026-04-21T10:00:00+08:00",
            "updated_at": "2026-04-21T10:00:00+08:00",
        },
        {
            "analysis_id": analysis_id,
            "bvid": bvid,
            "csv_filename": csv_filename,
            "txt_filename": txt_filename,
            "subtitle_filename": "subtitle_BV1xx411c7mD_20260421_120000.txt",
            "subtitle_original_filename": "input.txt",
            "count": 1,
            "created_at": "2026-04-21T10:00:00+08:00",
            "updated_at": "2026-04-21T10:05:00+08:00",
        },
    ]
    with manifest_path.open("w", encoding="utf-8") as file:
        for record in manifest_records:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    for seed, status, kind in (
        (1, "succeeded", "download"),
        (2, "queued", "analysis"),
    ):
        job_id = make_job_id(seed)
        job_path = job_dir / job_id
        job_path.mkdir(parents=True, exist_ok=True)
        status_payload = {
            "job_id": job_id,
            "kind": kind,
            "status": status,
            "created_at": "2026-04-21T10:00:00+08:00",
            "updated_at": "2026-04-21T10:00:00+08:00",
            "payload": {"seed": seed},
            "result": {"ok": True} if status == "succeeded" else None,
            "error": None,
        }
        write_json(job_path / "status.json", status_payload)
        events = [
            {"job_id": job_id, "type": "info", "message": "任务已创建，等待执行", "ts": "2026-04-21T10:00:00+08:00"},
            {"job_id": job_id, "type": "info", "message": "处理中", "ts": "2026-04-21T10:00:01+08:00"},
        ]
        if status == "succeeded":
            events.append({"job_id": job_id, "type": "success", "message": "任务执行完成", "ts": "2026-04-21T10:00:02+08:00"})
        with (job_path / "events.jsonl").open("w", encoding="utf-8") as file:
            for event in events:
                file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    return {
        "analysis_id": analysis_id,
        "bvid": bvid,
        "job_ids": [make_job_id(1), make_job_id(2)],
        "root": root,
    }


def assert_artifact_store_roundtrip(root: Path) -> None:
    artifact_root = root / "artifact-smoke"
    download_dir = artifact_root / "downloads"
    subtitle_dir = artifact_root / "subtitles"
    index_dir = artifact_root / ".artifact-index"
    download_dir.mkdir(parents=True, exist_ok=True)
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)

    payload = [
        {
            "text": "hello",
            "appear_time": 1,
            "send_time": "2026-04-21 10:00:00",
            "type": "scroll",
            "size": 25,
            "color": "#ffffff",
            "sender": "user-1",
        }
    ]
    record = append_artifact_record(
        artifact_root,
        "BV1xx411c7mD",
        payload,
        subtitle_text="subtitle",
    )
    latest_record = latest_artifact_for_bvid(artifact_root, "BV1xx411c7mD")
    txt_path = latest_artifact_txt(artifact_root, "BV1xx411c7mD", analysis_id=record["analysis_id"])
    assert_true((download_dir / record["csv_filename"]).exists(), "artifact CSV should be written")
    assert_true(txt_path is not None and txt_path.exists(), "artifact TXT should be discoverable")
    assert_true(latest_record is not None and latest_record["count"] == 1, "artifact manifest roundtrip failed")
    assert_true(latest_record["analysis_id"] == record["analysis_id"], "latest artifact lookup should return saved record")
    assert_true((subtitle_dir / record["subtitle_filename"]).exists(), "subtitle file should be written")


def assert_job_store_roundtrip(root: Path) -> None:
    db_path = root / "job-smoke" / "jobs.sqlite3"
    ensure_state_db(db_path)
    job_id = make_job_id(42)
    now = "2026-04-21T10:00:00+08:00"
    with connect_state_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, kind, status, payload_json, result_json, error_json,
                created_at, updated_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL)
            """,
            (
                job_id,
                "analysis",
                "queued",
                json.dumps({"bvid": "BV1xx411c7mD"}, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO job_events (job_id, type, message, ts) VALUES (?, ?, ?, ?)",
            (job_id, "info", "任务已创建，等待执行", now),
        )
        conn.execute(
            "INSERT INTO job_events (job_id, type, message, ts) VALUES (?, ?, ?, ?)",
            (job_id, "info", "extra event", "2026-04-21T10:00:01+08:00"),
        )

    with connect_state_db(db_path) as conn:
        events, offset = read_events_since(conn, job_id, 0)
        more_events, next_offset = read_events_since(conn, job_id, offset)
    assert_true(len(events) == 2, "job events should be readable from offset 0")
    assert_true(more_events == [], "job event offset should suppress already consumed entries")
    assert_true(next_offset == offset, "job event offset should remain stable when idle")


def append_artifact_record(
    artifact_root: Path,
    bvid: str,
    danmaku_list: list[dict[str, Any]],
    subtitle_text: str | None = None,
) -> dict[str, Any]:
    download_dir = artifact_root / "downloads"
    subtitle_dir = artifact_root / "subtitles"
    index_dir = artifact_root / ".artifact-index"
    download_dir.mkdir(parents=True, exist_ok=True)
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)

    analysis_id = make_analysis_id(len(_read_artifact_records(artifact_root)) + 1)
    timestamp = "20260421_120000"
    csv_filename = f"danmaku_{bvid}_{timestamp}.csv"
    txt_filename = f"danmaku_{bvid}_{timestamp}.txt"
    csv_path = download_dir / csv_filename
    txt_path = download_dir / txt_filename
    with csv_path.open("w", encoding="utf-8") as file:
        file.write("弹幕内容,出现时间(秒),发送时间,类型,字体大小,颜色,发送者ID\n")
        for item in danmaku_list:
            file.write(
                f"{item['text']},{item['appear_time']},{item['send_time']},{item['type']},{item['size']},{item['color']},{item['sender']}\n"
            )
    with txt_path.open("w", encoding="utf-8") as file:
        for item in danmaku_list:
            minutes = int(item["appear_time"]) // 60
            seconds = int(item["appear_time"]) % 60
            file.write(f"[{minutes:02d}:{seconds:02d}] {item['text']}\n")

    subtitle_filename = None
    if subtitle_text is not None:
        subtitle_filename = f"subtitle_{bvid}_{timestamp}.txt"
        (subtitle_dir / subtitle_filename).write_text(subtitle_text, encoding="utf-8")

    record = {
        "analysis_id": analysis_id,
        "bvid": bvid,
        "csv_filename": csv_filename,
        "txt_filename": txt_filename,
        "subtitle_filename": subtitle_filename,
        "subtitle_original_filename": "input.txt" if subtitle_text is not None else None,
        "count": len(danmaku_list),
        "created_at": "2026-04-21T10:00:00+08:00",
        "updated_at": "2026-04-21T10:05:00+08:00" if subtitle_text is not None else None,
    }
    with (index_dir / "manifest.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def _read_artifact_records(artifact_root: Path) -> dict[str, dict[str, Any]]:
    manifest_path = artifact_root / ".artifact-index" / "manifest.jsonl"
    records: dict[str, dict[str, Any]] = {}
    if not manifest_path.exists():
        return records
    with manifest_path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            records[str(record["analysis_id"])] = record
    return records


def latest_artifact_for_bvid(artifact_root: Path, bvid: str) -> dict[str, Any] | None:
    records = [record for record in _read_artifact_records(artifact_root).values() if record.get("bvid") == bvid]
    if not records:
        return None
    return max(records, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))


def latest_artifact_txt(artifact_root: Path, bvid: str, analysis_id: str | None = None) -> Path | None:
    records = _read_artifact_records(artifact_root)
    if analysis_id:
        record = records.get(analysis_id)
        if record and record.get("bvid") == bvid and record.get("txt_filename"):
            path = artifact_root / "downloads" / str(record["txt_filename"])
            return path if path.exists() else None
        return None

    record = latest_artifact_for_bvid(artifact_root, bvid)
    if not record or not record.get("txt_filename"):
        return None
    path = artifact_root / "downloads" / str(record["txt_filename"])
    return path if path.exists() else None


def migrate_artifacts(conn: sqlite3.Connection, root: Path) -> None:
    manifest_path = root / ".artifact-index" / "manifest.jsonl"
    if not manifest_path.exists():
        return

    with manifest_path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            conn.execute(
                """
                INSERT OR REPLACE INTO artifact_records (
                    analysis_id, bvid, csv_filename, txt_filename, subtitle_filename,
                    subtitle_original_filename, count, created_at, updated_at, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("analysis_id"),
                    record.get("bvid"),
                    record.get("csv_filename"),
                    record.get("txt_filename"),
                    record.get("subtitle_filename"),
                    record.get("subtitle_original_filename"),
                    record.get("count"),
                    record.get("created_at"),
                    record.get("updated_at"),
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                ),
            )


def migrate_jobs(conn: sqlite3.Connection, root: Path) -> None:
    jobs_root = root / ".jobs"
    if not jobs_root.exists():
        return

    for status_path in sorted(jobs_root.glob("*/status.json")):
        job_id = status_path.parent.name
        status_payload = json.loads(status_path.read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT OR REPLACE INTO jobs (
                job_id, kind, status, payload_json, result_json, error_json,
                created_at, updated_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                status_payload.get("kind"),
                status_payload.get("status"),
                json.dumps(status_payload.get("payload"), ensure_ascii=False, sort_keys=True),
                json.dumps(status_payload.get("result"), ensure_ascii=False, sort_keys=True)
                if status_payload.get("result") is not None
                else None,
                json.dumps(status_payload.get("error"), ensure_ascii=False, sort_keys=True)
                if status_payload.get("error") is not None
                else None,
                status_payload.get("created_at"),
                status_payload.get("updated_at"),
                status_payload.get("started_at"),
                status_payload.get("finished_at"),
            ),
        )

        events_path = status_path.parent / "events.jsonl"
        if not events_path.exists():
            continue
        with events_path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                conn.execute(
                    """
                    INSERT INTO job_events (job_id, type, message, ts)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        event.get("type"),
                        event.get("message"),
                        event.get("ts"),
                    ),
                )


def read_events_since(conn: sqlite3.Connection, job_id: str, offset: int) -> tuple[list[dict[str, Any]], int]:
    rows = conn.execute(
        """
        SELECT id, job_id, type, message, ts
        FROM job_events
        WHERE job_id = ? AND id > ?
        ORDER BY id ASC
        """,
        (job_id, offset),
    ).fetchall()
    events = [dict(row) for row in rows]
    next_offset = events[-1]["id"] if events else offset
    return events, next_offset


def claim_next_job(db_path: Path, max_concurrent: int) -> str | None:
    with connect_state_db(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0]
            if running >= max_concurrent:
                conn.execute("ROLLBACK")
                return None
            row = conn.execute(
                """
                SELECT job_id
                FROM jobs
                WHERE status = 'queued'
                ORDER BY created_at, job_id
                LIMIT 1
                """
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return None
            job_id = row[0]
            timestamp = now_iso()
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE job_id = ?
                """,
                (timestamp, timestamp, job_id),
            )
            conn.execute("COMMIT")
            return job_id
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise


def finish_job(db_path: Path, job_id: str) -> None:
    with connect_state_db(db_path) as conn:
        timestamp = now_iso()
        conn.execute(
            """
            UPDATE jobs
            SET status = 'succeeded', finished_at = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (timestamp, timestamp, job_id),
        )


def assert_schema(conn: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        )
    }
    indexes = {
        row["name"]
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'index'
            """
        )
    }
    required_tables = {"state_meta", "artifact_records", "jobs", "job_events"}
    required_indexes = {"idx_artifact_records_bvid_created", "idx_jobs_status_created", "idx_job_events_job_id_id"}
    assert_true(required_tables.issubset(tables), f"missing tables: {sorted(required_tables - tables)}")
    assert_true(required_indexes.issubset(indexes), f"missing indexes: {sorted(required_indexes - indexes)}")

    version = conn.execute("SELECT value FROM state_meta WHERE key = 'schema_version'").fetchone()
    assert_true(version is not None and version[0] == "1", "schema_version should be initialized to 1")


def assert_migration(conn: sqlite3.Connection, expected: dict[str, Any]) -> None:
    artifact_count = conn.execute("SELECT COUNT(*) FROM artifact_records").fetchone()[0]
    assert_true(artifact_count == 1, f"expected 1 migrated artifact record, got {artifact_count}")

    row = conn.execute(
        """
        SELECT analysis_id, bvid, csv_filename, txt_filename, subtitle_filename,
               subtitle_original_filename, count, created_at, updated_at, data_json
        FROM artifact_records
        WHERE analysis_id = ?
        """,
        (expected["analysis_id"],),
    ).fetchone()
    assert_true(row is not None, "migrated artifact record missing")
    assert_true(row["subtitle_filename"] is not None, "subtitle filename should be migrated")
    assert_true(row["subtitle_original_filename"] == "input.txt", "subtitle original filename mismatch")

    job_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert_true(job_count == 2, f"expected 2 migrated jobs, got {job_count}")

    events_for_first = conn.execute(
        "SELECT COUNT(*) FROM job_events WHERE job_id = ?",
        (expected["job_ids"][0],),
    ).fetchone()[0]
    assert_true(events_for_first == 3, f"expected 3 events for succeeded job, got {events_for_first}")


def assert_event_offset(conn: sqlite3.Connection, job_id: str) -> None:
    events, offset = read_events_since(conn, job_id, 0)
    assert_true(len(events) == 3, f"expected 3 events on first read, got {len(events)}")
    assert_true(offset > 0, "offset should advance after first read")
    more_events, next_offset = read_events_since(conn, job_id, offset)
    assert_true(more_events == [], "no events should be returned after offset catches up")
    assert_true(next_offset == offset, "offset should stay stable when no new events exist")


def assert_concurrent_claim_limit(db_path: Path, max_concurrent: int) -> None:
    with connect_state_db(db_path) as conn:
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM job_events")
        for seed in range(1, 9):
            job_id = make_job_id(100 + seed)
            timestamp = datetime(2026, 4, 21, 10, seed, tzinfo=timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, status, payload_json, result_json, error_json,
                    created_at, updated_at, started_at, finished_at
                ) VALUES (?, ?, 'queued', ?, NULL, NULL, ?, ?, NULL, NULL)
                """,
                (
                    job_id,
                    "analysis",
                    json.dumps({"seed": seed}, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    timestamp,
                ),
            )

    active_claims = 0
    observed_max = 0
    lock = threading.Lock()
    stop = threading.Event()

    def worker() -> None:
        nonlocal active_claims, observed_max
        while not stop.is_set():
            job_id = claim_next_job(db_path, max_concurrent=max_concurrent)
            if not job_id:
                with connect_state_db(db_path) as conn:
                    queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
                if queued == 0:
                    break
                time.sleep(0.01)
                continue
            with lock:
                active_claims += 1
                observed_max = max(observed_max, active_claims)
            time.sleep(0.08)
            finish_job(db_path, job_id)
            with lock:
                active_claims -= 1

    def monitor() -> None:
        nonlocal observed_max
        while not stop.is_set():
            with connect_state_db(db_path) as conn:
                running = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0]
            with lock:
                observed_max = max(observed_max, running)
            time.sleep(0.01)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stop.set()
    monitor_thread.join(timeout=1.0)

    with connect_state_db(db_path) as conn:
        remaining_running = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0]
        succeeded = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'succeeded'").fetchone()[0]
    assert_true(remaining_running == 0, "no jobs should remain running after claim workers finish")
    assert_true(succeeded == 8, f"expected 8 succeeded jobs, got {succeeded}")
    assert_true(observed_max <= max_concurrent, f"concurrent claims exceeded max_concurrent: {observed_max} > {max_concurrent}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test SQLite state initialization and migration shape.")
    parser.add_argument("--keep-tmp", action="store_true", help="keep the temporary workspace for inspection")
    parser.add_argument("--max-concurrent", type=int, default=3, help="concurrent claim ceiling used in the smoke test")
    args = parser.parse_args()

    tmp_ctx = tempfile.TemporaryDirectory(prefix="sqlite-state-smoke-")
    root = Path(tmp_ctx.name)
    if args.keep_tmp:
        print(f"workspace: {root}")

    try:
        legacy = build_legacy_state(root)
        db_path = root / ".state" / "bilibili_danmaku.sqlite3"
        assert_artifact_store_roundtrip(root)
        assert_job_store_roundtrip(root)
        ensure_state_db(db_path)

        with connect_state_db(db_path) as conn:
            assert_schema(conn)
            migrate_artifacts(conn, root)
            migrate_jobs(conn, root)
            conn.execute("INSERT OR REPLACE INTO state_meta(key, value) VALUES (?, ?)", ("schema_version", "1"))
            conn.commit()
            assert_migration(conn, legacy)
            assert_event_offset(conn, legacy["job_ids"][0])

        assert_concurrent_claim_limit(db_path, max_concurrent=args.max_concurrent)
        print("OK: SQLite schema initialization, legacy migration, artifact/job state, event offsets, and claim ceiling verified.")
    finally:
        if args.keep_tmp:
            print("temporary workspace preserved")
        else:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    main()
