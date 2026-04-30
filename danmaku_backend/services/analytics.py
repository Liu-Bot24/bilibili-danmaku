from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import hashlib
import ipaddress
import json
import os
import pwd
import re
import threading
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from danmaku_backend.services.bilibili import BILIBILI_HEADERS, BV_RE
from danmaku_backend.services.database import connect_state_db, ensure_state_db
from danmaku_backend.settings import (
    ACCESS_LOG_FILE,
    DOWNLOAD_DIR,
    LOG_FILE,
    OPS_DASHBOARD_CACHE_FILE,
    REPORT_ARCHIVE_DIR,
    REPORT_DIR,
    STATE_DB_PATH,
    SUBTITLE_DIR,
)


ACCESS_LOG_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<method>\S+) (?P<target>[^"]*?) (?P<protocol>[^"]*)" '
    r"(?P<status>\d{3}) (?P<size>\S+) "
    r'"(?P<referer>[^"]*)" "(?P<ua>[^"]*)"'
)
APP_LOG_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+(?P<level>\w+):\s+(?P<message>.*)$")
BOT_RE = re.compile(
    r"bot|spider|crawl|slurp|bytespider|bingpreview|curl|wget|python-requests|httpclient|headless|go-http-client",
    re.IGNORECASE,
)
INTERNAL_HOSTS = {"danmu.liu-qi.cn", "dm.liu-qi.cn"}
ANALYTICS_CACHE_SECONDS = 300
MAX_ACCESS_LOG_BYTES = 80 * 1024 * 1024
OPS_VIDEO_META_CACHE_FILE = OPS_DASHBOARD_CACHE_FILE.with_name("ops_video_meta.json")

FEATURE_LABELS = {
    "page_home": "首页",
    "page_result": "结果页",
    "page_faq": "FAQ",
    "page_plugin": "插件页",
    "download_generate": "弹幕解析",
    "download_csv": "CSV下载",
    "download_txt": "TXT下载",
    "download_file": "文件下载",
    "plugin_download": "插件包下载",
    "subtitle_upload": "字幕上传",
    "content_analysis": "弹幕内容分析",
    "deep_analysis": "字幕深度分析",
    "custom_content": "自主内容分析",
    "custom_deep": "自主深度分析",
    "report_save": "分享报告保存",
    "report_view": "分享报告读取",
    "job_poll": "任务查询",
    "video_cover": "封面代理",
    "api_other": "其他 API",
    "not_found": "404/异常访问",
    "seo_file": "SEO文件",
    "static_asset": "静态资源",
    "other": "其他",
}

PAGE_CATEGORIES = {"page_home", "page_result", "page_faq", "page_plugin"}
DOWNLOAD_CATEGORIES = {"download_csv", "download_txt", "download_file", "plugin_download"}
API_CATEGORIES = {
    "download_generate",
    "subtitle_upload",
    "content_analysis",
    "deep_analysis",
    "custom_content",
    "custom_deep",
    "report_save",
    "report_view",
    "job_poll",
    "video_cover",
    "api_other",
}
SKIP_ANALYTICS_CATEGORIES = {"dashboard", "ops_api", "static_asset", "health", "logs"}

_dashboard_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_dashboard_cache_lock = threading.Lock()
_analytics_db_ready = False
_analytics_db_lock = threading.Lock()


def record_request_event(flask_request, response, duration_ms: float | None = None) -> None:
    category = classify_request(flask_request.method, flask_request.path)[0]
    if category in SKIP_ANALYTICS_CATEGORIES:
        return

    _ensure_analytics_db()
    ip_value = _client_ip(flask_request.headers, flask_request.remote_addr)
    target = flask_request.full_path if flask_request.query_string else flask_request.path
    bvid = _extract_bvid_from_request(flask_request, target)
    analysis_id = _extract_analysis_id_from_request(flask_request, target)
    now = datetime.now().astimezone()
    ua = flask_request.headers.get("User-Agent", "")
    with connect_state_db(STATE_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO analytics_events (
                ts, date, method, path, category, status, duration_ms, ip_hash,
                ip_segment, user_agent_family, is_bot, referer_domain, bvid, analysis_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(timespec="seconds"),
                now.date().isoformat(),
                flask_request.method,
                flask_request.path or "/",
                category,
                int(response.status_code or 0),
                float(duration_ms) if duration_ms is not None else None,
                _ip_hash(ip_value),
                _ip_segment(ip_value),
                _user_agent_family(ua),
                1 if _is_bot(ua) else 0,
                _referer_domain(flask_request.headers.get("Referer", "")),
                bvid,
                analysis_id,
            ),
        )


