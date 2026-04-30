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
AI_BOT_RE = re.compile(
    r"gptbot|chatgpt|oai-searchbot|claudebot|anthropic|perplexitybot|bytespider|ccbot|google-extended|"
    r"applebot-extended|amazonbot|meta-externalagent|diffbot|youbot|omgili",
    re.IGNORECASE,
)
SEARCH_BOT_RE = re.compile(
    r"googlebot|bingbot|baiduspider|sogou|360spider|yisouspider|duckduckbot|yandexbot|petalbot|slurp",
    re.IGNORECASE,
)
TOOL_BOT_RE = re.compile(
    r"curl|wget|python-requests|httpclient|headless|go-http-client|okhttp|postman|node-fetch",
    re.IGNORECASE,
)
AI_BOT_TOKENS = (
    "gptbot",
    "chatgpt",
    "oai-searchbot",
    "claudebot",
    "anthropic",
    "perplexitybot",
    "bytespider",
    "ccbot",
    "google-extended",
    "applebot-extended",
    "amazonbot",
    "meta-externalagent",
    "diffbot",
    "youbot",
    "omgili",
)
SEARCH_BOT_TOKENS = (
    "googlebot",
    "bingbot",
    "baiduspider",
    "sogou",
    "360spider",
    "yisouspider",
    "duckduckbot",
    "yandexbot",
    "petalbot",
    "slurp",
)
TOOL_BOT_TOKENS = (
    "curl",
    "wget",
    "python-requests",
    "httpclient",
    "headless",
    "go-http-client",
    "okhttp",
    "postman",
    "node-fetch",
)
GENERIC_BOT_TOKENS = ("bot", "spider", "crawl", "bingpreview")
ALL_BOT_TOKENS = AI_BOT_TOKENS + SEARCH_BOT_TOKENS + TOOL_BOT_TOKENS + GENERIC_BOT_TOKENS
BOT_SOURCE_RULES = (
    ("AI Bot", "OpenAI GPTBot", ("gptbot",)),
    ("AI Bot", "OpenAI ChatGPT", ("chatgpt-user", "chatgpt")),
    ("AI Bot", "OpenAI SearchBot", ("oai-searchbot",)),
    ("AI Bot", "Anthropic ClaudeBot", ("claudebot", "anthropic")),
    ("AI Bot", "PerplexityBot", ("perplexitybot",)),
    ("AI Bot", "ByteDance Bytespider", ("bytespider",)),
    ("AI Bot", "Common Crawl CCBot", ("ccbot",)),
    ("AI Bot", "Google-Extended", ("google-extended",)),
    ("AI Bot", "Applebot-Extended", ("applebot-extended",)),
    ("AI Bot", "Amazonbot", ("amazonbot",)),
    ("AI Bot", "Meta External Agent", ("meta-externalagent",)),
    ("AI Bot", "Diffbot", ("diffbot",)),
    ("AI Bot", "YouBot", ("youbot",)),
    ("AI Bot", "Omgili", ("omgili",)),
    ("搜索引擎蜘蛛", "Googlebot", ("googlebot",)),
    ("搜索引擎蜘蛛", "Bingbot", ("bingbot", "bingpreview")),
    ("搜索引擎蜘蛛", "Baiduspider", ("baiduspider",)),
    ("搜索引擎蜘蛛", "Sogou Spider", ("sogou",)),
    ("搜索引擎蜘蛛", "360Spider", ("360spider",)),
    ("搜索引擎蜘蛛", "YisouSpider", ("yisouspider",)),
    ("搜索引擎蜘蛛", "DuckDuckBot", ("duckduckbot",)),
    ("搜索引擎蜘蛛", "YandexBot", ("yandexbot",)),
    ("搜索引擎蜘蛛", "PetalBot", ("petalbot",)),
    ("搜索引擎蜘蛛", "Yahoo Slurp", ("slurp",)),
    ("工具/脚本", "curl/wget", ("curl", "wget")),
    ("工具/脚本", "Python requests", ("python-requests",)),
    ("工具/脚本", "Go HTTP Client", ("go-http-client",)),
    ("工具/脚本", "Headless Browser", ("headless",)),
    ("工具/脚本", "HTTP Client", ("httpclient", "okhttp", "postman", "node-fetch")),
    ("其他 Bot", "其他 Bot", GENERIC_BOT_TOKENS),
)
INTERNAL_HOSTS = {"danmu.liu-qi.cn", "dm.liu-qi.cn"}
ANALYTICS_CACHE_SECONDS = 900
VIDEO_META_FETCH_LIMIT = 6
MAX_ACCESS_LOG_BYTES = 80 * 1024 * 1024
OPS_VIDEO_META_CACHE_FILE = OPS_DASHBOARD_CACHE_FILE.with_name("ops_video_meta.json")
OPS_IP_REGION_CACHE_FILE = OPS_DASHBOARD_CACHE_FILE.with_name("ops_ip_region_cache.json")
ACCESS_LOG_MONTHS = {
    "Jan": "01",
    "Feb": "02",
    "Mar": "03",
    "Apr": "04",
    "May": "05",
    "Jun": "06",
    "Jul": "07",
    "Aug": "08",
    "Sep": "09",
    "Oct": "10",
    "Nov": "11",
    "Dec": "12",
}

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
    "job_poll": "AI任务状态接口",
    "video_cover": "视频封面接口",
    "api_other": "其他 API",
    "ops_event": "运营埋点",
    "not_found": "异常访问",
    "seo_file": "SEO文件",
    "static_asset": "静态资源",
    "other": "其他",
}

