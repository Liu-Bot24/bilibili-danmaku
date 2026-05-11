from __future__ import annotations

from collections import defaultdict, deque
from html import escape
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
from queue import Empty
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import quote, urlparse

import requests
from flask import Flask, Response, g, jsonify, render_template, request, send_from_directory, stream_with_context
from werkzeug.exceptions import Forbidden, HTTPException

from config import get_analysis_config, get_app_access_token
from danmaku_backend.analysis.ai_analyzer import AIAnalyzer
from danmaku_backend.analysis.deep_analysis import DeepAnalysis
from danmaku_backend.runtime.bootstrap import ensure_directories
from danmaku_backend.runtime.logging_bus import set_job_event_writer
from danmaku_backend.services.artifacts import default_store
from danmaku_backend.services.analytics import build_ops_dashboard, record_client_event, record_request_event
from danmaku_backend.services.baidu_submit import default_baidu_submitter
from danmaku_backend.services.bilibili import BILIBILI_HEADERS, extract_bvid, get_video_info
from danmaku_backend.services.database import connect_state_db, ensure_state_db
from danmaku_backend.services.downloads import get_danmaku
from danmaku_backend.services.jobs import TERMINAL_STATUSES, default_job_store
from danmaku_backend.services.reports import default_report_store
from danmaku_backend.services.stats import analyze_danmaku
from danmaku_backend.settings import (
    LOG_FILE,
    LOG_STREAM_MAX_AGE_SECONDS,
    LOG_STREAM_MAX_PER_IP,
    MAX_UPLOAD_BYTES,
    STATE_DB_PATH,
)
from logger import log_error, log_queue


app = Flask(__name__)
app.static_folder = "static"
app.static_url_path = "/static"
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
log_formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
log_handler = RotatingFileHandler(str(LOG_FILE), maxBytes=10 * 1024 * 1024, backupCount=10)
log_handler.setFormatter(log_formatter)
app.logger.addHandler(log_handler)
app.logger.setLevel(logging.INFO)

_log_clients: defaultdict[str, int] = defaultdict(int)
_log_clients_lock = threading.Lock()
_request_history: defaultdict[tuple[str, str], deque[float]] = defaultdict(deque)
_request_history_lock = threading.Lock()
_rate_limit_db_ready = False
_rate_limit_db_lock = threading.Lock()
_RATE_LIMITS = {
    "download": (30, 600),
    "analysis": (12, 600),
    "upload": (20, 600),
    "maintenance": (6, 600),
}
_CSRF_COOKIE = "bili_danmaku_csrf"
_CSRF_HEADER = "X-Bili-Danmaku-CSRF"
SITE_BASE_URL = "https://danmu.liu-qi.cn"
PLUGIN_VERSION = "1.0.2"
PLUGIN_DOWNLOAD_FILENAME = "bili-lite-subtitle-store-upload-1.0.2.zip"
PLUGIN_DOWNLOAD_PATH = f"/static/plugin/{PLUGIN_DOWNLOAD_FILENAME}"
PLUGIN_CHROME_WEB_STORE_URL = "https://chromewebstore.google.com/detail/b-%E7%AB%99%E8%BD%BB%E9%87%8F%E5%AD%97%E5%B9%95%E5%8A%A9%E6%89%8B/ifhokpfhemfpnmgoajodifgamfbhioga"
PLUGIN_GITHUB_URL = "https://github.com/Liu-Bot24/bili-lite-subtitle"
INDEXNOW_KEY = "782ce4166c93b3da40b54acae9b34686"
SOGOU_SITE_VERIFICATION = "d5smJSzPUQ"
SITEMAP_PAGES = (
    {"path": "/", "lastmod": "2026-04-29", "changefreq": "weekly", "priority": "1.0"},
    {"path": "/plugin", "lastmod": "2026-05-11", "changefreq": "monthly", "priority": "0.8"},
    {"path": "/faq", "lastmod": "2026-04-29", "changefreq": "monthly", "priority": "0.8"},
)
set_job_event_writer(default_job_store.add_event)


@app.before_request
def _start_request_timer():
    g.ops_request_started_at = time.perf_counter()


@app.after_request
def _record_request_analytics(response):
    started_at = getattr(g, "ops_request_started_at", None)
    duration_ms = None
    if started_at is not None:
        duration_ms = (time.perf_counter() - started_at) * 1000
    try:
        record_request_event(request, response, duration_ms)
    except Exception:
        app.logger.exception("Request analytics write failed")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=15552000")
    return response