def build_ops_dashboard(days: int = 30, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    date_keys = _date_keys_for_range(start_date, end_date, days)
    days = len(date_keys)
    cache_key = f"ops:{date_keys[0]}:{date_keys[-1]}"
    now = time.time()
    with _dashboard_cache_lock:
        cached = _dashboard_cache.get(cache_key)
        if cached and now - cached[0] < ANALYTICS_CACHE_SECONDS:
            return cached[1]
    disk_cached = _read_dashboard_disk_cache(cache_key, now)
    if disk_cached:
        with _dashboard_cache_lock:
            _dashboard_cache[cache_key] = (now, disk_cached)
        return disk_cached

    access = _access_log_metrics(date_keys)
    events = _analytics_event_metrics(date_keys)
    artifacts = _artifact_metrics(date_keys)
    jobs = _job_metrics(date_keys)
    reports = _report_metrics(date_keys)
    inventory = _inventory_metrics()
    app_errors = _app_log_errors(date_keys)

    daily = _merge_daily(date_keys, access["daily"], artifacts["daily"], jobs["daily"], reports["daily"], app_errors["daily"])
    dashboard = {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "days": days,
            "range_start": date_keys[0],
            "range_end": date_keys[-1],
            "sources": [
                "Nginx access log",
                "SQLite artifact_records/jobs/analytics_events",
                "reports JSON",
                "runtime file inventory",
                "app.log errors",
            ],
            "access_log": _file_freshness(ACCESS_LOG_FILE),
            "app_log": _file_freshness(LOG_FILE),
            "notes": [
                "PV/UV 默认排除明显爬虫与命令行探测流量。",
                "IP 只展示聚合网段，不返回完整原始 IP。",
                "接口耗时从本次埋点上线后开始累计。",
            ],
        },
        "kpis": _kpis(daily, jobs, events),
        "daily": daily,
        "feature_trends": _feature_trends(date_keys, access["feature_daily"]),
        "download_breakdown": _counter_items(access["download_breakdown"]),
        "status_codes": _counter_items(access["status_codes"]),
        "api_endpoints": _api_endpoint_items(access["api_endpoints"], access["api_endpoint_errors"]),
        "top_pages": _counter_items(access["top_pages"], 10),
        "top_referrers": _counter_items(access["top_referrers"], 10),
        "top_ip_segments": _counter_items(access["top_ip_segments"], 12),
        "top_user_agents": _counter_items(access["top_user_agents"], 8),
        "top_bvids": _top_bvids(
            access["top_bvids"],
            artifacts["top_bvids"],
            reports["top_bvids"],
            video_meta=reports["video_meta"],
        ),
        "jobs": jobs["summary"],
        "artifacts": artifacts["summary"],
        "reports": reports["summary"],
        "inventory": inventory,
        "latency": events["latency"],
        "recent_errors": app_errors["recent"],
    }

    with _dashboard_cache_lock:
        _dashboard_cache[cache_key] = (time.time(), dashboard)
    _write_dashboard_disk_cache(cache_key, dashboard)
    return dashboard


def classify_request(method: str, path: str) -> tuple[str, str]:
    method = (method or "GET").upper()
    path = _normalize_path(path)
    if path in {"/ops", "/ops/"}:
        return "dashboard", "后台看板"
    if path == "/api/v2/ops-dashboard":
        return "ops_api", "看板接口"
    if path == "/health":
        return "health", "健康检查"
    if path == "/logs":
        return "logs", "实时日志"
    if path in {"/robots.txt", "/sitemap.xml", "/sogousiteverification.txt"} or re.fullmatch(r"/[0-9a-f]{32}\.txt", path):
        return "seo_file", "SEO文件"
    if path == "/":
        return "page_home", FEATURE_LABELS["page_home"]
    if path == "/result":
        return "page_result", FEATURE_LABELS["page_result"]
    if path == "/faq":
        return "page_faq", FEATURE_LABELS["page_faq"]
    if path == "/plugin":
        return "page_plugin", FEATURE_LABELS["page_plugin"]
    if method == "POST" and path == "/download":
        return "download_generate", FEATURE_LABELS["download_generate"]
    if path.startswith("/downloads/"):
        lowered = path.lower()
        if "/csv/" in lowered or lowered.endswith(".csv"):
            return "download_csv", FEATURE_LABELS["download_csv"]
        if "/txt/" in lowered or lowered.endswith(".txt"):
            return "download_txt", FEATURE_LABELS["download_txt"]
        return "download_file", "文件下载"
    if path == "/static/plugin/bili-lite-subtitle-store-upload-1.0.2.zip":
        return "plugin_download", FEATURE_LABELS["plugin_download"]
    if path.startswith("/static/"):
        return "static_asset", FEATURE_LABELS["static_asset"]
    if path == "/favicon.ico":
        return "static_asset", FEATURE_LABELS["static_asset"]
    if method == "POST" and path == "/upload_subtitle":
        return "subtitle_upload", FEATURE_LABELS["subtitle_upload"]
    if method == "POST" and path == "/analyze_content":
        return "content_analysis", FEATURE_LABELS["content_analysis"]
    if method == "POST" and path == "/deep_analysis":
        return "deep_analysis", FEATURE_LABELS["deep_analysis"]
    if method == "POST" and path == "/api/v2/custom-analysis/content":
        return "custom_content", FEATURE_LABELS["custom_content"]
    if method == "POST" and path == "/api/v2/custom-analysis/deep":
        return "custom_deep", FEATURE_LABELS["custom_deep"]
    if path == "/api/v2/reports" and method == "POST":
        return "report_save", FEATURE_LABELS["report_save"]
    if path.startswith("/api/v2/reports/"):
        return "report_view", FEATURE_LABELS["report_view"]
    if path.startswith("/api/v2/jobs/"):
        return "job_poll", FEATURE_LABELS["job_poll"]
    if path.startswith("/api/v2/video-cover/"):
        return "video_cover", FEATURE_LABELS["video_cover"]
    if path.startswith("/api/"):
        return "api_other", FEATURE_LABELS["api_other"]
    if path.endswith(".php") or "/wp-" in path or path in {"/.git/config", "/.htpasswd"}:
        return "not_found", FEATURE_LABELS["not_found"]
    return "other", FEATURE_LABELS["other"]