CLICK_EVENT_LABELS = {
    "parse_danmaku_click": "解析弹幕按钮",
    "csv_download_click": "下载 CSV",
    "txt_download_click": "下载 TXT",
    "plugin_github_click": "插件 GitHub 链接",
    "plugin_download_click": "下载插件包",
    "custom_api_key_click": "获取 API Key",
    "custom_api_test_click": "自主模型测试",
    "custom_api_save_click": "自主模型保存",
    "custom_api_clear_click": "自主模型清空",
    "builtin_content_analysis_click": "内置内容分析",
    "builtin_deep_analysis_click": "内置深度分析",
    "custom_content_analysis_click": "自主内容分析",
    "custom_deep_analysis_click": "自主深度分析",
    "subtitle_upload_button_click": "上传字幕按钮",
    "subtitle_file_selected": "选择字幕文件",
    "share_report_click": "分享报告按钮",
    "share_copy_link_click": "复制分享链接",
    "share_card_download_click": "下载分享卡片",
}

AI_MODE_LABELS = {
    "content_analysis": ("内置 AI", "内置内容分析", "builtin_ai_calls"),
    "deep_analysis": ("内置 AI", "内置深度分析", "builtin_ai_calls"),
    "custom_content": ("自主模型", "自主内容分析", "custom_ai_calls"),
    "custom_deep": ("自主模型", "自主深度分析", "custom_ai_calls"),
}
OPERATING_FEATURE_CATEGORIES = {
    "download_generate",
    "download_csv",
    "download_txt",
    "subtitle_upload",
    "content_analysis",
    "deep_analysis",
    "custom_content",
    "custom_deep",
    "report_save",
    "report_view",
}
IP_REGION_LABEL_BLACKLIST = {"未知地区", "待归类地区", "其他地区"}
COUNTRY_LABELS = {
    "CN": "中国",
    "US": "美国",
    "JP": "日本",
    "KR": "韩国",
    "SG": "新加坡",
    "HK": "中国香港",
    "TW": "中国台湾",
}
REGION_LABELS = {
    "Beijing": "北京",
    "Shanghai": "上海",
    "Tianjin": "天津",
    "Chongqing": "重庆",
    "Guangdong": "广东",
    "Zhejiang": "浙江",
    "Jiangsu": "江苏",
    "Shandong": "山东",
    "Henan": "河南",
    "Sichuan": "四川",
    "Hubei": "湖北",
    "Hunan": "湖南",
    "Fujian": "福建",
    "Shaanxi": "陕西",
    "Shanxi": "山西",
    "Hebei": "河北",
    "Liaoning": "辽宁",
    "Jilin": "吉林",
    "Heilongjiang": "黑龙江",
    "Anhui": "安徽",
    "Jiangxi": "江西",
    "Guangxi": "广西",
    "Yunnan": "云南",
    "Guizhou": "贵州",
    "Gansu": "甘肃",
    "Inner Mongolia": "内蒙古",
    "Xinjiang": "新疆",
    "Ningxia": "宁夏",
    "Qinghai": "青海",
    "Hainan": "海南",
    "Hangzhou": "杭州",
    "Chengdu": "成都",
    "Luancheng": "石家庄栾城",
    "Guangzhou": "广州",
    "Shenzhen": "深圳",
    "Nanjing": "南京",
    "Suzhou": "苏州",
    "Wuhan": "武汉",
    "Xi'an": "西安",
    "Xian": "西安",
    "Changsha": "长沙",
    "Shaoxing": "绍兴",
    "Shijiazhuang": "石家庄",
    "Haikou": "海口",
    "Nanning": "南宁",
    "Zhangjiakou": "张家口",
    "Qingdao": "青岛",
    "Jinan": "济南",
    "Shenyang": "沈阳",
    "Virginia": "弗吉尼亚",
    "Ashburn": "阿什本",
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
SKIP_ANALYTICS_CATEGORIES = {"dashboard", "ops_api", "ops_event", "static_asset", "health", "logs"}

_dashboard_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_dashboard_cache_lock = threading.Lock()
_analytics_db_ready = False
_analytics_db_lock = threading.Lock()


def record_client_event(flask_request, event_name: str, metadata: dict[str, Any] | None = None) -> bool:
    event_name = str(event_name or "").strip()
    if event_name not in CLICK_EVENT_LABELS:
        return False

    _ensure_analytics_db()
    metadata = metadata if isinstance(metadata, dict) else {}
    ip_value = _client_ip(flask_request.headers, flask_request.remote_addr)
    now = datetime.now().astimezone()
    ua = flask_request.headers.get("User-Agent", "")
    bvid = _normalize_bvid(str(metadata.get("bvid") or ""))
    analysis_id = str(metadata.get("analysis_id") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", analysis_id):
        analysis_id = None
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
                "EVENT",
                f"/ops-event/{event_name}",
                f"click_{event_name}",
                200,
                None,
                _ip_hash(ip_value),
                _ip_segment(ip_value),
                _user_agent_family(ua),
                1 if _is_bot(ua) else 0,
                _referer_domain(flask_request.headers.get("Referer", "")),
                bvid,
                analysis_id,
            ),
        )
    return True


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


