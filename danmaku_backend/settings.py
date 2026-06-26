from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(os.getenv("BILI_DANMAKU_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
DOWNLOAD_DIR = Path(os.getenv("BILI_DANMAKU_DOWNLOAD_DIR", PROJECT_ROOT / "downloads"))
SUBTITLE_DIR = Path(os.getenv("BILI_DANMAKU_SUBTITLE_DIR", PROJECT_ROOT / "subtitles"))
COMMENT_DIR = Path(os.getenv("BILI_DANMAKU_COMMENT_DIR", PROJECT_ROOT / "comments"))
REPORT_DIR = Path(os.getenv("BILI_DANMAKU_REPORT_DIR", PROJECT_ROOT / "reports"))
REPORT_ARCHIVE_DIR = Path(os.getenv("BILI_DANMAKU_REPORT_ARCHIVE_DIR", REPORT_DIR / "archive"))
STATIC_DIR = Path(os.getenv("BILI_DANMAKU_STATIC_DIR", PROJECT_ROOT / "static"))
TEMPLATE_DIR = Path(os.getenv("BILI_DANMAKU_TEMPLATE_DIR", PROJECT_ROOT / "templates"))
LOG_FILE = Path(os.getenv("BILI_DANMAKU_LOG_FILE", PROJECT_ROOT / "app.log"))
ACCESS_LOG_FILE = Path(os.getenv("BILI_DANMAKU_ACCESS_LOG_FILE", "/www/wwwlogs/bilibili_danmaku.log"))
EN_ACCESS_LOG_FILE = Path(os.getenv("BILI_DANMAKU_EN_ACCESS_LOG_FILE", "/www/wwwlogs/en.danmu.liu-qi.cn.log"))
JOB_DIR = Path(os.getenv("BILI_DANMAKU_JOB_DIR", PROJECT_ROOT / ".jobs"))
STATE_DIR = Path(os.getenv("BILI_DANMAKU_STATE_DIR", PROJECT_ROOT / ".state"))
STATE_DB_PATH = Path(os.getenv("BILI_DANMAKU_STATE_DB", STATE_DIR / "bilibili_danmaku.sqlite3"))
OPS_DASHBOARD_CACHE_FILE = Path(os.getenv("BILI_DANMAKU_OPS_CACHE_FILE", STATE_DIR / "ops_dashboard_cache.json"))

MAX_UPLOAD_BYTES = int(os.getenv("BILI_DANMAKU_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024)))
LOG_STREAM_MAX_AGE_SECONDS = int(os.getenv("BILI_DANMAKU_LOG_STREAM_MAX_AGE_SECONDS", "900"))
LOG_STREAM_MAX_PER_IP = int(os.getenv("BILI_DANMAKU_LOG_STREAM_MAX_PER_IP", "1"))
ARTIFACT_RETENTION_SECONDS = int(os.getenv("BILI_DANMAKU_ARTIFACT_RETENTION_SECONDS", str(7 * 24 * 60 * 60)))
JOB_RETENTION_SECONDS = int(os.getenv("BILI_DANMAKU_JOB_RETENTION_SECONDS", str(7 * 24 * 60 * 60)))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("BILI_DANMAKU_CLEANUP_INTERVAL_SECONDS", str(60 * 60)))
JOB_LEASE_SECONDS = int(os.getenv("BILI_DANMAKU_JOB_LEASE_SECONDS", str(30 * 60)))
JOB_DISPATCH_POLL_INTERVAL_SECONDS = int(
    os.getenv("BILI_DANMAKU_JOB_DISPATCH_POLL_INTERVAL_SECONDS", "2")
)
REPORT_RETENTION_DAYS = int(os.getenv("BILI_DANMAKU_REPORT_RETENTION_DAYS", "30"))