def _ensure_analytics_db() -> None:
    global _analytics_db_ready
    if _analytics_db_ready:
        return
    with _analytics_db_lock:
        if not _analytics_db_ready:
            ensure_state_db(STATE_DB_PATH)
            _analytics_db_ready = True


def _access_log_metrics(date_keys: list[str]) -> dict[str, Any]:
    daily = {key: _empty_daily(key) for key in date_keys}
    date_set = set(date_keys)
    visitors: dict[str, set[str]] = defaultdict(set)
    feature_daily: dict[str, Counter[str]] = defaultdict(Counter)
    top_pages: Counter[str] = Counter()
    top_referrers: Counter[str] = Counter()
    top_ip_segments: Counter[str] = Counter()
    top_user_agents: Counter[str] = Counter()
    top_bvids: Counter[str] = Counter()
    download_breakdown: Counter[str] = Counter()
    status_codes: Counter[str] = Counter()
    api_endpoints: Counter[str] = Counter()
    api_endpoint_errors: Counter[str] = Counter()

    for path in _access_log_paths():
        for row in _iter_access_log(path):
            date_key = row["date"]
            if date_key not in date_set:
                continue
            category, label = classify_request(row["method"], row["path"])
            day = daily[date_key]
            is_bot = row["is_bot"]
            status = row["status"]
            day["all_hits"] += 1
            day["bytes"] += row["bytes"]
            status_codes[str(status)] += 1
            if is_bot:
                day["bot_hits"] += 1
            if status >= 400:
                day["errors"] += 1
            if category in API_CATEGORIES:
                day["api_calls"] += 1
                api_endpoints[label] += 1
                if status >= 400:
                    api_endpoint_errors[label] += 1
            if category in DOWNLOAD_CATEGORIES:
                day["downloads"] += 1
                download_breakdown[label] += 1
                if category == "plugin_download":
                    day["plugin_downloads"] += 1
            is_operator_or_asset = category in SKIP_ANALYTICS_CATEGORIES or category == "seo_file"
            if not is_bot and not is_operator_or_asset:
                feature_daily[date_key][label] += 1
                if category in PAGE_CATEGORIES and row["method"] == "GET" and status < 500:
                    day["pv"] += 1
                    visitors[date_key].add(row["ip_hash"])
                    top_pages[label] += 1
                if category in API_CATEGORIES and status < 500:
                    top_ip_segments[row["ip_segment"]] += 1
                if category in PAGE_CATEGORIES:
                    top_ip_segments[row["ip_segment"]] += 1
                    top_user_agents[row["user_agent_family"]] += 1
                if row["referer_domain"]:
                    top_referrers[row["referer_domain"]] += 1
                if row["bvid"]:
                    top_bvids[row["bvid"]] += 1

    for key, unique_ips in visitors.items():
        daily[key]["uv"] = len(unique_ips)
    return {
        "daily": daily,
        "feature_daily": feature_daily,
        "top_pages": top_pages,
        "top_referrers": top_referrers,
        "top_ip_segments": top_ip_segments,
        "top_user_agents": top_user_agents,
        "top_bvids": top_bvids,
        "download_breakdown": download_breakdown,
        "status_codes": status_codes,
        "api_endpoints": api_endpoints,
        "api_endpoint_errors": api_endpoint_errors,
    }