def build_ops_dashboard(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    exclude_ips: str | list[str] | None = None,
) -> dict[str, Any]:
    date_keys = _date_keys_for_range(start_date, end_date, days)
    days = len(date_keys)
    excluded_ips = _normalize_excluded_ips(exclude_ips)
    excluded_ip_hashes = {_ip_hash(ip_value) for ip_value in excluded_ips}
    filter_key = ",".join(sorted(excluded_ip_hashes)) or "none"
    cache_key = f"ops:{date_keys[0]}:{date_keys[-1]}:exclude:{filter_key}"
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

    access = _access_log_metrics(date_keys, excluded_ips)
    events = _analytics_event_metrics(date_keys, excluded_ip_hashes)
    excluded_analysis_ids = events["excluded_analysis_ids"]
    artifacts = _artifact_metrics(date_keys, excluded_analysis_ids)
    jobs = _job_metrics(date_keys, excluded_analysis_ids)
    reports = _report_metrics(date_keys, excluded_analysis_ids)

    daily = _merge_daily(
        date_keys,
        access["daily"],
        events["daily"],
        artifacts["daily"],
        jobs["daily"],
        reports["daily"],
        {},
    )
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
            ],
            "access_log": _file_freshness(ACCESS_LOG_FILE),
            "admin_filter": {
                "enabled": bool(excluded_ips),
                "ips": sorted(excluded_ips),
            },
            "notes": [
                "PV/UV 默认排除明显爬虫与命令行探测流量。",
                "可按管理员 IP 过滤访问日志、按钮埋点以及可关联的解析/任务/报告数据。",
                "地区来源基于聚合网段做近似归类，不返回完整原始 IP。",
                "按钮点击从前端埋点上线后开始累计。",
            ],
        },
        "kpis": _kpis(daily, jobs, events),
        "daily": daily,
        "feature_trends": _feature_trends(date_keys, access["feature_daily"]),
        "click_actions": events["click_actions"],
        "click_trends": events["click_trends"],
        "ai_mode_breakdown": _counter_items(access["ai_modes"]),
        "ai_mode_details": _counter_items(access["ai_mode_details"]),
        "ai_mode_trends": _counter_trends(date_keys, access["ai_mode_daily"], ["内置 AI", "自主模型"]),
        "ai_analysis_breakdown": _counter_items(access["ai_mode_details"]),
        "ai_analysis_trends": _counter_trends(
            date_keys,
            access["ai_detail_daily"],
            ["内置内容分析", "自主内容分析", "内置深度分析", "自主深度分析"],
        ),
        "download_breakdown": _counter_items(access["download_breakdown"]),
        "status_codes": _counter_items(access["status_codes"]),
        "api_endpoints": _api_endpoint_items(access["api_endpoints"], access["api_endpoint_errors"]),
        "top_pages": _counter_items(access["top_pages"], 10),
        "top_referrers": _counter_items(access["top_referrers"], 10),
        "top_regions": access["top_regions"],
        "top_user_agents": _counter_items(access["top_user_agents"], 8),
        "bot_traffic": access["bot_traffic"],
        "top_bvids": _top_bvids(
            access["top_bvids"],
            artifacts["top_bvids"],
            reports["top_bvids"],
            video_meta=reports["video_meta"],
        ),
        "ai_bvids": _ai_bvid_items(jobs["ai_bvids"], video_meta=reports["video_meta"]),
        "jobs": jobs["summary"],
        "artifacts": artifacts["summary"],
        "reports": reports["summary"],
        "latency": events["latency"],
        "recent_errors": access["recent_errors"],
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
    if path == "/api/v2/ops-events":
        return "ops_event", FEATURE_LABELS["ops_event"]
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