FAQ_CONTENT = [
    {
        "category": "基础使用",
        "items": [
            {
                "question": "本站是否收费？",
                "answer": "本站目前为免费公益站点，不设付费项目；弹幕下载、统计图表和 AI 分析均可免费使用。受服务器负载和第三方 API 可用性影响，高峰时段的 AI 分析可能排队、超时或失败。需要更稳定的分析体验时，可在页面中自主配置 OpenAI 兼容 API。",
            },
            {
                "question": "本站支持哪些 Bilibili 视频弹幕？",
                "answer": "本站支持查询公开可访问的 Bilibili 视频弹幕。可输入 BV 号、标准视频链接，或带参数的视频链接；常见格式包括 BV1w7411Y77t 和 https://www.bilibili.com/video/BV1w7411Y77t。",
            },
            {
                "question": "只有视频链接，没有 BV 号可以使用吗？",
                "answer": "可以。将 Bilibili 视频详情页链接粘贴到查询框后，本站会自动提取 BV 号并解析弹幕。为提高解析成功率，建议使用视频详情页的完整链接。",
            },
        ],
    },
    {
        "category": "弹幕下载与格式",
        "items": [
            {
                "question": "弹幕支持哪些下载格式？",
                "answer": "解析完成后可下载 CSV 和 TXT 两种格式。CSV 保留更完整的弹幕字段，适合表格筛选、统计和二次分析；TXT 更轻量，适合快速查看、复制或交给文本处理流程。",
            },
            {
                "question": "下载的弹幕文件里通常包含哪些信息？",
                "answer": "下载文件会保留弹幕文本及其对应时间等解析信息。TXT 文件做了简化，主要保留时间戳和文本内容，便于复制、阅读和进行 AI 分析；如需自行做完整字段统计、筛选或全维度弹幕分析，建议下载 CSV 版本。",
            },
        ],
    },
    {
        "category": "统计图表",
        "items": [
            {
                "question": "弹幕密度分布和词云适合用来做什么？",
                "answer": "弹幕密度分布用于定位视频中的高互动时间段，词云用于概览观众反复提到的关键词。两者适合用于视频复盘、选题观察、内容研究和评论区趋势判断。",
            },
            {
                "question": "为什么有些视频的弹幕统计较少？",
                "answer": "弹幕统计结果取决于公开可获取的数据范围。视频发布时间较近、弹幕总量较少、存在访问限制，或 Bilibili 上游接口临时不可用时，本站可展示的数据也会相应减少。",
            },
        ],
    },
    {
        "category": "AI 弹幕分析",
        "items": [
            {
                "question": "弹幕内容分析和字幕深度分析有什么区别？",
                "answer": "弹幕内容分析基于弹幕数据生成观众态度、互动特征和热点话题；字幕深度分析会在上传字幕 TXT 后，将字幕内容与弹幕数据结合分析，更适合用于视频结构复盘和关键片段总结。",
            },
            {
                "question": "没有字幕文件还能进行 AI 分析吗？",
                "answer": "可以，仍可进行弹幕内容分析。字幕深度分析需要额外上传 TXT 字幕文件，因为该功能需要同时读取字幕文本和弹幕数据。",
            },
        ],
    },
    {
        "category": "自主配置 API 与隐私",
        "items": [
            {
                "question": "自主配置 API Key 会保存到服务器吗？",
                "answer": "不会。自主配置 API 的 Base URL、模型名和 API Key 仅保存在当前浏览器的本地存储中；分析时由浏览器直接请求你配置的 OpenAI 兼容接口，配置内容不会写入本站服务器。清理浏览器数据、更换设备或使用无痕模式时，这些配置可能会失效。",
            },
            {
                "question": "内置 AI 分析和自主配置 API 有什么区别？",
                "answer": "内置 AI 分析使用本站服务器端配置，无需填写 API Key，适合直接使用；分析任务会进入公共处理队列，负载较高时可能等待或超时。启用自主配置 API 后，本站服务器只负责整理弹幕、字幕和分析提示词，浏览器会直接调用你填写的 OpenAI 兼容接口。自主配置 API 支持设置 Base URL、模型名、API Key、上下文长度、最大输出 tokens、全量/均衡采样和样本数量等参数；如果你的接口额度、响应速度或上下文窗口更充足，通常可以改善排队等待、超时概率、模型可选范围和长视频分析承载能力。",
            },
        ],
    },
    {
        "category": "分享报告与故障处理",
        "items": [
            {
                "question": "分享报告链接会保存哪些内容？",
                "answer": "分享报告保存当前结果页的结构化分析快照，用于复现视频信息、统计图表和 AI 分析结果。本站不会将原始弹幕文件或上传的字幕原文作为长期报告数据保存。",
            },
            {
                "question": "为什么分享报告链接可能会失效？",
                "answer": "报告链接有保留期限，目前按站点配置保留约 30 天，过期后会从公开报告目录移入归档。用于长期传播时，建议同时保留对应的标准结果页链接，例如 /result?bvid=BV号。",
            },
        ],
    },
]


def _video_cover_proxy_url(bvid: str) -> str:
    return f"{SITE_BASE_URL}/api/v2/video-cover/{bvid}.jpg"


def _client_ip() -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    return forwarded_for.split(",", 1)[0].strip() or request.remote_addr or "unknown"


def _same_origin_request() -> bool:
    origin = request.headers.get("Origin")
    host_url = request.host_url.rstrip("/")
    if origin:
        return origin.rstrip("/") == host_url

    referer = request.headers.get("Referer")
    if referer:
        parsed = urlparse(referer)
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/") == host_url

    return request.headers.get("Sec-Fetch-Site") == "same-origin"


def _valid_app_token() -> bool:
    required_token = get_app_access_token()
    provided_token = request.headers.get("X-Bili-Danmaku-Token", "")
    return bool(required_token and hmac.compare_digest(provided_token, required_token))


def _csrf_token() -> str:
    token = request.cookies.get(_CSRF_COOKIE, "")
    if len(token) < 24:
        return secrets.token_urlsafe(32)
    return token


def _valid_csrf() -> bool:
    cookie_token = request.cookies.get(_CSRF_COOKIE, "")
    header_token = request.headers.get(_CSRF_HEADER, "")
    return bool(cookie_token and header_token and hmac.compare_digest(cookie_token, header_token))