def _analytics_event_metrics(date_keys: list[str]) -> dict[str, Any]:
    _ensure_analytics_db()
    date_set = set(date_keys)
    durations: list[float] = []
    durations_by_category: dict[str, list[float]] = defaultdict(list)
    event_count = 0
    with connect_state_db(STATE_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT date, category, status, duration_ms
            FROM analytics_events
            WHERE date >= ? AND date <= ?
              AND is_bot = 0
              AND duration_ms IS NOT NULL
            """,
            (date_keys[0], date_keys[-1]),
        ).fetchall()
    for row in rows:
        if row["date"] not in date_set:
            continue
        value = float(row["duration_ms"] or 0)
        if value <= 0:
            continue
        event_count += 1
        durations.append(value)
        label = FEATURE_LABELS.get(row["category"], row["category"])
        durations_by_category[label].append(value)
    by_category = [
        {
            "name": name,
            "count": len(values),
            "p50_ms": _percentile(values, 50),
            "p95_ms": _percentile(values, 95),
        }
        for name, values in sorted(durations_by_category.items(), key=lambda item: len(item[1]), reverse=True)
    ][:10]
    return {
        "latency": {
            "events": event_count,
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
            "by_category": by_category,
        }
    }


def _artifact_metrics(date_keys: list[str]) -> dict[str, Any]:
    _ensure_analytics_db()
    date_set = set(date_keys)
    daily = {key: {"artifact_success": 0, "danmaku_lines": 0, "unique_bvids": 0, "subtitle_attached": 0} for key in date_keys}
    bvids_by_date: dict[str, set[str]] = defaultdict(set)
    top_bvids: Counter[str] = Counter()
    total_records = 0
    total_danmaku = 0
    subtitle_records = 0
    with connect_state_db(STATE_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT bvid, count, created_at, subtitle_filename
            FROM artifact_records
            """
        ).fetchall()
    for row in rows:
        total_records += 1
        count = int(row["count"] or 0)
        total_danmaku += count
        bvid = str(row["bvid"] or "")
        if bvid:
            top_bvids[bvid] += 1
        if row["subtitle_filename"]:
            subtitle_records += 1
        date_key = _date_from_iso(row["created_at"])
        if date_key not in date_set:
            continue
        daily[date_key]["artifact_success"] += 1
        daily[date_key]["danmaku_lines"] += count
        if row["subtitle_filename"]:
            daily[date_key]["subtitle_attached"] += 1
        if bvid:
            bvids_by_date[date_key].add(bvid)
    for key, values in bvids_by_date.items():
        daily[key]["unique_bvids"] = len(values)
    return {
        "daily": daily,
        "top_bvids": top_bvids,
        "summary": {
            "total_records": total_records,
            "unique_bvids": len(top_bvids),
            "total_danmaku_lines": total_danmaku,
            "subtitle_records": subtitle_records,
        },
    }