def _access_log_metrics(date_keys: list[str], excluded_ips: set[str] | None = None) -> dict[str, Any]:
    excluded_ips = excluded_ips or set()
    daily = {key: _empty_daily(key) for key in date_keys}
    date_set = set(date_keys)
    visitors: dict[str, set[str]] = defaultdict(set)
    feature_daily: dict[str, Counter[str]] = defaultdict(Counter)
    top_pages: Counter[str] = Counter()
    top_referrers: Counter[str] = Counter()
    region_visitors: dict[str, set[str]] = defaultdict(set)
    top_user_agents: Counter[str] = Counter()
    top_bvids: Counter[str] = Counter()
    download_breakdown: Counter[str] = Counter()
    status_codes: Counter[str] = Counter()
    api_endpoints: Counter[str] = Counter()
    api_endpoint_errors: Counter[str] = Counter()
    ai_modes: Counter[str] = Counter()
    ai_mode_details: Counter[str] = Counter()
    ai_mode_daily: dict[str, Counter[str]] = defaultdict(Counter)
    ai_detail_daily: dict[str, Counter[str]] = defaultdict(Counter)
    bot_families: Counter[str] = Counter()
    bot_sources: Counter[str] = Counter()
    bot_paths: Counter[str] = Counter()
    bot_daily: dict[str, Counter[str]] = defaultdict(Counter)
    bot_source_daily: dict[str, Counter[str]] = defaultdict(Counter)
    recent_errors: list[dict[str, str]] = []

    for path in _access_log_paths():
        for row in _iter_access_log(path):
            date_key = row["date"]
            if date_key not in date_set:
                continue
            if row["ip"] in excluded_ips:
                continue
            category, label = classify_request(row["method"], row["path"])
            day = daily[date_key]
            is_bot = row["is_bot"]
            status = row["status"]
            day["all_hits"] += 1
            day["bytes"] += row["bytes"]
            status_codes[str(status)] += 1
            if is_bot:
                bot_family = row["bot_family"]
                bot_source = row["bot_source"]
                day["bot_hits"] += 1
                bot_families[bot_family] += 1
                bot_sources[bot_source] += 1
                bot_daily[date_key][bot_family] += 1
                bot_source_daily[date_key][bot_source] += 1
                bot_paths[f"{bot_source} · {label}"] += 1
            if status >= 400:
                day["errors"] += 1
                recent_errors.append(
                    {
                        "ts": str(row.get("ts") or date_key),
                        "message": _safe_message(f"{status} {row['method']} {row['path']} · {label}"),
                    }
                )
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
                if category in OPERATING_FEATURE_CATEGORIES and status < 500:
                    if category in AI_MODE_LABELS:
                        _, detail, _ = AI_MODE_LABELS[category]
                        feature_daily[date_key][detail] += 1
                    else:
                        feature_daily[date_key][label] += 1
                if category in AI_MODE_LABELS and status < 500:
                    mode, detail, field = AI_MODE_LABELS[category]
                    day["ai_calls"] += 1
                    day[field] += 1
                    ai_modes[mode] += 1
                    ai_mode_details[detail] += 1
                    ai_mode_daily[date_key][mode] += 1
                    ai_detail_daily[date_key][detail] += 1
                if category in PAGE_CATEGORIES and row["method"] == "GET" and status < 500:
                    day["pv"] += 1
                    visitor_key = _ip_hash(row["ip"])
                    visitors[date_key].add(visitor_key)
                    region_visitors[_ip_segment(row["ip"])].add(visitor_key)
                    top_pages[label] += 1
                if category in PAGE_CATEGORIES:
                    top_user_agents[_user_agent_family(row["ua"])] += 1
                referer_domain = _referer_domain(row["referer"])
                if referer_domain:
                    top_referrers[referer_domain] += 1
                if row["bvid"]:
                    top_bvids[row["bvid"]] += 1

    for key, unique_ips in visitors.items():
        daily[key]["uv"] = len(unique_ips)
    return {
        "daily": daily,
        "feature_daily": feature_daily,
        "top_pages": top_pages,
        "top_referrers": top_referrers,
        "top_ip_segments": Counter({segment: len(values) for segment, values in region_visitors.items()}),
        "top_user_agents": top_user_agents,
        "top_bvids": top_bvids,
        "download_breakdown": download_breakdown,
        "status_codes": status_codes,
        "api_endpoints": api_endpoints,
        "api_endpoint_errors": api_endpoint_errors,
        "ai_modes": ai_modes,
        "ai_mode_details": ai_mode_details,
        "ai_mode_daily": ai_mode_daily,
        "ai_detail_daily": ai_detail_daily,
        "top_regions": _region_items_from_segment_visitors(region_visitors, 16),
        "bot_traffic": {
            "families": _counter_items(bot_families, 8),
            "summary": _counter_items(bot_sources, 12),
            "trends": _counter_trends(
                date_keys,
                bot_source_daily,
                ["Googlebot", "Bingbot", "Baiduspider", "Sogou Spider", "OpenAI GPTBot", "Anthropic ClaudeBot", "ByteDance Bytespider"],
            ),
            "family_trends": _counter_trends(date_keys, bot_daily, ["AI Bot", "搜索引擎蜘蛛", "工具/脚本", "其他 Bot"]),
            "paths": _counter_items(bot_paths, 10),
        },
        "recent_errors": sorted(recent_errors, key=lambda item: item["ts"], reverse=True)[:12],
    }


def _analytics_event_metrics(date_keys: list[str], excluded_ip_hashes: set[str] | None = None) -> dict[str, Any]:
    _ensure_analytics_db()
    excluded_ip_hashes = excluded_ip_hashes or set()
    date_set = set(date_keys)
    daily = {key: {"button_clicks": 0} for key in date_keys}
    durations: list[float] = []
    durations_by_category: dict[str, list[float]] = defaultdict(list)
    clicks: Counter[str] = Counter()
    click_daily: dict[str, Counter[str]] = defaultdict(Counter)
    event_count = 0
    excluded_analysis_ids: set[str] = set()
    with connect_state_db(STATE_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT date, category, status, duration_ms, ip_hash, analysis_id
            FROM analytics_events
            WHERE date >= ? AND date <= ?
              AND is_bot = 0
            """,
            (date_keys[0], date_keys[-1]),
        ).fetchall()
    for row in rows:
        if row["date"] not in date_set:
            continue
        if row["ip_hash"] in excluded_ip_hashes:
            analysis_id = str(row["analysis_id"] or "").strip()
            if re.fullmatch(r"[0-9a-f]{32}", analysis_id):
                excluded_analysis_ids.add(analysis_id)
            continue
        category = str(row["category"] or "")
        if category.startswith("click_"):
            label = _click_event_label(category)
            clicks[label] += 1
            click_daily[row["date"]][label] += 1
            daily[row["date"]]["button_clicks"] += 1
            continue
        value = float(row["duration_ms"] or 0)
        if value > 0:
            event_count += 1
            durations.append(value)
            label = FEATURE_LABELS.get(category, category)
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
        "daily": daily,
        "click_actions": _counter_items(clicks, 16),
        "click_trends": _counter_trends(
            date_keys,
            click_daily,
            [
                "解析弹幕按钮",
                "下载 CSV",
                "下载 TXT",
                "内置内容分析",
                "自主内容分析",
                "内置深度分析",
                "自主深度分析",
                "分享报告按钮",
                "插件 GitHub 链接",
                "获取 API Key",
            ],
        ),
        "latency": {
            "events": event_count,
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
            "by_category": by_category,
        },
        "excluded_analysis_ids": excluded_analysis_ids,
    }


def _artifact_metrics(date_keys: list[str], excluded_analysis_ids: set[str] | None = None) -> dict[str, Any]:
    _ensure_analytics_db()
    excluded_analysis_ids = excluded_analysis_ids or set()
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
            SELECT analysis_id, bvid, count, created_at, subtitle_filename
            FROM artifact_records
            """
        ).fetchall()
    for row in rows:
        if str(row["analysis_id"] or "") in excluded_analysis_ids:
            continue
        date_key = _date_from_iso(row["created_at"])
        if date_key not in date_set:
            continue
        total_records += 1
        count = int(row["count"] or 0)
        total_danmaku += count
        bvid = str(row["bvid"] or "")
        if bvid:
            top_bvids[bvid] += 1
        if row["subtitle_filename"]:
            subtitle_records += 1
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


