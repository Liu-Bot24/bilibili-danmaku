from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from werkzeug.utils import secure_filename

from danmaku_backend.services.bilibili import BV_RE
from danmaku_backend.services.database import connect_state_db, ensure_state_db
from danmaku_backend.settings import (
    ARTIFACT_RETENTION_SECONDS,
    CLEANUP_INTERVAL_SECONDS,
    COMMENT_DIR,
    DOWNLOAD_DIR,
    STATE_DB_PATH,
    SUBTITLE_DIR,
)


ANALYSIS_ID_RE = r"^[0-9a-f]{32}$"
DANMAKU_CACHE_WINDOW_SECONDS = 3 * 60 * 60
CSV_DOWNLOAD_SUBDIR = "CSV"
TXT_DOWNLOAD_SUBDIR = "TXT"
SUPPORTED_SUBTITLE_EXTENSIONS = {".txt", ".md", ".srt"}
SRT_TIMESTAMP_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}"
)


@dataclass(frozen=True)
class DanmakuExport:
    analysis_id: str
    bvid: str
    csv_filename: str
    txt_filename: str
    count: int
    created_at: str
    site: str = ""
    host: str = ""


class ArtifactStore:
    def __init__(
        self,
        download_dir: Path = DOWNLOAD_DIR,
        subtitle_dir: Path = SUBTITLE_DIR,
        db_path: Path = STATE_DB_PATH,
    ):
        self.download_dir = Path(download_dir)
        self.csv_dir = self.download_dir / CSV_DOWNLOAD_SUBDIR
        self.txt_dir = self.download_dir / TXT_DOWNLOAD_SUBDIR
        self.subtitle_dir = Path(subtitle_dir)
        self.comment_dir = Path(COMMENT_DIR)
        self.db_path = Path(db_path)
        self.index_dir = self.download_dir.parent / ".artifact-index"
        self.manifest_path = self.index_dir / "manifest.jsonl"
        self.cleanup_stamp_path = self.index_dir / "last_cleanup"

    def ensure(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.csv_dir.mkdir(parents=True, exist_ok=True)
        self.txt_dir.mkdir(parents=True, exist_ok=True)
        self.subtitle_dir.mkdir(parents=True, exist_ok=True)
        self.comment_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.download_dir, 0o755)
        os.chmod(self.csv_dir, 0o755)
        os.chmod(self.txt_dir, 0o755)
        os.chmod(self.subtitle_dir, 0o755)
        os.chmod(self.comment_dir, 0o755)
        os.chmod(self.index_dir, 0o755)
        ensure_state_db(self.db_path)
        self._migrate_legacy_manifest_once()

    def _validate_bvid(self, bvid: str) -> str:
        if not BV_RE.fullmatch(bvid or ""):
            raise ValueError("invalid bvid")
        return bvid

    def _validate_analysis_id(self, analysis_id: str | None) -> str | None:
        if analysis_id is None or analysis_id == "":
            return None
        if not re.fullmatch(ANALYSIS_ID_RE, analysis_id):
            raise ValueError("invalid analysis_id")
        return analysis_id

    def _available_pair(self, bvid: str) -> tuple[Path, Path]:
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"danmaku_{safe_bvid}_{timestamp}_{uuid.uuid4().hex[:8]}"
        csv_path = self.csv_dir / f"{stem}.csv"
        txt_path = self.txt_dir / f"{stem}.txt"
        suffix = 1
        while csv_path.exists() or txt_path.exists():
            csv_path = self.csv_dir / f"{stem}_{suffix}.csv"
            txt_path = self.txt_dir / f"{stem}_{suffix}.txt"
            suffix += 1
        return csv_path, txt_path

    def save_danmaku_files(
        self,
        bvid: str,
        danmaku_list: list[dict[str, Any]],
        *,
        site: str = "",
        host: str = "",
    ) -> DanmakuExport:
        self.ensure()
        bvid = self._validate_bvid(bvid)
        csv_path, txt_path = self._available_pair(bvid)
        analysis_id = uuid.uuid4().hex

        csv_tmp = self._temp_path(self.csv_dir)
        txt_tmp = self._temp_path(self.txt_dir)
        with csv_tmp.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["弹幕内容", "出现时间(秒)", "发送时间", "类型", "字体大小", "颜色", "发送者ID"])
            for danmaku in danmaku_list:
                writer.writerow(
                    [
                        danmaku["text"],
                        danmaku["appear_time"],
                        danmaku["send_time"],
                        danmaku["type"],
                        danmaku["size"],
                        danmaku["color"],
                        danmaku["sender"],
                    ]
                )

        with txt_tmp.open("w", encoding="utf-8") as file:
            for danmaku in danmaku_list:
                minutes = int(danmaku["appear_time"]) // 60
                seconds = int(danmaku["appear_time"]) % 60
                file.write(f"[{minutes:02d}:{seconds:02d}] {danmaku['text']}\n")

        os.replace(csv_tmp, csv_path)
        os.replace(txt_tmp, txt_path)
        os.chmod(csv_path, 0o644)
        os.chmod(txt_path, 0o644)
        record = DanmakuExport(
            analysis_id=analysis_id,
            bvid=bvid,
            csv_filename=self._download_relative_name(csv_path),
            txt_filename=self._download_relative_name(txt_path),
            count=len(danmaku_list),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            site=self._normalize_site(site),
            host=self._normalize_host(host),
        )
        try:
            self._upsert_record(asdict(record))
        except Exception:
            for path in (csv_path, txt_path):
                try:
                    if path.exists():
                        path.unlink()
                except Exception:
                    pass
            raise
        self.cleanup_if_due()
        return record

    def latest_cached_danmaku_record(
        self,
        bvid: str,
        max_age_seconds: int = DANMAKU_CACHE_WINDOW_SECONDS,
    ) -> dict[str, Any] | None:
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        self.ensure()
        cutoff = datetime.now().astimezone() - timedelta(seconds=max_age_seconds)
        for record in self.records_for_bvid(safe_bvid):
            created_at = self._created_time(record)
            if not created_at or created_at < cutoff:
                continue
            csv_name = record.get("csv_filename")
            txt_name = record.get("txt_filename")
            if not csv_name or not txt_name:
                continue
            csv_path = self.danmaku_csv_path(str(csv_name))
            txt_path = self.danmaku_txt_path(str(txt_name))
            if csv_path.exists() and txt_path.exists():
                return record
        return None

    def latest_danmaku_txt(self, bvid: str, analysis_id: str | None = None) -> Optional[Path]:
        self.ensure()
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        analysis_id = self._validate_analysis_id(analysis_id)
        if analysis_id:
            record = self.get_record(analysis_id)
            if record and record.get("bvid") == safe_bvid:
                path = self.danmaku_txt_path(str(record.get("txt_filename", "")))
                return path if path.exists() else None
            return None

        record = self.latest_record_for_bvid(safe_bvid)
        if record:
            path = self.danmaku_txt_path(str(record.get("txt_filename", "")))
            if path.exists():
                return path

        return None

    def latest_subtitle(self, bvid: str, analysis_id: str | None = None) -> Optional[Path]:
        self.ensure()
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        analysis_id = self._validate_analysis_id(analysis_id)
        if analysis_id:
            record = self.get_record(analysis_id)
            if record and record.get("bvid") == safe_bvid and record.get("subtitle_filename"):
                path = self.subtitle_dir / str(record["subtitle_filename"])
                return path if path.exists() else None
            return None

        record = self.latest_record_for_bvid(safe_bvid, require_subtitle=True)
        if record:
            path = self.subtitle_dir / str(record.get("subtitle_filename", ""))
            if path.exists():
                return path

        return None

    def save_subtitle(
        self,
        file,
        bvid: str,
        analysis_id: str | None = None,
        *,
        site: str = "",
        host: str = "",
    ) -> Path:
        self.ensure()
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        analysis_id = self._validate_analysis_id(analysis_id)
        original_name = self._normalize_original_filename(file.filename or "")
        subtitle_extension = Path(original_name).suffix.lower()
        if subtitle_extension not in SUPPORTED_SUBTITLE_EXTENSIONS:
            raise ValueError("仅支持 TXT、MD、SRT 字幕文件")
        if analysis_id:
            record = self.get_record(analysis_id)
            if not record or record.get("bvid") != safe_bvid:
                raise ValueError("analysis_id does not match bvid")
        else:
            raise ValueError("analysis_id is required")

        content = file.read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        try:
            file.stream.seek(0)
        except Exception:
            pass
        content = self._normalize_subtitle_content(content, subtitle_extension)

        matched_record = self._find_matching_subtitle_record(safe_bvid, content)
        if matched_record and matched_record.get("subtitle_filename"):
            reused_path = self.subtitle_dir / str(matched_record["subtitle_filename"])
            if reused_path.exists():
                self._merge_record_with_site(
                    analysis_id,
                    record,
                    reused_path.name,
                    original_name,
                    site=site,
                    host=host,
                )
                self.cleanup_if_due()
                return reused_path

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.subtitle_dir / f"subtitle_{safe_bvid}_{timestamp}_{uuid.uuid4().hex[:8]}.txt"
        suffix = 1
        while path.exists():
            path = self.subtitle_dir / f"subtitle_{safe_bvid}_{timestamp}_{suffix}.txt"
            suffix += 1
        tmp_path = self._temp_path(self.subtitle_dir)
        tmp_path.write_bytes(content)
        os.replace(tmp_path, path)
        os.chmod(path, 0o644)
        try:
            self._merge_record_with_site(
                analysis_id,
                record,
                path.name,
                original_name,
                site=site,
                host=host,
            )
        except Exception:
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
            raise
        self.cleanup_if_due()
        return path

    def comment_export_path(self, bvid: str, analysis_id: str) -> Path:
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        safe_analysis_id = self._validate_analysis_id(analysis_id)
        if not safe_analysis_id:
            raise ValueError("analysis_id is required")
        return self.comment_dir / f"comment_{safe_bvid}_{safe_analysis_id}.json"

    def save_comment_export(
        self,
        export_data: dict[str, Any],
        bvid: str,
        analysis_id: str | None = None,
        *,
        site: str = "",
        host: str = "",
    ) -> Path:
        self.ensure()
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        safe_analysis_id = self._validate_analysis_id(analysis_id)
        if not safe_analysis_id:
            raise ValueError("analysis_id is required")
        record = self.get_record(safe_analysis_id)
        if not record or record.get("bvid") != safe_bvid:
            raise ValueError("analysis_id does not match bvid")

        path = self.comment_export_path(safe_bvid, safe_analysis_id)
        tmp_path = self._temp_path(self.comment_dir)
        tmp_path.write_text(
            json.dumps(export_data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
        os.chmod(path, 0o644)
        updates = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        if site and not record.get("site"):
            updates["site"] = self._normalize_site(site)
        if host and not record.get("host"):
            updates["host"] = self._normalize_host(host)
        try:
            self._merge_record(safe_analysis_id, updates)
        except Exception:
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
            raise
        self.cleanup_if_due()
        return path

    def latest_comment_export(self, bvid: str, analysis_id: str | None = None) -> dict[str, Any] | None:
        self.ensure()
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        safe_analysis_id = self._validate_analysis_id(analysis_id)
        candidate_paths: list[Path] = []
        if safe_analysis_id:
            record = self.get_record(safe_analysis_id)
            if not record or record.get("bvid") != safe_bvid:
                return None
            candidate_paths.append(self.comment_export_path(safe_bvid, safe_analysis_id))
        else:
            for record in self.records_for_bvid(safe_bvid):
                candidate_paths.append(self.comment_export_path(safe_bvid, str(record["analysis_id"])))

        for path in candidate_paths:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return None

    def safe_download_name(self, filename: str) -> str:
        safe_name = secure_filename(filename or "")
        if not safe_name or safe_name != filename:
            raise ValueError("invalid filename")
        return safe_name

    def safe_download_path(self, filename: str) -> str:
        raw_name = str(filename or "").replace("\\", "/").strip("/")
        parts = raw_name.split("/") if raw_name else []
        if len(parts) == 1:
            return self.safe_download_name(parts[0])
        if len(parts) == 2 and parts[0] in {CSV_DOWNLOAD_SUBDIR, TXT_DOWNLOAD_SUBDIR}:
            safe_name = self.safe_download_name(parts[1])
            suffix = Path(safe_name).suffix.lower()
            if parts[0] == CSV_DOWNLOAD_SUBDIR and suffix != ".csv":
                raise ValueError("invalid filename")
            if parts[0] == TXT_DOWNLOAD_SUBDIR and suffix != ".txt":
                raise ValueError("invalid filename")
            return f"{parts[0]}/{safe_name}"
        raise ValueError("invalid filename")

    def resolve_download_path(self, filename: str) -> Path:
        safe_path = self.safe_download_path(filename)
        candidates = [self.download_dir / safe_path]
        if "/" not in safe_path:
            subdir = self._download_subdir_for_name(safe_path)
            if subdir:
                candidates.append(self.download_dir / subdir / safe_path)
        for path in candidates:
            if path.is_file():
                return path
        raise FileNotFoundError("download not found")

    def danmaku_csv_path(self, filename: str) -> Path:
        safe_path = self.safe_download_path(filename)
        suffix = Path(safe_path).suffix.lower()
        if suffix != ".csv":
            raise ValueError("invalid csv filename")
        return self._preferred_download_path(safe_path)

    def danmaku_txt_path(self, filename: str) -> Path:
        safe_path = self.safe_download_path(filename)
        suffix = Path(safe_path).suffix.lower()
        if suffix != ".txt":
            raise ValueError("invalid txt filename")
        return self._preferred_download_path(safe_path)

    def get_record(self, analysis_id: str) -> dict[str, Any] | None:
        analysis_id = self._validate_analysis_id(analysis_id)
        if not analysis_id:
            return None
        self.ensure()
        with connect_state_db(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT analysis_id, bvid, site, host, csv_filename, txt_filename, subtitle_filename,
                       subtitle_original_filename, count, created_at, updated_at
                FROM artifact_records
                WHERE analysis_id = ?
                """,
                (analysis_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def latest_record_for_bvid(self, bvid: str, require_subtitle: bool = False) -> dict[str, Any] | None:
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        self.ensure()
        sql = """
            SELECT analysis_id, bvid, site, host, csv_filename, txt_filename, subtitle_filename,
                   subtitle_original_filename, count, created_at, updated_at
            FROM artifact_records
            WHERE bvid = ?
        """
        if require_subtitle:
            sql += " AND subtitle_filename IS NOT NULL AND subtitle_filename != ''"
        sql += " ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 1"
        with connect_state_db(self.db_path) as conn:
            row = conn.execute(sql, (safe_bvid,)).fetchone()
        return self._row_to_record(row) if row else None

    def cleanup_if_due(
        self,
        retention_seconds: int = ARTIFACT_RETENTION_SECONDS,
        interval_seconds: int = CLEANUP_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        self.ensure()
        now = datetime.now().astimezone()
        last_run = self._meta_get("artifacts_last_cleanup")
        if last_run:
            try:
                parsed = datetime.fromisoformat(last_run)
                if (now - parsed).total_seconds() < interval_seconds:
                    return {"ran": False, "deleted_files": 0, "deleted_records": 0}
            except Exception:
                pass
        result = self.cleanup(retention_seconds)
        self._meta_set("artifacts_last_cleanup", now.isoformat(timespec="seconds"))
        return {"ran": True, **result}

    def cleanup(self, retention_seconds: int = ARTIFACT_RETENTION_SECONDS) -> dict[str, Any]:
        self.ensure()
        cutoff = datetime.now().astimezone() - timedelta(seconds=retention_seconds)
        records = self._records_by_id()
        deleted_records = 0
        deleted_files = 0
        delete_ids: list[str] = []
        for record in records.values():
            record_time = self._record_time(record)
            if not record_time or record_time >= cutoff:
                continue
            deleted_records += 1
            delete_ids.append(str(record["analysis_id"]))
            for key, folder in (
                ("subtitle_filename", self.subtitle_dir),
            ):
                name = record.get(key)
                if not name:
                    continue
                try:
                    path = folder / self.safe_download_name(str(name))
                except ValueError:
                    continue
                if path.exists():
                    path.unlink()
                    deleted_files += 1
            try:
                comment_path = self.comment_export_path(str(record["bvid"]), str(record["analysis_id"]))
                if comment_path.exists():
                    comment_path.unlink()
                    deleted_files += 1
            except Exception:
                pass
        if delete_ids:
            with connect_state_db(self.db_path) as conn:
                conn.executemany("DELETE FROM artifact_records WHERE analysis_id = ?", [(item,) for item in delete_ids])
        deleted_files += self._cleanup_orphan_files(records, cutoff)
        return {"deleted_files": deleted_files, "deleted_records": deleted_records}

    @staticmethod
    def _temp_path(directory: Path) -> Path:
        fd, name = tempfile.mkstemp(prefix=".tmp-", dir=str(directory))
        os.close(fd)
        return Path(name)

    def _download_relative_name(self, path: Path) -> str:
        return path.relative_to(self.download_dir).as_posix()

    @staticmethod
    def _download_subdir_for_name(filename: str) -> str | None:
        suffix = Path(filename).suffix.lower()
        if suffix == ".csv":
            return CSV_DOWNLOAD_SUBDIR
        if suffix == ".txt":
            return TXT_DOWNLOAD_SUBDIR
        return None

    def _preferred_download_path(self, safe_path: str) -> Path:
        if "/" in safe_path:
            return self.download_dir / safe_path
        subdir = self._download_subdir_for_name(safe_path)
        if subdir:
            subdir_path = self.download_dir / subdir / safe_path
            if subdir_path.exists() or not (self.download_dir / safe_path).exists():
                return subdir_path
        return self.download_dir / safe_path

    def _merge_record_with_site(
        self,
        analysis_id: str,
        record: dict[str, Any],
        subtitle_filename: str,
        original_name: str,
        *,
        site: str = "",
        host: str = "",
    ) -> None:
        updates = {
            "subtitle_filename": subtitle_filename,
            "subtitle_original_filename": original_name,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if site and not record.get("site"):
            updates["site"] = self._normalize_site(site)
        if host and not record.get("host"):
            updates["host"] = self._normalize_host(host)
        self._merge_record(analysis_id, updates)

    def _merge_record(self, analysis_id: str, updates: dict[str, Any]) -> None:
        record = self.get_record(analysis_id)
        if not record:
            raise ValueError("analysis_id not found")
        merged = dict(record)
        merged.update(updates)
        self._upsert_record(merged)

    def _records_by_id(self) -> dict[str, dict[str, Any]]:
        self.ensure()
        with connect_state_db(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT analysis_id, bvid, site, host, csv_filename, txt_filename, subtitle_filename,
                       subtitle_original_filename, count, created_at, updated_at
                FROM artifact_records
                """
            ).fetchall()
        return {row["analysis_id"]: self._row_to_record(row) for row in rows}

    def records_for_bvid(self, bvid: str, require_subtitle: bool = False) -> list[dict[str, Any]]:
        safe_bvid = secure_filename(self._validate_bvid(bvid))
        self.ensure()
        sql = """
            SELECT analysis_id, bvid, site, host, csv_filename, txt_filename, subtitle_filename,
                   subtitle_original_filename, count, created_at, updated_at
            FROM artifact_records
            WHERE bvid = ?
        """
        if require_subtitle:
            sql += " AND subtitle_filename IS NOT NULL AND subtitle_filename != ''"
        sql += " ORDER BY created_at DESC"
        with connect_state_db(self.db_path) as conn:
            rows = conn.execute(sql, (safe_bvid,)).fetchall()
        return [self._row_to_record(row) for row in rows]

    def _cleanup_orphan_files(self, records: dict[str, dict[str, Any]], cutoff: datetime) -> int:
        referenced: set[Path] = set()
        for record in records.values():
            for key, folder in (
                ("subtitle_filename", self.subtitle_dir),
            ):
                name = record.get(key)
                if not name:
                    continue
                try:
                    referenced.add((folder / self.safe_download_name(str(name))).resolve())
                except ValueError:
                    continue
            try:
                comment_path = self.comment_export_path(str(record["bvid"]), str(record["analysis_id"]))
                if comment_path.exists():
                    referenced.add(comment_path.resolve())
            except Exception:
                continue

        deleted = 0
        # Keep user-visible danmaku exports in DOWNLOAD_DIR for manual lookup/download.
        patterns = (
            (self.subtitle_dir, "subtitle_*.txt"),
            (self.comment_dir, "comment_*.json"),
            (self.download_dir, ".tmp-*"),
            (self.csv_dir, ".tmp-*"),
            (self.txt_dir, ".tmp-*"),
            (self.subtitle_dir, ".tmp-*"),
            (self.comment_dir, ".tmp-*"),
        )
        for folder, pattern in patterns:
            if not folder.exists():
                continue
            for path in folder.glob(pattern):
                try:
                    if path.resolve() in referenced:
                        continue
                    modified_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
                    if modified_at >= cutoff:
                        continue
                    path.unlink()
                    deleted += 1
                except Exception:
                    continue
        return deleted

    def _upsert_record(self, record: dict[str, Any]) -> None:
        analysis_id = self._validate_analysis_id(record.get("analysis_id"))
        if not analysis_id:
            raise ValueError("invalid analysis_id")
        bvid = secure_filename(self._validate_bvid(str(record.get("bvid") or "")))
        created_at = str(record.get("created_at") or datetime.now().astimezone().isoformat(timespec="seconds"))
        updated_at = str(record.get("updated_at") or created_at)
        count = int(record.get("count") or 0)
        csv_filename = self._normalize_download_filename(record.get("csv_filename"), ".csv")
        txt_filename = self._normalize_download_filename(record.get("txt_filename"), ".txt")
        subtitle_filename = self._normalize_filename(record.get("subtitle_filename"))
        subtitle_original_filename = self._normalize_original_filename(
            record.get("subtitle_original_filename")
        )
        site = self._normalize_site(record.get("site"))
        host = self._normalize_host(record.get("host"))
        with connect_state_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO artifact_records (
                    analysis_id, bvid, site, host, csv_filename, txt_filename, subtitle_filename,
                    subtitle_original_filename, count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(analysis_id) DO UPDATE SET
                    bvid = excluded.bvid,
                    site = excluded.site,
                    host = excluded.host,
                    csv_filename = excluded.csv_filename,
                    txt_filename = excluded.txt_filename,
                    subtitle_filename = excluded.subtitle_filename,
                    subtitle_original_filename = excluded.subtitle_original_filename,
                    count = excluded.count,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    analysis_id,
                    bvid,
                    site,
                    host,
                    csv_filename,
                    txt_filename,
                    subtitle_filename,
                    subtitle_original_filename,
                    count,
                    created_at,
                    updated_at,
                ),
            )

    @staticmethod
    def _created_time(record: dict[str, Any]) -> datetime | None:
        raw = record.get("created_at")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.astimezone()

    @staticmethod
    def _record_time(record: dict[str, Any]) -> datetime | None:
        raw = record.get("updated_at") or record.get("created_at")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.astimezone()

    def _migrate_legacy_manifest_once(self) -> None:
        if self._meta_get("legacy_artifacts_migrated") == "1":
            return
        if self.manifest_path.exists():
            with self.manifest_path.open("r", encoding="utf-8") as manifest:
                for line in manifest:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        self._upsert_record(record)
                    except Exception:
                        continue
        self._meta_set("legacy_artifacts_migrated", "1")

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
    def _row_to_record(row) -> dict[str, Any]:
        record = {
            "analysis_id": row["analysis_id"],
            "bvid": row["bvid"],
            "count": int(row["count"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for key in ("site", "host", "csv_filename", "txt_filename", "subtitle_filename", "subtitle_original_filename"):
            if row[key] is not None:
                record[key] = row[key]
        return record

    @staticmethod
    def _normalize_site(value: Any) -> str:
        text = str(value or "").strip().lower()
        return text if text in {"cn", "en"} else ""

    @staticmethod
    def _normalize_host(value: Any) -> str:
        return str(value or "").strip().lower()[:120]

    @staticmethod
    def _normalize_filename(value: Any) -> str | None:
        if value in (None, ""):
            return None
        name = secure_filename(str(value))
        if not name or name != str(value):
            raise ValueError("invalid filename")
        return name

    def _normalize_download_filename(self, value: Any, expected_suffix: str) -> str | None:
        if value in (None, ""):
            return None
        name = self.safe_download_path(str(value))
        if Path(name).suffix.lower() != expected_suffix:
            raise ValueError("invalid filename")
        return name

    @staticmethod
    def _normalize_original_filename(value: Any) -> str | None:
        if value in (None, ""):
            return None
        name = str(value).replace("\\", "/").split("/")[-1].strip()
        if not name or name in {".", ".."} or "\x00" in name:
            raise ValueError("invalid filename")
        return name

    @staticmethod
    def _decode_subtitle_bytes(content: bytes) -> str:
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return content.decode("utf-8", errors="replace").lstrip("\ufeff")

    @classmethod
    def _normalize_subtitle_content(cls, content: bytes, extension: str) -> bytes:
        if extension == ".srt":
            return cls._clean_srt_content(content).encode("utf-8")
        return content

    @classmethod
    def _clean_srt_content(cls, content: bytes) -> str:
        text = cls._decode_subtitle_bytes(content)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n{2,}", text)
        cues: list[str] = []

        for block in blocks:
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            if not lines:
                continue

            timestamp_index = next((index for index, line in enumerate(lines) if SRT_TIMESTAMP_RE.match(line)), -1)
            if timestamp_index >= 0:
                payload_lines = lines[timestamp_index + 1 :]
            else:
                payload_lines = [
                    line
                    for line in lines
                    if not line.isdigit() and "-->" not in line
                ]

            cleaned_lines = [cls._clean_srt_text_line(line) for line in payload_lines]
            cleaned_lines = [line for line in cleaned_lines if line]
            if cleaned_lines:
                cues.append(" ".join(cleaned_lines))

        if cues:
            return "\n".join(cues).strip() + "\n"

        fallback_lines = [
            cls._clean_srt_text_line(line)
            for line in text.split("\n")
            if line.strip() and not line.strip().isdigit() and "-->" not in line
        ]
        fallback_lines = [line for line in fallback_lines if line]
        return "\n".join(fallback_lines).strip() + ("\n" if fallback_lines else "")

    @staticmethod
    def _clean_srt_text_line(line: str) -> str:
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{\\.*?\}", "", line)
        line = html.unescape(line)
        return re.sub(r"\s+", " ", line).strip()

    def _find_matching_subtitle_record(self, bvid: str, content: bytes) -> dict[str, Any] | None:
        target_digest = hashlib.md5(content).hexdigest()
        for record in self.records_for_bvid(bvid, require_subtitle=True):
            filename = record.get("subtitle_filename")
            if not filename:
                continue
            path = self.subtitle_dir / str(filename)
            if not path.exists():
                continue
            try:
                existing_digest = hashlib.md5(path.read_bytes()).hexdigest()
            except Exception:
                continue
            if existing_digest == target_digest:
                return record
        return None


default_store = ArtifactStore()
