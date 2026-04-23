from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from danmaku_backend.settings import REPORT_ARCHIVE_DIR, REPORT_DIR, REPORT_RETENTION_DAYS
from danmaku_backend.services.bilibili import BV_RE


REPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ReportStore:
    def __init__(
        self,
        report_dir: Path = REPORT_DIR,
        archive_dir: Path = REPORT_ARCHIVE_DIR,
    ):
        self.report_dir = Path(report_dir)
        self.archive_dir = Path(archive_dir)
        self.config_path = self.report_dir / "config.json"
        self.cleanup_stamp_path = self.report_dir / ".last_cleanup"

    def ensure(self) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.report_dir, 0o755)
        os.chmod(self.archive_dir, 0o755)
        if not self.config_path.exists():
            self._write_json_file(self.config_path, self._default_config())
            try:
                os.chmod(self.config_path, 0o644)
            except Exception:
                pass

    def public_config(self) -> dict[str, Any]:
        config = self.get_config()
        return {
            "retention_days": int(config["retention_days"]),
        }

    def get_config(self) -> dict[str, Any]:
        self.ensure()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        retention_days = self._positive_int(raw.get("retention_days"), REPORT_RETENTION_DAYS)
        baidu_submit = raw.get("baidu_submit", {})
        if not isinstance(baidu_submit, dict):
            baidu_submit = {}
        config = {
            "retention_days": retention_days,
            "baidu_submit": {
                "enabled": bool(baidu_submit.get("enabled", False)),
                "site": str(baidu_submit.get("site") or "https://danmu.liu-qi.cn").strip(),
                "dedupe_days": self._positive_int(baidu_submit.get("dedupe_days"), 30),
            },
        }
        if raw != config:
            self._write_json_file(self.config_path, config)
        return config

    def save_report(
        self,
        *,
        bvid: str,
        snapshot: dict[str, Any],
        analysis_id: str | None = None,
        content_analysis: dict[str, Any] | None = None,
        deep_analysis: dict[str, Any] | None = None,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure()
        self.cleanup_if_due()
        self._validate_bvid(bvid)
        snapshot_payload = self._validate_snapshot(snapshot)
        content_payload = self._validate_analysis_block(content_analysis, "content_analysis")
        deep_payload = self._validate_analysis_block(deep_analysis, "deep_analysis")
        if not content_payload and not deep_payload:
            raise ValueError("至少需要保存一个分析结果")

        existing = None
        report_id = self._normalize_report_id(report_id)
        if report_id:
            existing = self._read_active_report(report_id)
        if not report_id or not existing:
            report_id = uuid.uuid4().hex
            created_at = self._now()
        else:
            created_at = existing.get("created_at") or self._now()

        config = self.get_config()
        now = self._now()
        expires_at = (datetime.now().astimezone() + timedelta(days=int(config["retention_days"]))).isoformat(
            timespec="seconds"
        )
        report = {
            "schema_version": "1.0",
            "report_id": report_id,
            "bvid": bvid,
            "analysis_id": analysis_id or "",
            "created_at": created_at,
            "updated_at": now,
            "expires_at": expires_at,
            "snapshot": snapshot_payload,
            "content_analysis": content_payload,
            "deep_analysis": deep_payload,
        }
        self._write_json_file(self._report_path(report_id), report)
        return report

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        self.ensure()
        report_id = self._normalize_report_id(report_id, required=True)
        path = self._report_path(report_id)
        report = self._read_json_file(path)
        if not report:
            return None
        if self._is_expired(report):
            self._archive_report(path, report)
            return None
        return report

    def cleanup_if_due(self, *, force: bool = False) -> None:
        self.ensure()
        if not force and self.cleanup_stamp_path.exists():
            try:
                age_seconds = datetime.now().timestamp() - self.cleanup_stamp_path.stat().st_mtime
            except Exception:
                age_seconds = 0
            if age_seconds < 60 * 60:
                return
        self.archive_expired_reports()
        self.cleanup_stamp_path.write_text(self._now(), encoding="utf-8")

    def archive_expired_reports(self) -> int:
        self.ensure()
        moved = 0
        for path in self.report_dir.glob("*.json"):
            if path.name == self.config_path.name:
                continue
            report = self._read_json_file(path)
            if not report:
                continue
            if self._is_expired(report):
                self._archive_report(path, report)
                moved += 1
        return moved

    def _archive_report(self, path: Path, report: dict[str, Any]) -> None:
        archive_path = self.archive_dir / path.name
        archive_payload = {
            **report,
            "archived_at": self._now(),
        }
        self._write_json_file(archive_path, archive_payload)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _validate_bvid(self, bvid: str) -> None:
        if not BV_RE.fullmatch(str(bvid or "").strip()):
            raise ValueError("无效的 BV 号")

    def _validate_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise ValueError("报告快照格式无效")
        video_info = snapshot.get("video_info")
        charts = snapshot.get("charts")
        if not isinstance(video_info, dict) or not isinstance(charts, dict):
            raise ValueError("报告快照缺少必要字段")
        return snapshot

    def _validate_analysis_block(self, value: dict[str, Any] | None, field_name: str) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"{field_name} 格式无效")
        return value

    def _default_config(self) -> dict[str, Any]:
        return {
            "retention_days": REPORT_RETENTION_DAYS,
            "baidu_submit": {
                "enabled": False,
                "site": "https://danmu.liu-qi.cn",
                "dedupe_days": 30,
            },
        }

    def _positive_int(self, value: Any, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    def _normalize_report_id(self, report_id: str | None, *, required: bool = False) -> str | None:
        if report_id is None:
            if required:
                raise ValueError("缺少 report_id")
            return None
        normalized = str(report_id).strip()
        if not normalized:
            if required:
                raise ValueError("缺少 report_id")
            return None
        if not REPORT_ID_RE.fullmatch(normalized):
            raise ValueError("无效的 report_id")
        return normalized

    def _read_active_report(self, report_id: str) -> dict[str, Any] | None:
        path = self._report_path(report_id)
        report = self._read_json_file(path)
        if not report:
            return None
        if self._is_expired(report):
            self._archive_report(path, report)
            return None
        return report

    def _read_json_file(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_json_file(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
        ) as tmp_file:
            json.dump(payload, tmp_file, ensure_ascii=False, indent=2, sort_keys=True)
            tmp_name = tmp_file.name
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o644)
        except Exception:
            pass

    def _report_path(self, report_id: str) -> Path:
        return self.report_dir / f"{report_id}.json"

    def _is_expired(self, report: dict[str, Any]) -> bool:
        expires_at = str(report.get("expires_at") or "").strip()
        if not expires_at:
            return False
        try:
            return datetime.fromisoformat(expires_at) <= datetime.now().astimezone()
        except ValueError:
            return False

    def _now(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")


default_report_store = ReportStore()