def _job_metrics(date_keys: list[str], excluded_analysis_ids: set[str] | None = None) -> dict[str, Any]:
    _ensure_analytics_db()
    excluded_analysis_ids = excluded_analysis_ids or set()
    date_set = set(date_keys)
    daily = {key: {"analysis_jobs": 0, "job_succeeded": 0, "job_failed": 0} for key in date_keys}
    by_kind_status: Counter[str] = Counter()
    by_model_status: Counter[tuple[str, str, str]] = Counter()
    ai_bvids: dict[str, dict[str, Any]] = {}
    active = {"queued": 0, "running": 0}
    durations: list[float] = []
    trends: dict[str, Counter[str]] = defaultdict(Counter)
    with connect_state_db(STATE_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT job_id, kind, status, payload_json, created_at, started_at, finished_at
            FROM jobs
            """
        ).fetchall()
        event_rows = conn.execute(
            """
            SELECT e.job_id, e.type, e.message
            FROM job_events e
            JOIN jobs j ON j.job_id = e.job_id
            WHERE j.created_at >= ? AND j.created_at <= ?
            ORDER BY e.id ASC
            """,
            (f"{date_keys[0]}T00:00:00", f"{date_keys[-1]}T23:59:59"),
        ).fetchall()
    events_by_job: dict[str, list[dict[str, str]]] = defaultdict(list)
    for event in event_rows:
        events_by_job[str(event["job_id"])].append(
            {"type": str(event["type"] or ""), "message": str(event["message"] or "")}
        )
    for row in rows:
        payload = _json_object(row["payload_json"])
        if str(payload.get("analysis_id") or "") in excluded_analysis_ids:
            continue
        kind = str(row["kind"] or "unknown")
        status = str(row["status"] or "unknown")
        date_key = _date_from_iso(row["created_at"])
        label = f"{_job_kind_label(kind)} / {_job_status_label(status)}"
        if date_key in date_set:
            if status in active:
                active[status] += 1
            by_kind_status[f"{kind}:{status}"] += 1
            model_label, model_outcome = _job_model_outcome(status, events_by_job.get(str(row["job_id"])) or [])
            by_model_status[(_job_kind_label(kind), model_label, model_outcome)] += 1
            bvid = _normalize_bvid(str(payload.get("bvid") or ""))
            if bvid:
                ai_item = ai_bvids.setdefault(
                    bvid,
                    {
                        "value": 0,
                        "content": 0,
                        "deep": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "latest_at": "",
                    },
                )
                ai_item["value"] += 1
                if kind == "content_analysis":
                    ai_item["content"] += 1
                elif kind == "deep_analysis":
                    ai_item["deep"] += 1
                if status == "succeeded":
                    ai_item["succeeded"] += 1
                elif status == "failed":
                    ai_item["failed"] += 1
                ai_item["latest_at"] = max(str(ai_item.get("latest_at") or ""), str(row["created_at"] or ""))
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
        "ai_bvids": ai_bvids,
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
            "model_status": [
                {
                    "kind": kind,
                    "model": model,
                    "status": status,
                    "count": count,
                }
                for (kind, model, status), count in sorted(
                    by_model_status.items(),
                    key=lambda item: (item[0][0], item[0][2], item[0][1]),
                )
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


def _report_metrics(date_keys: list[str], excluded_analysis_ids: set[str] | None = None) -> dict[str, Any]:
    excluded_analysis_ids = excluded_analysis_ids or set()
    date_set = set(date_keys)
    daily = {key: {"reports": 0} for key in date_keys}
    top_bvids: Counter[str] = Counter()
    video_meta: dict[str, dict[str, str]] = {}
    active_reports = 0
    archived_reports = 0

    def collect_report(report: dict[str, Any] | None, archived: bool = False) -> None:
        nonlocal active_reports, archived_reports
        if not report:
            return
        if str(report.get("analysis_id") or "") in excluded_analysis_ids:
            return
        date_key = _date_from_iso(report.get("created_at"))
        if date_key not in date_set:
            return
        if archived:
            archived_reports += 1
        else:
            active_reports += 1
        daily[date_key]["reports"] += 1
        bvid = str(report.get("bvid") or "")
        if not bvid:
            return
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

    for path in REPORT_DIR.glob("*.json") if REPORT_DIR.exists() else []:
        if path.name == "config.json":
            continue
        collect_report(_read_json(path), False)
    if REPORT_ARCHIVE_DIR.exists():
        for path in REPORT_ARCHIVE_DIR.glob("*.json"):
            if path.is_file():
                collect_report(_read_json(path), True)
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
    event_daily: dict[str, dict[str, Any]],
    artifact_daily: dict[str, dict[str, Any]],
    job_daily: dict[str, dict[str, Any]],
    report_daily: dict[str, dict[str, Any]],
    app_error_daily: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in date_keys:
        row = _empty_daily(key)
        row.update(access_daily.get(key, {}))
        row.update(event_daily.get(key, {}))
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
        ("artifact_success", "解析成功"),
        ("button_clicks", "按钮点击"),
        ("builtin_ai_calls", "内置AI"),
        ("custom_ai_calls", "自主模型"),
        ("analysis_jobs", "AI任务"),
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
    priority = [
        "弹幕解析",
        "CSV下载",
        "TXT下载",
        "内置内容分析",
        "自主内容分析",
        "内置深度分析",
        "自主深度分析",
        "分享报告保存",
        "分享报告读取",
        "字幕上传",
    ]
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


def _ai_bvid_items(
    ai_bvids: dict[str, dict[str, Any]],
    video_meta: dict[str, dict[str, str]] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    top_items = sorted(
        ai_bvids.items(),
        key=lambda item: (int(item[1].get("value") or 0), str(item[1].get("latest_at") or "")),
        reverse=True,
    )[:limit]
    meta = _video_meta_for_bvids([name for name, _ in top_items], video_meta or {})
    rows: list[dict[str, Any]] = []
    for bvid, stats in top_items:
        rows.append(
            {
                "name": bvid,
                "value": int(stats.get("value") or 0),
                "content": int(stats.get("content") or 0),
                "deep": int(stats.get("deep") or 0),
                "succeeded": int(stats.get("succeeded") or 0),
                "failed": int(stats.get("failed") or 0),
                "latest_at": str(stats.get("latest_at") or ""),
                "title": meta.get(bvid, {}).get("title", ""),
                "author": meta.get(bvid, {}).get("author", ""),
                "url": meta.get(bvid, {}).get("url", f"https://www.bilibili.com/video/{bvid}"),
            }
        )
    return rows


def _api_endpoint_items(counts: Counter[str], errors: Counter[str]) -> list[dict[str, Any]]:
    rows = []
    for name, value in counts.most_common(12):
        error_count = errors.get(name, 0)
        if error_count <= 0:
            continue
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


def _counter_trends(
    date_keys: list[str],
    daily_counters: dict[str, Counter[str]],
    priority: list[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    totals: Counter[str] = Counter()
    for counts in daily_counters.values():
        totals.update(counts)
    names = [name for name, _ in totals.most_common(limit)]
    ordered = [name for name in (priority or []) if name in totals]
    ordered.extend([name for name in names if name not in ordered])
    return [
        {"name": name, "data": [daily_counters[key].get(name, 0) for key in date_keys]}
        for name in ordered[:limit]
    ]


def _click_event_label(category: str) -> str:
    name = str(category or "")
    if name.startswith("click_"):
        name = name[6:]
    return CLICK_EVENT_LABELS.get(name, name or "未知按钮")


def _normalize_excluded_ips(value: str | list[str] | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw_items = re.split(r"[\s,;，；]+", value)
    else:
        raw_items = []
        for item in value:
            raw_items.extend(re.split(r"[\s,;，；]+", str(item or "")))
    result: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            result.add(str(ipaddress.ip_address(text)))
        except ValueError:
            continue
    return result


def _json_object(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _job_model_outcome(status: str, events: list[dict[str, str]]) -> tuple[str, str]:
    status = str(status or "unknown")
    messages = [str(event.get("message") or "") for event in events]
    attempts: list[str] = []
    successes: list[str] = []
    failures: list[str] = []
    had_fallback = False
    for message in messages:
        if "切换备用通道" in message or "备用通道" in message or "fallback" in message.lower():
            had_fallback = True
        model = _extract_model_label(message)
        if not model:
            continue
        if model not in attempts and ("正在调用" in message or "调用" in message):
            attempts.append(model)
        if "成功" in message or "已返回结果" in message:
            successes.append(model)
        if "失败" in message or "错误" in message or "超时" in message:
            failures.append(model)
            had_fallback = True
    model_label = successes[-1] if successes else (attempts[-1] if attempts else (failures[-1] if failures else "未记录模型"))
    if len(attempts) > 1 and successes:
        model_label = f"{attempts[0]} → {successes[-1]}"
        had_fallback = True
    elif len(attempts) > 1 and not successes:
        model_label = " → ".join(attempts[-3:])
        had_fallback = True

    if status == "succeeded":
        return model_label, "兜底后成功" if had_fallback else "直接成功"
    if status == "failed":
        return model_label, "兜底后仍失败" if had_fallback else "失败"
    if status == "running":
        return model_label, "运行中"
    if status == "queued":
        return model_label, "排队中"
    return model_label, _job_status_label(status)


def _extract_model_label(message: str) -> str:
    text = str(message or "")
    match = re.search(r"文本分析服务(?:调用)?(?:成功|失败)?[:：]\s*([^/\s]+)\s*/\s*([^\s，,）)]+)", text)
    if match:
        return f"{match.group(1)} / {match.group(2)}"
    match = re.search(r"模型[:：]\s*([^\s，,）)]+)", text)
    if match:
        return match.group(1)
    return ""


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

    missing = missing[:VIDEO_META_FETCH_LIMIT]
    if missing:
        with ThreadPoolExecutor(max_workers=min(6, len(missing))) as executor:
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
        timeout=(0.8, 2.2),
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
                date_key = parsed_time.date().isoformat()
                method = match.group("method").upper()
                target = match.group("target")
                path_value, query_string = _target_path_query(target)
                ua = match.group("ua")
                ip_value = match.group("ip")
                bot_family, bot_source = _bot_info(ua)
                size_text = match.group("size")
                try:
                    size = int(size_text) if size_text != "-" else 0
                except ValueError:
                    size = 0
                yield {
                    "date": date_key,
                    "ts": parsed_time.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    "method": method,
                    "target": target,
                    "path": path_value,
                    "status": int(match.group("status")),
                    "bytes": size,
                    "ip": ip_value,
                    "referer": match.group("referer"),
                    "ua": ua,
                    "is_bot": bot_family != "真实用户",
                    "bot_family": bot_family,
                    "bot_source": bot_source,
                    "bvid": _extract_bvid_from_target(path_value, query_string),
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


def _target_path_query(target: str) -> tuple[str, str]:
    target = str(target or "/")
    if target.startswith("http://") or target.startswith("https://"):
        parsed = urlparse(target)
        return _normalize_path(parsed.path or "/"), parsed.query
    path, _, query = target.partition("?")
    return _normalize_path(path or "/"), query


def _access_date_key(value: str) -> str | None:
    value = str(value or "")
    if len(value) < 11:
        return None
    day = value[:2]
    month = ACCESS_LOG_MONTHS.get(value[3:6])
    year = value[7:11]
    if not month or not day.isdigit() or not year.isdigit():
        return None
    return f"{year}-{month}-{day}"


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


def _extract_bvid_from_target(path: str, query_string: str) -> str | None:
    if "BV" not in (path or "") and "BV" not in (query_string or "") and "bvid" not in (query_string or "") and "bv=" not in (query_string or ""):
        return None
    return _extract_bvid_from_parts(path, parse_qs(query_string))


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
    if value.count(".") == 3 and ":" not in value:
        parts = value.split(".")
        if all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return "unknown"
    prefix = 24 if ip.version == 4 else 48
    network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    return f"{network.network_address}/{prefix}"


def _region_items_from_segments(segments: Counter[str], limit: int = 12) -> list[dict[str, Any]]:
    if not segments:
        return []
    cache = _read_ip_region_cache()
    top_segments = segments.most_common(200)
    missing = [
        segment
        for segment, _ in top_segments[:48]
        if (
            segment not in cache
            or _is_placeholder_region((cache.get(segment) or {}).get("label"))
        )
        and _public_representative_ip(segment)
    ][:24]
    changed = False
    if missing:
        with ThreadPoolExecutor(max_workers=min(10, len(missing))) as executor:
            futures = {executor.submit(_fetch_ip_region, _public_representative_ip(segment)): segment for segment in missing}
            for future in as_completed(futures):
                segment = futures[future]
                try:
                    label = future.result()
                except Exception:
                    label = ""
                if label and not _is_placeholder_region(label):
                    cache[segment] = {"label": label, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
                    changed = True
    region_counts: Counter[str] = Counter()
    for segment, count in top_segments:
        label, did_fetch = _region_label_for_segment(segment, cache, False)
        changed = changed or did_fetch
        if not label or _is_placeholder_region(label):
            continue
        region_counts[label] += count
    if changed:
        _write_ip_region_cache(cache)
    return _counter_items(region_counts, limit)


def _region_items_from_segment_visitors(
    segment_visitors: dict[str, set[str]],
    limit: int = 12,
) -> list[dict[str, Any]]:
    segment_counts = Counter({segment: len(visitors) for segment, visitors in segment_visitors.items() if visitors})
    if not segment_counts:
        return []
    cache = _read_ip_region_cache()
    top_segments = segment_counts.most_common(200)
    missing = [
        segment
        for segment, _ in top_segments[:48]
        if (
            segment not in cache
            or _is_placeholder_region((cache.get(segment) or {}).get("label"))
        )
        and _public_representative_ip(segment)
    ][:24]
    changed = False
    if missing:
        with ThreadPoolExecutor(max_workers=min(10, len(missing))) as executor:
            futures = {executor.submit(_fetch_ip_region, _public_representative_ip(segment)): segment for segment in missing}
            for future in as_completed(futures):
                segment = futures[future]
                try:
                    label = future.result()
                except Exception:
                    label = ""
                if label and not _is_placeholder_region(label):
                    cache[segment] = {"label": label, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
                    changed = True
    visitors_by_region: dict[str, set[str]] = defaultdict(set)
    for segment, _ in top_segments:
        label, did_fetch = _region_label_for_segment(segment, cache, False)
        changed = changed or did_fetch
        if not label or _is_placeholder_region(label):
            continue
        visitors_by_region[label].update(segment_visitors.get(segment) or set())
    if changed:
        _write_ip_region_cache(cache)
    rows = [
        {"name": label, "value": len(visitors)}
        for label, visitors in visitors_by_region.items()
        if visitors
    ]
    rows.sort(key=lambda item: item["value"], reverse=True)
    return rows[:limit]


def _region_label_for_segment(segment: str, cache: dict[str, dict[str, Any]], can_fetch: bool) -> tuple[str, bool]:
    segment = str(segment or "").strip()
    if not segment or segment == "unknown":
        return "", False
    cached = cache.get(segment)
    if isinstance(cached, dict) and cached.get("label") and not _is_placeholder_region(cached.get("label")):
        return str(cached["label"]), False
    ip_value = _representative_ip(segment)
    if not ip_value:
        return "", False
    try:
        ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return "", False
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        label = "内网/保留地址"
        cache[segment] = {"label": label, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        return label, True
    if not can_fetch:
        return "", False
    label = _fetch_ip_region(ip_value)
    if label and not _is_placeholder_region(label):
        cache[segment] = {"label": label, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        return label, True
    return "", False


def _representative_ip(segment: str) -> str:
    try:
        network = ipaddress.ip_network(segment, strict=False)
    except ValueError:
        return ""
    try:
        value = int(network.network_address)
        if network.num_addresses > 2:
            value += 1
        return str(ipaddress.ip_address(value))
    except Exception:
        return str(network.network_address)


def _public_representative_ip(segment: str) -> str:
    ip_value = _representative_ip(segment)
    if not ip_value:
        return ""
    try:
        ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return ""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return ""
    return ip_value


def _fetch_ip_region(ip_value: str) -> str:
    for fetcher in (_fetch_ipapi_region, _fetch_ipinfo_region):
        label = fetcher(ip_value)
        if label and not _is_placeholder_region(label):
            return label
    return ""


def _fetch_ipapi_region(ip_value: str) -> str:
    try:
        response = requests.get(
            f"https://ipapi.co/{ip_value}/json/",
            timeout=(0.6, 1.8),
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return ""
    if payload.get("error"):
        return ""
    return _format_region_label(
        payload.get("country_code"),
        payload.get("country_name"),
        payload.get("region"),
        payload.get("city"),
    )


def _fetch_ipinfo_region(ip_value: str) -> str:
    try:
        response = requests.get(f"https://ipinfo.io/{ip_value}/json", timeout=(0.6, 1.8))
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return ""
    return _format_region_label(
        payload.get("country"),
        payload.get("country"),
        payload.get("region"),
        payload.get("city"),
    )


def _format_region_label(country_code: Any, country_name: Any, region_name: Any, city_name: Any) -> str:
    code = str(country_code or "").strip().upper()
    country = COUNTRY_LABELS.get(code) or str(country_name or "").strip()
    region = _friendly_region_name(region_name)
    city = _friendly_region_name(city_name)
    parts: list[str] = []
    for value in (country, region, city):
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return " / ".join(parts[:3])


def _friendly_region_name(value: Any) -> str:
    text = str(value or "").strip()
    return REGION_LABELS.get(text, text)


def _normalize_region_label(value: Any) -> str:
    parts = [part.strip() for part in str(value or "").split("/") if part.strip()]
    normalized: list[str] = []
    for part in parts:
        text = COUNTRY_LABELS.get(part.upper()) or _friendly_region_name(part)
        if text and text not in normalized:
            normalized.append(text)
    return " / ".join(normalized[:3])


def _is_placeholder_region(value: Any) -> bool:
    text = _normalize_region_label(value)
    return not text or text in IP_REGION_LABEL_BLACKLIST


def _read_ip_region_cache() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(OPS_IP_REGION_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    cache: dict[str, dict[str, Any]] = {}
    for segment, value in raw.items():
        if not isinstance(value, dict):
            continue
        label = _normalize_region_label(value.get("label"))
        if _is_placeholder_region(label):
            continue
        cache[str(segment)] = {**value, "label": label}
    return cache


def _write_ip_region_cache(cache: dict[str, dict[str, Any]]) -> None:
    try:
        OPS_IP_REGION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        trimmed = dict(list(cache.items())[-800:])
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(OPS_IP_REGION_CACHE_FILE.parent),
            delete=False,
        ) as tmp_file:
            json.dump(trimmed, tmp_file, ensure_ascii=False, sort_keys=True)
            tmp_name = tmp_file.name
        os.replace(tmp_name, OPS_IP_REGION_CACHE_FILE)
        try:
            if os.geteuid() == 0:
                user = pwd.getpwnam("www")
                os.chown(OPS_IP_REGION_CACHE_FILE, user.pw_uid, user.pw_gid)
            os.chmod(OPS_IP_REGION_CACHE_FILE, 0o660)
        except OSError:
            pass
    except Exception:
        return


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


def _bot_family(value: str) -> str:
    return _bot_info(value)[0]


def _bot_info(value: str) -> tuple[str, str]:
    ua = str(value or "").lower()
    if not _contains_any(ua, ALL_BOT_TOKENS):
        return "真实用户", "真实用户"
    for family, source, tokens in BOT_SOURCE_RULES:
        if _contains_any(ua, tokens):
            return family, source
    return "其他 Bot", "其他 Bot"


def _contains_any(value: str, tokens: tuple[str, ...]) -> bool:
    return any(token in value for token in tokens)


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
        "button_clicks": 0,
        "ai_calls": 0,
        "builtin_ai_calls": 0,
        "custom_ai_calls": 0,
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