def _rate_limit(kind: str) -> bool:
    limit, window_seconds = _RATE_LIMITS.get(kind, (60, 600))
    client_key = _client_ip()
    now = time.time()
    try:
        _ensure_rate_limit_db()
        with connect_state_db(STATE_DB_PATH) as conn:
            conn.execute("DELETE FROM request_rate_limits WHERE ts < ?", (now - window_seconds,))
            count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM request_rate_limits
                WHERE client_key = ? AND kind = ? AND ts >= ?
                """,
                (client_key, kind, now - window_seconds),
            ).fetchone()["count"]
            if int(count) >= limit:
                return False
            conn.execute(
                "INSERT INTO request_rate_limits(client_key, kind, ts) VALUES (?, ?, ?)",
                (client_key, kind, now),
            )
            return True
    except Exception:
        app.logger.exception("SQLite rate limit failed; using process-local fallback")

    key = (_client_ip(), kind)
    now = time.monotonic()
    with _request_history_lock:
        history = _request_history[key]
        while history and now - history[0] > window_seconds:
            history.popleft()
        if len(history) >= limit:
            return False
        history.append(now)
        return True


def _ensure_rate_limit_db() -> None:
    global _rate_limit_db_ready
    if _rate_limit_db_ready:
        return
    with _rate_limit_db_lock:
        if not _rate_limit_db_ready:
            ensure_state_db(STATE_DB_PATH)
            _rate_limit_db_ready = True


def _guard_post(kind: str, *, require_operator_token: bool = False):
    if require_operator_token:
        if not _valid_app_token():
            return jsonify({"success": False, "message": "访问令牌无效"}), 403
    elif not (_valid_app_token() or (_same_origin_request() and _valid_csrf())):
        return jsonify({"success": False, "message": "请求来源不允许"}), 403

    if not _rate_limit(kind):
        return jsonify({"success": False, "message": "请求过于频繁，请稍后再试"}), 429
    return None


def _guard_read():
    if _valid_app_token() or _same_origin_request() or request.cookies.get(_CSRF_COOKIE):
        return None
    raise Forbidden()


def _noindex_response(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def _resolve_bvid_and_analysis_id(data: dict) -> tuple[str | None, str | None]:
    analysis_id = (data.get("analysis_id") or "").strip() or None
    record = default_store.get_record(analysis_id) if analysis_id else None
    bvid = extract_bvid(data.get("bvid", "") or "")
    if not bvid and record:
        bvid = record.get("bvid")
    if analysis_id and (not record or record.get("bvid") != bvid):
        raise ValueError("analysis_id does not match bvid")
    return bvid, analysis_id


def _positive_int_from_payload(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_analysis_options(data: dict) -> dict:
    config = get_analysis_config()
    sample_mode = str(data.get("sample_mode") or "balanced").strip().lower()
    if sample_mode not in {"balanced", "full"}:
        sample_mode = "balanced"
    return {
        "sample_mode": sample_mode,
        "content_max_samples": _positive_int_from_payload(
            data.get("content_max_samples"),
            config["content_max_samples"],
        ),
        "deep_max_samples": _positive_int_from_payload(
            data.get("deep_max_samples"),
            config["deep_max_samples"],
        ),
        "head_samples": _positive_int_from_payload(
            data.get("head_samples"),
            config["head_samples"],
        ),
        "peak_bucket_count": _positive_int_from_payload(
            data.get("peak_bucket_count"),
            config["peak_bucket_count"],
        ),
        "peak_window_seconds": _positive_int_from_payload(
            data.get("peak_window_seconds"),
            config["peak_window_seconds"],
        ),
        "peak_samples_per_bucket": _positive_int_from_payload(
            data.get("peak_samples_per_bucket"),
            config["peak_samples_per_bucket"],
        ),
    }


def _start_analysis_job(
    kind: str,
    bvid: str,
    analysis_id: str | None,
    analysis_options: dict | None = None,
) -> dict:
    job = default_job_store.create(
        kind,
        {
            "bvid": bvid,
            "analysis_id": analysis_id,
            "analysis_options": analysis_options or {},
        },
    )
    return job


def _download_url(filename: str) -> str:
    safe_name = default_store.safe_download_path(filename)
    if "/" not in safe_name:
        suffix = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
        if suffix == "csv":
            safe_name = f"CSV/{safe_name}"
        elif suffix == "txt":
            safe_name = f"TXT/{safe_name}"
    return f"/downloads/{quote(safe_name, safe='/')}"


def _prepare_content_analysis_bundle(
    bvid: str,
    analysis_id: str | None,
    analysis_options: dict,
) -> dict:
    file_path = default_store.latest_danmaku_txt(bvid, analysis_id)
    if not file_path:
        raise FileNotFoundError("未找到弹幕文件，请重新下载")
    video_info = get_video_info(bvid)
    if not video_info:
        raise RuntimeError("获取视频信息失败")
    danmaku_list = AIAnalyzer.parse_danmaku_txt(str(file_path))
    if not danmaku_list:
        raise RuntimeError("读取弹幕文件失败")
    analysis_result = analyze_danmaku(danmaku_list)
    bundle = AIAnalyzer.prepare_content_request(
        video_info,
        danmaku_list,
        analysis_result["time_density"],
        analysis_options=analysis_options,
        with_logs=False,
    )
    return {
        "prompt": bundle["prompt"],
        "sample_mode": bundle["sample_mode"],
        "sample_count": bundle["sample_count"],
        "peak_count": bundle["peak_count"],
        "analysis_id": analysis_id,
        "bvid": bvid,
    }


def _prepare_deep_analysis_bundle(
    bvid: str,
    analysis_id: str | None,
    analysis_options: dict,
) -> dict:
    subtitle_path = default_store.latest_subtitle(bvid, analysis_id)
    if not subtitle_path:
        raise FileNotFoundError("未找到字幕文件")
    danmaku_path = default_store.latest_danmaku_txt(bvid, analysis_id)
    if not danmaku_path:
        raise FileNotFoundError("未找到弹幕文件，请重新下载")
    subtitle_content = subtitle_path.read_text(encoding="utf-8")
    video_info = get_video_info(bvid)
    if not video_info:
        raise RuntimeError("获取视频信息失败")
    danmaku_list = AIAnalyzer.parse_danmaku_txt(str(danmaku_path))
    if not danmaku_list:
        raise RuntimeError("读取弹幕文件失败")
    analysis_result = analyze_danmaku(danmaku_list)
    bundle = DeepAnalysis.prepare_deep_request(
        video_info,
        subtitle_content,
        danmaku_list,
        analysis_result["time_density"],
        analysis_options=analysis_options,
        with_logs=False,
    )
    return {
        "prompt": bundle["prompt"],
        "sample_mode": bundle["sample_mode"],
        "sample_count": bundle["sample_count"],
        "peak_count": bundle["peak_count"],
        "analysis_id": analysis_id,
        "bvid": bvid,
    }


def generate_logs():
    started_at = time.monotonic()
    last_keepalive = 0.0
    try:
        while time.monotonic() - started_at < LOG_STREAM_MAX_AGE_SECONDS:
            sent = False
            while True:
                try:
                    log_item = log_queue.get_nowait()
                except Empty:
                    break
                app.logger.info("Sending log: %s", log_item)
                yield f"data: {json.dumps(log_item, ensure_ascii=False)}\n\n"
                sent = True

            now = time.monotonic()
            if not sent and now - last_keepalive >= 15:
                yield ": keep-alive\n\n"
                last_keepalive = now
            time.sleep(0.1)
    except GeneratorExit:
        pass
    except Exception as exc:
        app.logger.error("Log generation error: %s", exc)


def generate_job_logs(job_id: str):
    offset = 0
    last_keepalive = 0.0
    try:
        while True:
            events, offset = default_job_store.read_events(job_id, offset)
            for event in events:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            job = default_job_store.get(job_id)
            if job and job.get("status") in TERMINAL_STATUSES:
                events, offset = default_job_store.read_events(job_id, offset)
                for event in events:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                break

            now = time.monotonic()
            if now - last_keepalive >= 15:
                yield ": keep-alive\n\n"
                last_keepalive = now
            time.sleep(0.5)
    except GeneratorExit:
        pass
    except Exception as exc:
        app.logger.error("Job log generation error: %s", exc)


@app.route("/logs")
def logs():
    _guard_read()

    job_id = (request.args.get("job_id") or "").strip()
    remote_ip = _client_ip()
    with _log_clients_lock:
        if _log_clients[remote_ip] >= LOG_STREAM_MAX_PER_IP:
            raise Forbidden()
        _log_clients[remote_ip] += 1

    def wrapped():
        try:
            if job_id:
                yield from generate_job_logs(job_id)
            else:
                yield from generate_logs()
        finally:
            with _log_clients_lock:
                _log_clients[remote_ip] = max(0, _log_clients[remote_ip] - 1)

    return Response(stream_with_context(wrapped()), mimetype="text/event-stream")


@app.route("/")
def index():
    return _render_frontend("index.html", is_result_page=False, initial_bvid="", initial_report_id="")


@app.route("/result")
def result_page():
    return _render_frontend(
        "index.html",
        is_result_page=True,
        initial_bvid=request.args.get("bvid", ""),
        initial_report_id=request.args.get("report", ""),
    )


@app.route("/faq", strict_slashes=False)
def faq_page():
    return _render_frontend(
        "index.html",
        is_result_page=False,
        is_faq_page=True,
        initial_bvid="",
        initial_report_id="",
    )


@app.route("/plugin/")
def plugin_page_trailing_slash():
    return Response("", status=301, headers={"Location": f"{SITE_BASE_URL}/plugin"})


@app.route("/plugin")
def plugin_page():
    return _render_frontend(
        "index.html",
        is_result_page=False,
        is_plugin_page=True,
        initial_bvid="",
        initial_report_id="",
    )


@app.route("/favicon.ico")
def favicon_ico():
    response = send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route("/ops", strict_slashes=False)
def ops_dashboard_page():
    response = app.make_response(render_template("ops_dashboard.html"))
    return _noindex_response(response)


@app.route("/robots.txt")
def robots_txt():
    content = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "Disallow: /logs",
            "Disallow: /ops",
            "Disallow: /api/",
            "Disallow: /api/v2/ops-dashboard",
            "Disallow: /api/v2/ops-events",
            "Disallow: /download",
            "Disallow: /downloads/",
            "Disallow: /upload_subtitle",
            "Disallow: /analyze_content",
            "Disallow: /deep_analysis",
            "Disallow: /subtitles/",
            "",
            f"Sitemap: {SITE_BASE_URL}/sitemap.xml",
            "",
        ]
    )
    return Response(content, content_type="text/plain; charset=utf-8")


@app.route("/sitemap.xml")
def sitemap_xml():
    url_blocks = []
    for page in SITEMAP_PAGES:
        loc = escape(f"{SITE_BASE_URL}{page['path']}", quote=True)
        url_blocks.append(
            "\n".join(
                [
                    "  <url>",
                    f"    <loc>{loc}</loc>",
                    f"    <lastmod>{page['lastmod']}</lastmod>",
                    f"    <changefreq>{page['changefreq']}</changefreq>",
                    f"    <priority>{page['priority']}</priority>",
                    "  </url>",
                ]
            )
        )
    content = "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
            *url_blocks,
            "</urlset>",
            "",
        ]
    )
    return Response(content, content_type="application/xml; charset=utf-8")


@app.route(f"/{INDEXNOW_KEY}.txt")
def indexnow_key_file():
    return Response(f"{INDEXNOW_KEY}\n", content_type="text/plain; charset=utf-8")


@app.route("/sogousiteverification.txt")
def sogou_site_verification():
    return Response(f"{SOGOU_SITE_VERIFICATION}\n", content_type="text/plain; charset=utf-8")


@app.route("/api/v2/video-cover/<bvid>.jpg")
def video_cover(bvid):
    bvid = extract_bvid(bvid or "")
    if not bvid:
        return jsonify({"success": False, "message": "未找到有效的BV号"}), 400
    video_info = get_video_info(bvid)
    source_url = ""
    if video_info:
        source_url = str(
            video_info.get("app_cover_url")
            or video_info.get("cover_url")
            or video_info.get("web_cover_url")
            or video_info.get("first_frame_url")
            or ""
        ).strip()
    if not source_url:
        return jsonify({"success": False, "message": "未找到视频封面"}), 404

    try:
        upstream = requests.get(
            source_url,
            headers={**BILIBILI_HEADERS, "Referer": f"https://www.bilibili.com/video/{bvid}"},
            timeout=(5, 12),
        )
        upstream.raise_for_status()
    except Exception as exc:
        app.logger.info("video cover proxy failed for %s: %s", bvid, exc)
        return jsonify({"success": False, "message": "封面加载失败"}), 502

    content_type = upstream.headers.get("Content-Type") or "image/jpeg"
    response = Response(upstream.content, mimetype=content_type.split(";", 1)[0])
    response.headers["Cache-Control"] = "public, max-age=21600"
    return response


def _render_frontend(
    template_name: str,
    *,
    is_result_page: bool,
    is_faq_page: bool = False,
    is_plugin_page: bool = False,
    initial_bvid: str,
    initial_report_id: str,
):
    csrf_token = _csrf_token()
    report_preview = _load_report_preview(is_result_page, initial_report_id)
    render_initial_bvid = (report_preview or {}).get("bvid") or initial_bvid
    report_snapshot = (report_preview or {}).get("snapshot") or {}
    report_video_info = report_snapshot.get("video_info") or {}
    if is_result_page and render_initial_bvid and not report_video_info.get("cover_url"):
        try:
            fresh_video_info = get_video_info(render_initial_bvid)
            if fresh_video_info:
                report_video_info = {**report_video_info, **fresh_video_info}
        except Exception as exc:
            app.logger.info("initial video cover lookup failed for %s: %s", render_initial_bvid, exc)
    seo_context = _build_seo_context(
        is_result_page,
        is_faq_page,
        is_plugin_page,
        render_initial_bvid,
        initial_report_id,
        report_preview,
    )
    response = app.make_response(render_template(
        template_name,
        client_csrf_token=csrf_token,
        analysis_config=get_analysis_config(),
        report_config=default_report_store.public_config(),
        faq_items=FAQ_CONTENT,
        plugin_info={
            "version": PLUGIN_VERSION,
            "store_url": PLUGIN_CHROME_WEB_STORE_URL,
            "github_url": PLUGIN_GITHUB_URL,
            "download_url": f"{SITE_BASE_URL}{PLUGIN_DOWNLOAD_PATH}",
            "download_filename": PLUGIN_DOWNLOAD_FILENAME,
        },
        report_preview=report_preview,
        report_video_info=report_video_info,
        is_result_page=is_result_page,
        is_faq_page=is_faq_page,
        is_plugin_page=is_plugin_page,
        initial_bvid=render_initial_bvid,
        initial_report_id=initial_report_id,
        seo=seo_context,
    ))
    response.set_cookie(
        _CSRF_COOKIE,
        csrf_token,
        max_age=24 * 60 * 60,
        secure=request.is_secure,
        samesite="Strict",
        httponly=False,
    )
    return response


def _load_report_preview(is_result_page: bool, initial_report_id: str) -> dict | None:
    if not is_result_page or not (initial_report_id or "").strip():
        return None
    try:
        return default_report_store.get_report((initial_report_id or "").strip())
    except Exception:
        return None


def _build_seo_context(
    is_result_page: bool,
    is_faq_page: bool,
    is_plugin_page: bool,
    initial_bvid: str,
    initial_report_id: str,
    report_preview: dict | None = None,
) -> dict:
    home_title = "B站弹幕查询、解析、下载与AI分析工具｜小刘BOT danmuku"
    home_description = "输入 BV 号或 Bilibili 视频链接，查询视频信息，下载弹幕 CSV/TXT，并查看词云、时间分布和 AI 弹幕分析。"
    home_keywords = "B站弹幕查询,Bilibili弹幕解析,B站弹幕下载,弹幕分析,BV号弹幕查询,B站视频弹幕导出,弹幕词云,AI弹幕分析,免费弹幕查询,免费弹幕下载,免费B站弹幕查询,免费B站弹幕下载,免费Bilibili弹幕查询,免费Bilibili弹幕下载"
    home_url = f"{SITE_BASE_URL}/"
    default_share_image_url = f"{SITE_BASE_URL}/static/og-default.jpg"
    share_image_url = default_share_image_url
    context = {
        "title": home_title,
        "description": home_description,
        "keywords": home_keywords,
        "canonical_url": home_url,
        "og_url": home_url,
        "image_url": share_image_url,
        "robots": "index,follow",
        "bvid": "",
        "video_title": "",
        "structured_data": {
            "@context": "https://schema.org",
            "@type": "WebApplication",
            "name": "小刘BOT danmuku",
            "url": home_url,
            "applicationCategory": "UtilityApplication",
            "operatingSystem": "Web",
            "description": "B站弹幕查询、解析、下载和 AI 分析工具，支持 BV 号与 Bilibili 视频链接。",
        },
    }
    if is_plugin_page:
        plugin_url = f"{SITE_BASE_URL}/plugin"
        plugin_title = "bilibili轻量字幕助手｜B站字幕下载Chrome插件｜小刘BOT danmuku"
        plugin_description = "bilibili轻量字幕助手是一款适用于 Chrome 和 Chromium 浏览器的 B站字幕下载插件，可从 Chrome Web Store 安装，在视频页查看、搜索、复制和下载字幕，并将字幕导入本站进行 AI 内容分析。"
        plugin_keywords = "哔哩哔哩字幕插件,B站字幕下载,Bilibili字幕下载,B站字幕插件,Chrome字幕插件,Chrome扩展,浏览器插件,字幕助手,免费B站字幕下载,AI弹幕分析,小刘BOT danmuku"
        context.update(
            {
                "title": plugin_title,
                "description": plugin_description,
                "keywords": plugin_keywords,
                "canonical_url": plugin_url,
                "og_url": plugin_url,
                "image_url": default_share_image_url,
                "robots": "index,follow",
                "structured_data": {
                    "@context": "https://schema.org",
                    "@type": "SoftwareApplication",
                    "name": "bilibili轻量字幕助手",
                    "url": plugin_url,
                    "downloadUrl": f"{SITE_BASE_URL}{PLUGIN_DOWNLOAD_PATH}",
                    "softwareVersion": PLUGIN_VERSION,
                    "applicationCategory": "BrowserApplication",
                    "operatingSystem": "Chrome, Chromium",
                    "browserRequirements": "Chrome 或 Chromium 内核浏览器",
                    "description": plugin_description,
                    "isAccessibleForFree": True,
                    "inLanguage": "zh-CN",
                    "sameAs": PLUGIN_GITHUB_URL,
                    "offers": {
                        "@type": "Offer",
                        "price": "0",
                        "priceCurrency": "CNY",
                        "availability": "https://schema.org/InStock",
                    },
                    "publisher": {
                        "@type": "Organization",
                        "name": "小刘BOT danmuku",
                        "url": SITE_BASE_URL,
                    },
                    "isPartOf": {
                        "@type": "WebApplication",
                        "name": "小刘BOT danmuku",
                        "url": SITE_BASE_URL,
                    },
                },
            }
        )
        return context

    if is_faq_page:
        faq_url = f"{SITE_BASE_URL}/faq"
        faq_title = "常见问题 FAQ｜免费B站弹幕查询、下载与AI分析｜小刘BOT danmuku"
        faq_description = "小刘BOT danmuku 常见问题，了解 B站弹幕查询、Bilibili 弹幕下载、CSV/TXT 格式、弹幕词云、AI 弹幕分析、自主配置 API 和分享报告。"
        faq_keywords = "B站弹幕FAQ,Bilibili弹幕下载问题,B站弹幕查询教程,AI弹幕分析FAQ,弹幕词云,CSV弹幕下载,TXT弹幕下载,小刘BOT danmuku,免费弹幕查询,免费弹幕下载,免费B站弹幕查询,免费B站弹幕下载,免费Bilibili弹幕查询,免费Bilibili弹幕下载"
        questions = [
            {
                "@type": "Question",
                "name": item["question"],
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": item["answer"],
                },
            }
            for group in FAQ_CONTENT
            for item in group["items"]
        ]
        context.update(
            {
                "title": faq_title,
                "description": faq_description,
                "keywords": faq_keywords,
                "canonical_url": faq_url,
                "og_url": faq_url,
                "image_url": default_share_image_url,
                "robots": "index,follow",
                "structured_data": {
                    "@context": "https://schema.org",
                    "@type": "FAQPage",
                    "name": "小刘BOT danmuku 常见问题",
                    "url": faq_url,
                    "description": faq_description,
                    "mainEntity": questions,
                },
            }
        )
        return context

    if not is_result_page:
        return context

    bvid = extract_bvid(initial_bvid or "")
    report_id = (initial_report_id or "").strip()
    video_info = None
    report = report_preview
    if report_id:
        if report:
            bvid = report.get("bvid") or bvid
            video_info = (report.get("snapshot") or {}).get("video_info") or None
    if bvid and (not video_info or not (video_info or {}).get("cover_url")):
        try:
            fresh_video_info = get_video_info(bvid)
            if fresh_video_info:
                video_info = {**(video_info or {}), **fresh_video_info}
        except Exception as exc:
            app.logger.info("SEO video info lookup failed for %s: %s", bvid, exc)
            video_info = video_info or None

    video_title = str((video_info or {}).get("title") or "").strip()
    video_author = str((video_info or {}).get("author") or "").strip()
    has_video_cover = bool(
        video_info
        and bvid
        and (
            str(video_info.get("app_cover_url") or "").strip()
            or str(video_info.get("cover_url") or "").strip()
            or str(video_info.get("web_cover_url") or "").strip()
            or str(video_info.get("first_frame_url") or "").strip()
        )
    )
    if has_video_cover:
        share_image_url = _video_cover_proxy_url(bvid)
    if bvid and video_title:
        title_subject = f"{video_title} - {video_author}" if video_author else video_title
        title = f"{title_subject}｜{bvid}｜Bilibili弹幕解析下载｜小刘BOT danmuku"
        description = f"在线解析《{video_title}》（{bvid}）的 Bilibili 弹幕，下载 CSV/TXT，查看词云、时间分布、互动统计和 AI 弹幕分析。"
        keywords = f"{video_title},{bvid},{home_keywords}"
        canonical_url = f"{SITE_BASE_URL}/result?bvid={bvid}"
    elif bvid:
        title = f"{bvid}｜Bilibili弹幕解析下载｜小刘BOT danmuku"
        description = f"在线解析 {bvid} 的 Bilibili 弹幕，下载 CSV/TXT，查看词云、时间分布、互动统计和 AI 弹幕分析。"
        keywords = f"{bvid},{home_keywords}"
        canonical_url = f"{SITE_BASE_URL}/result?bvid={bvid}"
    else:
        title = "Bilibili弹幕解析结果｜小刘BOT danmuku"
        description = home_description
        keywords = home_keywords
        canonical_url = f"{SITE_BASE_URL}/result"

    page_data = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "url": canonical_url,
        "description": description,
        "isPartOf": {
            "@type": "WebSite",
            "name": "小刘BOT danmuku",
            "url": SITE_BASE_URL,
        },
    }
    if bvid and video_title:
        video_object = {
            "@type": "VideoObject",
            "name": video_title,
            "url": f"https://www.bilibili.com/video/{bvid}",
            "description": str(video_info.get("description") or description)[:500],
        }
        if share_image_url:
            video_object["thumbnailUrl"] = [share_image_url]
        publish_time = str(video_info.get("publish_time") or "").strip()
        if publish_time:
            try:
                video_object["uploadDate"] = datetime.strptime(publish_time, "%Y-%m-%d %H:%M:%S").isoformat()
            except ValueError:
                pass
        page_data["about"] = video_object

    context.update(
        {
            "title": title,
            "description": description,
            "keywords": keywords,
            "canonical_url": canonical_url,
            "og_url": canonical_url,
            "image_url": share_image_url,
            "robots": "index,follow",
            "bvid": bvid or "",
            "video_title": video_title,
            "structured_data": page_data,
        }
    )
    return context


@app.route("/download", methods=["POST"])
def download():
    guard = _guard_post("download")
    if guard:
        return guard

    bvid = extract_bvid(request.form.get("bvid", ""))
    if not bvid:
        return jsonify({"success": False, "message": "未找到有效的BV号，请检查输入"}), 400

    result = get_danmaku(bvid)
    if isinstance(result, tuple):
        return jsonify({"success": False, "message": result[1]}), 502

    return jsonify(
        {
            "success": True,
            "message": f"成功下载 {result['count']} 条弹幕",
            "analysis_id": result["analysis_id"],
            "csv_filename": result["csv_filename"],
            "txt_filename": result["txt_filename"],
            "download_urls": {
                "csv": _download_url(result["csv_filename"]),
                "txt": _download_url(result["txt_filename"]),
            },
            "video_info": result["video_info"],
            "analysis": result["analysis"],
            "danmaku_list": result["danmaku_list"],
            "meta": {
                "schema_version": "1.1",
                "bvid": bvid,
                "analysis_id": result["analysis_id"],
            },
        }
    )


@app.route("/analyze_content", methods=["POST"])
def analyze_content_route():
    try:
        guard = _guard_post("analysis")
        if guard:
            return guard

        data = request.get_json(silent=True) or {}
        bvid, analysis_id = _resolve_bvid_and_analysis_id(data)
        if not bvid:
            return jsonify({"success": False, "message": "未提供有效BV号"}), 400

        if not analysis_id:
            record = default_store.latest_record_for_bvid(bvid)
            if record:
                analysis_id = record.get("analysis_id")

        if not default_store.latest_danmaku_txt(bvid, analysis_id):
            return jsonify({"success": False, "message": "未找到弹幕文件，请重新下载"}), 404
        job = _start_analysis_job("content_analysis", bvid, analysis_id)
        return jsonify(
            {
                "success": True,
                "job_id": job["job_id"],
                "status": job["status"],
                "analysis_id": analysis_id,
                "meta": {"schema_version": "1.1", "bvid": bvid, "analysis_id": analysis_id},
            }
        )
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        log_error(f"内容分析出错：{exc}")
        return jsonify({"success": False, "message": "分析失败，请稍后再试"}), 500


@app.route("/downloads/<path:filename>")
def download_file(filename):
    try:
        path = default_store.resolve_download_path(filename)
        safe_name = path.name
        return send_from_directory(str(path.parent), safe_name, as_attachment=True)
    except Exception:
        return jsonify({"success": False, "message": "文件不存在"}), 404


@app.route("/upload_subtitle", methods=["POST"])
def upload_subtitle():
    try:
        guard = _guard_post("upload")
        if guard:
            return guard

        if "subtitle" not in request.files:
            return jsonify({"success": False, "message": "未找到上传文件"}), 400

        analysis_id = (request.form.get("analysis_id") or "").strip() or None
        bvid = extract_bvid(request.form.get("bvid", ""))
        subtitle_file = request.files["subtitle"]
        if not subtitle_file or not bvid:
            return jsonify({"success": False, "message": "参数不完整"}), 400
        if not analysis_id:
            record = default_store.latest_record_for_bvid(bvid)
            if record:
                analysis_id = record.get("analysis_id")

        default_store.save_subtitle(subtitle_file, bvid, analysis_id)
        return jsonify(
            {
                "success": True,
                "message": "字幕文件上传成功",
                "analysis_id": analysis_id,
                "meta": {"schema_version": "1.1", "bvid": bvid, "analysis_id": analysis_id},
            }
        )
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        log_error(f"字幕上传出错：{exc}")
        return jsonify({"success": False, "message": "字幕上传失败，请稍后再试"}), 500


@app.route("/deep_analysis", methods=["POST"])
def deep_analysis_route():
    try:
        guard = _guard_post("analysis")
        if guard:
            return guard

        data = request.get_json(silent=True) or {}
        bvid, analysis_id = _resolve_bvid_and_analysis_id(data)
        if not bvid:
            return jsonify({"success": False, "message": "未提供有效BV号"}), 400

        if not analysis_id:
            record = default_store.latest_record_for_bvid(bvid, require_subtitle=True)
            if record:
                analysis_id = record.get("analysis_id")

        if not default_store.latest_subtitle(bvid, analysis_id):
            return jsonify({"success": False, "message": "未找到字幕文件"}), 404
        if not default_store.latest_danmaku_txt(bvid, analysis_id):
            return jsonify({"success": False, "message": "未找到弹幕文件"}), 404
        job = _start_analysis_job("deep_analysis", bvid, analysis_id)
        return jsonify(
            {
                "success": True,
                "job_id": job["job_id"],
                "status": job["status"],
                "analysis_id": analysis_id,
                "meta": {"schema_version": "1.1", "bvid": bvid, "analysis_id": analysis_id},
            }
        )
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        log_error(f"深度分析出错：{exc}")
        return jsonify({"success": False, "message": "分析失败，请稍后再试"}), 500


@app.route("/api/v2/custom-analysis/content", methods=["POST"])
def prepare_custom_content_analysis():
    try:
        guard = _guard_post("analysis")
        if guard:
            return guard

        data = request.get_json(silent=True) or {}
        bvid, analysis_id = _resolve_bvid_and_analysis_id(data)
        if not bvid:
            return jsonify({"success": False, "message": "未提供有效BV号"}), 400
        if not analysis_id:
            record = default_store.latest_record_for_bvid(bvid)
            if record:
                analysis_id = record.get("analysis_id")
        bundle = _prepare_content_analysis_bundle(
            bvid,
            analysis_id,
            _parse_analysis_options(data),
        )
        return jsonify({"success": True, "data": bundle, "error": None, "meta": {"schema_version": "2.0"}})
    except FileNotFoundError as exc:
        return jsonify({"success": False, "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        log_error(f"自定义内容分析材料准备失败：{exc}")
        return jsonify({"success": False, "message": "分析材料准备失败，请稍后再试"}), 500


@app.route("/api/v2/custom-analysis/deep", methods=["POST"])
def prepare_custom_deep_analysis():
    try:
        guard = _guard_post("analysis")
        if guard:
            return guard

        data = request.get_json(silent=True) or {}
        bvid, analysis_id = _resolve_bvid_and_analysis_id(data)
        if not bvid:
            return jsonify({"success": False, "message": "未提供有效BV号"}), 400
        if not analysis_id:
            record = default_store.latest_record_for_bvid(bvid, require_subtitle=True)
            if record:
                analysis_id = record.get("analysis_id")
        bundle = _prepare_deep_analysis_bundle(
            bvid,
            analysis_id,
            _parse_analysis_options(data),
        )
        return jsonify({"success": True, "data": bundle, "error": None, "meta": {"schema_version": "2.0"}})
    except FileNotFoundError as exc:
        return jsonify({"success": False, "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        log_error(f"自定义深度分析材料准备失败：{exc}")
        return jsonify({"success": False, "message": "分析材料准备失败，请稍后再试"}), 500


@app.route("/api/v2/analyses/<analysis_id>")
def get_analysis_record(analysis_id):
    _guard_read()
    try:
        record = default_store.get_record(analysis_id)
    except ValueError:
        record = None
    if not record:
        return jsonify(
            {
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "analysis_id 不存在"},
                "meta": {"schema_version": "2.0"},
            }
        ), 404
    public_record = _public_analysis_record(record)
    return jsonify(
        {
            "success": True,
            "data": public_record,
            "error": None,
            "meta": {"schema_version": "2.0", "analysis_id": analysis_id},
        }
    )


@app.route("/api/v2/jobs/<job_id>")
def get_job(job_id):
    _guard_read()
    try:
        job = default_job_store.get(job_id)
    except ValueError:
        job = None
    if not job:
        return jsonify(
            {
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "job_id 不存在"},
                "meta": {"schema_version": "2.0"},
            }
        ), 404
    return jsonify({"success": True, "data": _public_job(job), "error": None, "meta": {"schema_version": "2.0"}})


@app.route("/api/v2/reports", methods=["POST"])
def save_report():
    try:
        guard = _guard_post("analysis")
        if guard:
            return guard

        data = request.get_json(silent=True) or {}
        bvid = extract_bvid(data.get("bvid", "") or "")
        if not bvid:
            return jsonify({"success": False, "message": "未提供有效BV号"}), 400

        report = default_report_store.save_report(
            report_id=(data.get("report_id") or "").strip() or None,
            bvid=bvid,
            analysis_id=(data.get("analysis_id") or "").strip() or None,
            snapshot=data.get("snapshot"),
            content_analysis=data.get("content_analysis"),
            deep_analysis=data.get("deep_analysis"),
        )
        seo_submission = default_baidu_submitter.submit_bvid_once(bvid)
        if seo_submission.get("status") == "failed":
            app.logger.info("Baidu submit failed for %s: %s", bvid, seo_submission.get("error") or seo_submission.get("response"))
        return jsonify(
            {
                "success": True,
                "data": {
                    **report,
                    "share_url": f"/result?bvid={bvid}&report={report['report_id']}",
                    "retention_days": default_report_store.public_config()["retention_days"],
                    "seo_submission": seo_submission,
                },
                "error": None,
                "meta": {"schema_version": "2.0"},
            }
        )
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        log_error(f"报告保存失败：{exc}")
        return jsonify({"success": False, "message": "报告保存失败，请稍后再试"}), 500


@app.route("/api/v2/reports/<report_id>")
def get_report(report_id):
    _guard_read()
    try:
        report = default_report_store.get_report(report_id)
    except ValueError:
        report = None
    if not report:
        return jsonify(
            {
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "报告已过期或不存在"},
                "meta": {"schema_version": "2.0"},
            }
        ), 404
    public_report = {**report}
    snapshot = public_report.get("snapshot")
    if isinstance(snapshot, dict):
        video_info = snapshot.get("video_info")
        if isinstance(video_info, dict) and not video_info.get("cover_url"):
            try:
                fresh_video_info = get_video_info(public_report.get("bvid", ""))
                if fresh_video_info:
                    public_report["snapshot"] = {
                        **snapshot,
                        "video_info": {**video_info, **fresh_video_info},
                    }
            except Exception as exc:
                app.logger.info("report video cover lookup failed for %s: %s", public_report.get("bvid"), exc)
    return jsonify(
        {
            "success": True,
            "data": {
                **public_report,
                "retention_days": default_report_store.public_config()["retention_days"],
                "share_url": f"/result?bvid={public_report['bvid']}&report={public_report['report_id']}",
            },
            "error": None,
            "meta": {"schema_version": "2.0"},
        }
    )


@app.route("/api/v2/maintenance/cleanup", methods=["POST"])
def run_cleanup():
    guard = _guard_post("maintenance", require_operator_token=True)
    if guard:
        return guard
    artifact_result = default_store.cleanup()
    job_result = default_job_store.cleanup()
    report_result = {"archived": default_report_store.archive_expired_reports()}
    return jsonify(
        {
            "success": True,
            "data": {"artifacts": artifact_result, "jobs": job_result, "reports": report_result},
            "error": None,
            "meta": {"schema_version": "2.0"},
        }
    )


@app.route("/api/v2/ops-dashboard")
def ops_dashboard_data():
    data = build_ops_dashboard(
        request.args.get("days", 30),
        request.args.get("start"),
        request.args.get("end"),
        request.args.get("exclude_ip"),
        request.args.get("refresh") in {"1", "true", "yes"},
    )
    response = jsonify({"success": True, "data": data, "error": None, "meta": {"schema_version": "2.0"}})
    return _noindex_response(response)


@app.route("/api/v2/ops-events", methods=["POST"])
def ops_event():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        try:
            payload = json.loads(request.get_data(as_text=True) or "{}")
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    recorded = record_client_event(request, payload.get("event", ""), payload.get("meta") if isinstance(payload.get("meta"), dict) else {})
    response = jsonify({"success": bool(recorded), "error": None, "meta": {"schema_version": "2.0"}})
    return _noindex_response(response)


@app.route("/health")
def health():
    return jsonify({"success": True, "status": "ok"})


def _public_job(job: dict) -> dict:
    return {
        key: value
        for key, value in job.items()
        if key not in {"payload", "lease_token", "lease_owner", "lease_expires_at"}
    }


def _public_analysis_record(record: dict) -> dict:
    return {
        key: value
        for key, value in record.items()
        if key not in {"subtitle_filename", "subtitle_original_filename"}
    }


ensure_directories()


@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"success": False, "message": "文件过大，最大允许 2MB"}), 413


@app.errorhandler(Exception)
def handle_error(error):
    if isinstance(error, HTTPException):
        return error
    log_error(f"应用错误: {error}")
    return jsonify({"success": False, "message": "服务器错误，请稍后再试"}), 500


@app.errorhandler(404)
def not_found_error(error):
    app.logger.error("Page not found: %s", request.url)
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    app.logger.error("Server Error: %s", error)
    return render_template("500.html"), 500


application = app