def _job_metrics(date_keys: list[str]) -> dict[str, Any]:
    _ensure_analytics_db()
    date_set = set(date_keys)
    daily = {key: {"analysis_jobs": 0, "job_succeeded": 0, "job_failed": 0} for key in date_keys}
    by_kind_status: Counter[str] = Counter()
    active = {"queued": 0, "running": 0}
    durations: list[float] = []
    trends: dict[str, Counter[str]] = defaultdict(Counter)
    with connect_state_db(STATE_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT kind, status, created_at, started_at, finished_at
            FROM jobs
            """
        ).fetchall()
    for row in rows:
        kind = str(row["kind"] or "unknown")
        status = str(row["status"] or "unknown")
        by_kind_status[f"{kind}:{status}"] += 1
        if status in active:
            active[status] += 1
        date_key = _date_from_iso(row["created_at"])
        label = f"{_job_kind_label(kind)} / {_job_status_label(status)}"
        if date_key in date_set:
            daily[date_key]["analysis_jobs"] += 1
            trends[date_key][label] += 1
            if status == "succeeded":
                daily[date_key]["job_succeeded"] += 1
            if status == "failed":
                daily[date_key]["job_failed"] += 1
        started = _parse_iso(row["started_at"])
        finished = _parse_iso(row["finished_at"])
        if started and finished and finished >= started:
            durations.append((finished - started).total_seconds())
    trend_names = sorted({name for counts in trends.values() for name in counts})
    return {
        "daily": daily,
        "summary": {
            "active": active,
            "by_kind_status": [
                {
                    "kind": _job_kind_label(key.split(":", 1)[0]),
                    "status": _job_status_label(key.split(":", 1)[1]),
                    "count": value,
                }
                for key, value in sorted(by_kind_status.items())
            ],
            "duration_seconds": {
                "p50": _percentile(durations, 50),
                "p95": _percentile(durations, 95),
            },
            "trends": [
                {"name": name, "data": [trends[key].get(name, 0) for key in date_keys]}
                for name in trend_names
            ],
        },
    }


def _report_metrics(date_keys: list[str]) -> dict[str, Any]:
    date_set = set(date_keys)
    daily = {key: {"reports": 0} for key in date_keys}
    top_bvids: Counter[str] = Counter()
    video_meta: dict[str, dict[str, str]] = {}
    active_reports = 0
    archived_reports = 0
    for path in REPORT_DIR.glob("*.json") if REPORT_DIR.exists() else []:
        if path.name == "config.json":
            continue
        report = _read_json(path)
        if not report:
            continue
        active_reports += 1
        bvid = str(report.get("bvid") or "")
        if bvid:
            top_bvids[bvid] += 1
            video_info = (report.get("snapshot") or {}).get("video_info") or {}
            if isinstance(video_info, dict):
                title = str(video_info.get("title") or "").strip()
                author = str(video_info.get("author") or "").strip()
                if title or author:
                    video_meta[bvid] = {
                        "title": title,
                        "author": author,
                        "url": f"https://www.bilibili.com/video/{bvid}",
                    }
        date_key = _date_from_iso(report.get("created_at"))
        if date_key in date_set:
            daily[date_key]["reports"] += 1
    if REPORT_ARCHIVE_DIR.exists():
        archived_reports = sum(1 for path in REPORT_ARCHIVE_DIR.glob("*.json") if path.is_file())
    return {
        "daily": daily,
        "top_bvids": top_bvids,
        "video_meta": video_meta,
        "summary": {
            "active_reports": active_reports,
            "archived_reports": archived_reports,
        },
    }


def _inventory_metrics() -> dict[str, Any]:
    downloads = _folder_inventory(DOWNLOAD_DIR)
    csv_files = _folder_inventory(DOWNLOAD_DIR / "CSV")
    txt_files = _folder_inventory(DOWNLOAD_DIR / "TXT")
    reports = _folder_inventory(REPORT_DIR)
    subtitles = _folder_inventory(SUBTITLE_DIR)
    plugin_zip = DOWNLOAD_DIR.parent / "static" / "plugin" / "bili-lite-subtitle-store-upload-1.0.2.zip"
    return {
        "downloads": downloads,
        "csv": csv_files,
        "txt": txt_files,
        "reports": reports,
        "subtitles": subtitles,
        "plugin_zip": {
            "exists": plugin_zip.exists(),
            "size_bytes": plugin_zip.stat().st_size if plugin_zip.exists() else 0,
            "mtime": _mtime_iso(plugin_zip),
        },
    }


def _app_log_errors(date_keys: list[str]) -> dict[str, Any]:
    date_set = set(date_keys)
    daily = {key: {"app_errors": 0} for key in date_keys}
    recent: list[dict[str, str]] = []
    if not LOG_FILE.exists():
        return {"daily": daily, "recent": recent}
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                match = APP_LOG_RE.match(line.strip())
                if not match:
                    continue
                level = match.group("level").upper()
                if level not in {"ERROR", "CRITICAL"}:
                    continue
                date_key = match.group("ts")[:10]
                if date_key not in date_set:
                    continue
                daily[date_key]["app_errors"] += 1
                recent.append({"ts": match.group("ts"), "message": _safe_message(match.group("message"))})
    except OSError:
        pass
    return {"daily": daily, "recent": recent[-12:][::-1]}


def _merge_daily(
    date_keys: list[str],
    access_daily: dict[str, dict[str, Any]],
    artifact_daily: dict[str, dict[str, Any]],
    job_daily: dict[str, dict[str, Any]],
    report_daily: dict[str, dict[str, Any]],
    app_error_daily: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in date_keys:
        row = _empty_daily(key)
        row.update(access_daily.get(key, {}))
        row.update(artifact_daily.get(key, {}))
        row.update(job_daily.get(key, {}))
        row.update(report_daily.get(key, {}))
        row.update(app_error_daily.get(key, {}))
        row["error_rate"] = round((row["errors"] / row["all_hits"] * 100), 2) if row["all_hits"] else 0
        rows.append(row)
    return rows


def _kpis(daily: list[dict[str, Any]], jobs: dict[str, Any], events: dict[str, Any]) -> dict[str, Any]:
    today = daily[-1] if daily else {}
    yesterday = daily[-2] if len(daily) >= 2 else _empty_daily("")
    half = max(1, min(7, len(daily) // 2))
    current_period = daily[-half:]
    previous_period = daily[-(half * 2):-half] if len(daily) >= half * 2 else []
    fields = [
        ("pv", "PV"),
        ("uv", "UV"),
        ("downloads", "文件下载"),
        ("artifact_success", "解析成功"),
        ("api_calls", "API调用"),
        ("analysis_jobs", "AI任务"),
        ("errors", "错误"),
        ("reports", "分享报告"),
    ]
    return {
        "today": today,
        "yesterday": yesterday,
        "day_compare": {
            field: _comparison(today.get(field, 0), yesterday.get(field, 0), label)
            for field, label in fields
        },
        "period_compare": {
            field: _comparison(_sum_field(current_period, field), _sum_field(previous_period, field), label)
            for field, label in fields
        },
        "active_jobs": jobs["summary"]["active"],
        "latency": events["latency"],
    }


def _feature_trends(date_keys: list[str], feature_daily: dict[str, Counter[str]]) -> list[dict[str, Any]]:
    totals = Counter()
    for counts in feature_daily.values():
        totals.update(counts)
    keep = [name for name, _ in totals.most_common(9)]
    priority = ["首页", "结果页", "弹幕解析", "CSV下载", "TXT下载", "弹幕内容分析", "字幕深度分析", "插件页", "插件包下载"]
    ordered = [name for name in priority if name in keep]
    ordered.extend([name for name in keep if name not in ordered])
    return [
        {"name": name, "data": [feature_daily[key].get(name, 0) for key in date_keys]}
        for name in ordered[:9]
    ]


def _top_bvids(*counters: Counter[str], video_meta: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
    merged: Counter[str] = Counter()
    for counter in counters:
        merged.update(counter)
    top_items = merged.most_common(12)
    meta = _video_meta_for_bvids([name for name, _ in top_items], video_meta or {})
    return [
        {
            "name": name,
            "value": value,
            "title": meta.get(name, {}).get("title", ""),
            "author": meta.get(name, {}).get("author", ""),
            "url": meta.get(name, {}).get("url", f"https://www.bilibili.com/video/{name}"),
        }
        for name, value in top_items
    ]


def _api_endpoint_items(counts: Counter[str], errors: Counter[str]) -> list[dict[str, Any]]:
    rows = []
    for name, value in counts.most_common(12):
        error_count = errors.get(name, 0)
        rows.append(
            {
                "name": name,
                "value": value,
                "errors": error_count,
                "error_rate": round(error_count / value * 100, 2) if value else 0,
            }
        )
    return rows


def _counter_items(counter: Counter[str], limit: int | None = None) -> list[dict[str, Any]]:
    return [{"name": name, "value": value} for name, value in counter.most_common(limit)]


def _video_meta_for_bvids(bvids: list[str], seed_meta: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    cached = _read_video_meta_cache()
    result: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for bvid in bvids:
        seed = seed_meta.get(bvid) or {}
        cached_item = cached.get(bvid) or {}
        item = {
            "title": str(seed.get("title") or cached_item.get("title") or "").strip(),
            "author": str(seed.get("author") or cached_item.get("author") or "").strip(),
            "url": f"https://www.bilibili.com/video/{bvid}",
        }
        result[bvid] = item
        if not item["title"] or not item["author"]:
            missing.append(bvid)

    if missing:
        with ThreadPoolExecutor(max_workers=min(4, len(missing))) as executor:
            futures = {executor.submit(_fetch_video_meta, bvid): bvid for bvid in missing}
            for future in as_completed(futures):
                bvid = futures[future]
                try:
                    fetched = future.result()
                except Exception:
                    fetched = {}
                if not fetched:
                    continue
                result[bvid] = {
                    "title": fetched.get("title") or result[bvid].get("title", ""),
                    "author": fetched.get("author") or result[bvid].get("author", ""),
                    "url": f"https://www.bilibili.com/video/{bvid}",
                }
                cached[bvid] = {
                    **result[bvid],
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
        _write_video_meta_cache(cached)
    return result


def _fetch_video_meta(bvid: str) -> dict[str, str]:
    if not BV_RE.fullmatch(bvid or ""):
        return {}
    response = requests.get(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        headers=BILIBILI_HEADERS,
        timeout=(2, 6),
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        return {}
    data = payload.get("data") or {}
    owner = data.get("owner") or {}
    return {
        "title": str(data.get("title") or "").strip(),
        "author": str(owner.get("name") or "").strip(),
    }


def _read_video_meta_cache() -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(OPS_VIDEO_META_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        bvid: value
        for bvid, value in raw.items()
        if isinstance(value, dict) and BV_RE.fullmatch(str(bvid or ""))
    }


def _write_video_meta_cache(cache: dict[str, dict[str, str]]) -> None:
    try:
        OPS_VIDEO_META_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        trimmed = dict(list(cache.items())[-300:])
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(OPS_VIDEO_META_CACHE_FILE.parent),
            delete=False,
        ) as tmp_file:
            json.dump(trimmed, tmp_file, ensure_ascii=False, sort_keys=True)
            tmp_name = tmp_file.name
        os.replace(tmp_name, OPS_VIDEO_META_CACHE_FILE)
        try:
            if os.geteuid() == 0:
                user = pwd.getpwnam("www")
                os.chown(OPS_VIDEO_META_CACHE_FILE, user.pw_uid, user.pw_gid)
            os.chmod(OPS_VIDEO_META_CACHE_FILE, 0o660)
        except OSError:
            pass
    except Exception:
        return


def _read_dashboard_disk_cache(cache_key: str, now: float) -> dict[str, Any] | None:
    try:
        cache = json.loads(OPS_DASHBOARD_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = cache.get(cache_key)
    if not isinstance(entry, dict):
        return None
    cached_at = float(entry.get("cached_at") or 0)
    if now - cached_at >= ANALYTICS_CACHE_SECONDS:
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def _write_dashboard_disk_cache(cache_key: str, dashboard: dict[str, Any]) -> None:
    try:
        OPS_DASHBOARD_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            cache = json.loads(OPS_DASHBOARD_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        cache[cache_key] = {"cached_at": time.time(), "data": dashboard}
        cutoff = time.time() - (ANALYTICS_CACHE_SECONDS * 4)
        cache = {
            key: value
            for key, value in cache.items()
            if isinstance(value, dict) and float(value.get("cached_at") or 0) >= cutoff
        }
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(OPS_DASHBOARD_CACHE_FILE.parent),
            delete=False,
        ) as tmp_file:
            json.dump(cache, tmp_file, ensure_ascii=False, sort_keys=True)
            tmp_name = tmp_file.name
        os.replace(tmp_name, OPS_DASHBOARD_CACHE_FILE)
        try:
            if os.geteuid() == 0:
                user = pwd.getpwnam("www")
                os.chown(OPS_DASHBOARD_CACHE_FILE, user.pw_uid, user.pw_gid)
            os.chmod(OPS_DASHBOARD_CACHE_FILE, 0o660)
        except OSError:
            pass
    except Exception:
        return


def _iter_access_log(path: Path):
    try:
        if not path.exists() or path.stat().st_size > MAX_ACCESS_LOG_BYTES:
            return
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                match = ACCESS_LOG_RE.match(line.strip())
                if not match:
                    continue
                parsed_time = _parse_access_time(match.group("time"))
                if not parsed_time:
                    continue
                method = match.group("method").upper()
                target = match.group("target")
                url = urlparse(target)
                path_value = _normalize_path(url.path or "/")
                query = parse_qs(url.query)
                ua = match.group("ua")
                ip_value = match.group("ip")
                size_text = match.group("size")
                try:
                    size = int(size_text) if size_text != "-" else 0
                except ValueError:
                    size = 0
                yield {
                    "date": parsed_time.date().isoformat(),
                    "method": method,
                    "target": target,
                    "path": path_value,
                    "status": int(match.group("status")),
                    "bytes": size,
                    "ip_hash": _ip_hash(ip_value),
                    "ip_segment": _ip_segment(ip_value),
                    "referer_domain": _referer_domain(match.group("referer")),
                    "user_agent_family": _user_agent_family(ua),
                    "is_bot": _is_bot(ua),
                    "bvid": _extract_bvid_from_parts(path_value, query),
                }
    except OSError:
        return


def _access_log_paths() -> list[Path]:
    candidates = [ACCESS_LOG_FILE]
    rotated = Path(f"{ACCESS_LOG_FILE}.1")
    if rotated.exists():
        candidates.append(rotated)
    return [path for path in candidates if path.exists()]


def _parse_access_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None


def _extract_bvid_from_request(flask_request, target: str) -> str | None:
    bvid = flask_request.values.get("bvid", "")
    if not bvid and flask_request.is_json:
        payload = flask_request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            bvid = str(payload.get("bvid") or "")
    url = urlparse(target)
    return _normalize_bvid(bvid) or _extract_bvid_from_parts(_normalize_path(url.path or "/"), parse_qs(url.query))


def _extract_analysis_id_from_request(flask_request, target: str) -> str | None:
    value = flask_request.values.get("analysis_id", "")
    if not value and flask_request.is_json:
        payload = flask_request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            value = str(payload.get("analysis_id") or "")
    if not value:
        query = parse_qs(urlparse(target).query)
        value = (query.get("analysis_id") or query.get("analysis") or [""])[0]
    value = str(value or "").strip()
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else None


def _extract_bvid_from_parts(path: str, query: dict[str, list[str]]) -> str | None:
    for key in ("bvid", "bv"):
        if query.get(key):
            found = _normalize_bvid(query[key][0])
            if found:
                return found
    found = re.search(BV_RE, path or "")
    return found.group(0) if found else None


def _normalize_bvid(value: str) -> str | None:
    value = str(value or "").strip()
    match = re.search(BV_RE, value)
    return match.group(0) if match else None


def _client_ip(headers, remote_addr: str | None) -> str:
    forwarded_for = headers.get("X-Forwarded-For", "")
    return forwarded_for.split(",", 1)[0].strip() or remote_addr or "unknown"


def _ip_hash(value: str) -> str:
    return hashlib.sha256(f"danmu-ops-v1:{value}".encode("utf-8")).hexdigest()[:16]


def _ip_segment(value: str) -> str:
    value = str(value or "").strip()
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return "unknown"
    prefix = 24 if ip.version == 4 else 48
    network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    return f"{network.network_address}/{prefix}"


def _referer_domain(value: str) -> str:
    value = str(value or "").strip()
    if not value or value == "-":
        return "直接访问"
    try:
        parsed = urlparse(value)
    except Exception:
        return "其他来源"
    host = (parsed.hostname or "").lower()
    if not host:
        return "直接访问"
    if host in INTERNAL_HOSTS:
        return "站内跳转"
    return host


def _user_agent_family(value: str) -> str:
    ua = str(value or "")
    lowered = ua.lower()
    if _is_bot(ua):
        return "Bot/脚本"
    if "micromessenger" in lowered:
        return "微信"
    if "edg/" in lowered:
        return "Edge"
    if "firefox/" in lowered:
        return "Firefox"
    if "chrome/" in lowered or "chromium/" in lowered:
        return "Chrome"
    if "safari/" in lowered:
        return "Safari"
    if "iphone" in lowered or "ipad" in lowered:
        return "iOS浏览器"
    if "android" in lowered:
        return "Android浏览器"
    return "其他浏览器"


def _is_bot(value: str) -> bool:
    return bool(BOT_RE.search(str(value or "")))


def _normalize_path(value: str) -> str:
    path = str(value or "/").split("?", 1)[0].strip() or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path


def _date_keys(days: int) -> list[str]:
    end = datetime.now().astimezone().date()
    start = end - timedelta(days=days - 1)
    return [(start + timedelta(days=index)).isoformat() for index in range(days)]


def _date_keys_for_range(start_value: str | None, end_value: str | None, days: int) -> list[str]:
    today = datetime.now().astimezone().date()
    start = _parse_date_key(start_value)
    end = _parse_date_key(end_value)
    if start or end:
        end = end or today
        start = start or (end - timedelta(days=_clamp_days(days) - 1))
        if start > end:
            start, end = end, start
        if (end - start).days >= 180:
            start = end - timedelta(days=179)
        total_days = (end - start).days + 1
        return [(start + timedelta(days=index)).isoformat() for index in range(total_days)]
    return _date_keys(_clamp_days(days))


def _empty_daily(date_key: str) -> dict[str, Any]:
    return {
        "date": date_key,
        "pv": 0,
        "uv": 0,
        "all_hits": 0,
        "bot_hits": 0,
        "api_calls": 0,
        "downloads": 0,
        "plugin_downloads": 0,
        "errors": 0,
        "bytes": 0,
        "artifact_success": 0,
        "danmaku_lines": 0,
        "unique_bvids": 0,
        "subtitle_attached": 0,
        "analysis_jobs": 0,
        "job_succeeded": 0,
        "job_failed": 0,
        "reports": 0,
        "app_errors": 0,
        "error_rate": 0,
    }


def _date_from_iso(value: Any) -> str:
    parsed = _parse_iso(value)
    return parsed.date().isoformat() if parsed else ""


def _parse_iso(value: Any) -> datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date_key(value: Any):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 2)
    rank = (len(ordered) - 1) * (percentile / 100)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(float(ordered[lower] * (1 - weight) + ordered[upper] * weight), 2)


def _comparison(current: int | float, previous: int | float, label: str) -> dict[str, Any]:
    current = current or 0
    previous = previous or 0
    diff = current - previous
    pct = round(diff / previous * 100, 1) if previous else (100 if current else 0)
    return {"label": label, "current": current, "previous": previous, "diff": diff, "pct": pct}


def _sum_field(rows: list[dict[str, Any]], field: str) -> int:
    return int(sum(row.get(field, 0) or 0 for row in rows))


def _clamp_days(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 30
    return min(180, max(1, parsed))


def _folder_inventory(path: Path) -> dict[str, Any]:
    count = 0
    size = 0
    newest = ""
    if not path.exists():
        return {"count": 0, "size_bytes": 0, "newest_mtime": ""}
    for root, _, files in os.walk(path):
        for filename in files:
            file_path = Path(root) / filename
            try:
                stat = file_path.stat()
            except OSError:
                continue
            count += 1
            size += stat.st_size
            mtime = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
            newest = max(newest, mtime)
    return {"count": count, "size_bytes": size, "newest_mtime": newest}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _file_freshness(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "mtime": _mtime_iso(path),
    }


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return ""


def _safe_message(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"https?://([^/\s]+)/", r"https://\1/", text)
    return text[:180]


def _job_kind_label(value: str) -> str:
    return {
        "content_analysis": "弹幕内容分析",
        "deep_analysis": "字幕深度分析",
    }.get(value, value or "未知任务")


def _job_status_label(value: str) -> str:
    return {
        "queued": "排队中",
        "running": "运行中",
        "succeeded": "成功",
        "failed": "失败",
        "cancelled": "取消",
    }.get(value, value or "未知")
