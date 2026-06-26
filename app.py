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
from urllib.parse import parse_qsl, quote, urlencode, urlparse

import requests
from flask import Flask, Response, g, jsonify, render_template, request, send_from_directory, stream_with_context
from werkzeug.exceptions import Forbidden, HTTPException

from config import get_analysis_config, get_app_access_token
from danmaku_backend.analysis.ai_analyzer import AIAnalyzer
from danmaku_backend.analysis.comment_analysis import CommentJointAnalysis
from danmaku_backend.analysis.deep_analysis import DeepAnalysis
from danmaku_backend.analysis.full_analysis import FullComprehensiveAnalysis
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
ENGLISH_SITE_BASE_URL = "https://en.danmu.liu-qi.cn"
ENGLISH_HOSTS = {"en.danmu.liu-qi.cn"}
PLUGIN_STORE_VERSION = "1.0.4"
PLUGIN_DOWNLOAD_VERSION = "1.0.4"
PLUGIN_DOWNLOAD_FILENAME = "bili-lite-subtitle-1.0.4.zip"
PLUGIN_DOWNLOAD_PATH = f"/static/plugin/{PLUGIN_DOWNLOAD_FILENAME}"
PLUGIN_CHROME_WEB_STORE_URL = "https://chromewebstore.google.com/detail/b-%E7%AB%99%E8%BD%BB%E9%87%8F%E5%AD%97%E5%B9%95%E5%8A%A9%E6%89%8B/ifhokpfhemfpnmgoajodifgamfbhioga"
PLUGIN_GITHUB_URL = "https://github.com/Liu-Bot24/bili-lite-subtitle"
COMMENT_PLUGIN_STORE_VERSION = "1.0.0"
COMMENT_PLUGIN_DOWNLOAD_VERSION = "1.0.0"
COMMENT_PLUGIN_DOWNLOAD_FILENAME = "bilibili-comment-capture-1.0.0.zip"
COMMENT_PLUGIN_DOWNLOAD_PATH = f"/static/plugin/{COMMENT_PLUGIN_DOWNLOAD_FILENAME}"
COMMENT_PLUGIN_CHROME_WEB_STORE_URL = "https://chromewebstore.google.com/detail/bilibili-comment-helper-c/emfgghgdlmihojemgbgljgibiagiohdj"
COMMENT_PLUGIN_GITHUB_URL = "https://github.com/Liu-Bot24/bilibili-comment-capture"
INDEXNOW_KEY = "782ce4166c93b3da40b54acae9b34686"
SOGOU_SITE_VERIFICATION = "d5smJSzPUQ"
SITEMAP_PAGES = (
    {"path": "/", "lastmod": "2026-04-29", "changefreq": "weekly", "priority": "1.0"},
    {"path": "/plugin", "lastmod": "2026-06-11", "changefreq": "monthly", "priority": "0.8"},
    {"path": "/faq", "lastmod": "2026-04-29", "changefreq": "monthly", "priority": "0.8"},
)
set_job_event_writer(default_job_store.add_event)


def _request_language() -> str:
    lang = (request.args.get("lq_lang") or "").strip().lower()
    if lang == "en":
        return "en"
    host = (request.host or "").split(":", 1)[0].lower()
    return "en" if host in ENGLISH_HOSTS else "zh"


def _request_host() -> str:
    return (request.host or "").split(":", 1)[0].lower()


def _request_site() -> str:
    return "en" if _request_language() == "en" else "cn"


def _site_base_url(language: str | None = None) -> str:
    return ENGLISH_SITE_BASE_URL if (language or _request_language()) == "en" else SITE_BASE_URL


def _alternate_url(path: str, language: str) -> str:
    base_url = ENGLISH_SITE_BASE_URL if language == "en" else SITE_BASE_URL
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base_url}{path}"


def _alternate_links(path: str) -> list[dict[str, str]]:
    return [
        {"hreflang": "zh-CN", "href": _alternate_url(path, "zh")},
        {"hreflang": "en", "href": _alternate_url(path, "en")},
        {"hreflang": "x-default", "href": _alternate_url(path, "zh")},
    ]


def _language_switch_links() -> dict[str, str]:
    path = request.path or "/"
    query_items = [
        (key, value)
        for key, value in parse_qsl(request.query_string.decode("utf-8"), keep_blank_values=True)
        if key.lower() != "lq_lang"
    ]
    query = urlencode(query_items, doseq=True)
    suffix = f"{path}?{query}" if query else path
    return {
        "zh_url": f"{SITE_BASE_URL}{suffix}",
        "en_url": f"{ENGLISH_SITE_BASE_URL}{suffix}",
    }


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

FAQ_CONTENT_EN = [
    {
        "category": "Basics",
        "items": [
            {
                "question": "Is this site free?",
                "answer": "Yes. Xiaoliu BOT danmuku is currently a free public tool. Danmaku download, charts, and AI analysis can be used without a paid plan. During busy periods, built-in AI analysis may queue, time out, or fail because it depends on server load and upstream model availability.",
            },
            {
                "question": "Which Bilibili videos are supported?",
                "answer": "The site supports public Bilibili videos that can be accessed normally. You can paste a BV ID, a standard video URL, or a URL with query parameters. Common inputs include BV1w7411Y77t and https://www.bilibili.com/video/BV1w7411Y77t.",
            },
            {
                "question": "Can I paste a full video link instead of a BV ID?",
                "answer": "Yes. Paste the Bilibili video page URL into the search box and the site will extract the BV ID automatically. A full video page URL usually gives the best success rate.",
            },
        ],
    },
    {
        "category": "Danmaku Download",
        "items": [
            {
                "question": "Which download formats are available?",
                "answer": "After parsing, you can download CSV and TXT files. CSV keeps richer danmaku fields for spreadsheet filtering, statistics, and further analysis. TXT is lighter and easier to read, copy, or pass into text workflows.",
            },
            {
                "question": "What is included in the downloaded files?",
                "answer": "The files include danmaku text and timing information. TXT is simplified for reading and AI analysis, while CSV is better for complete field-level statistics and custom filtering.",
            },
        ],
    },
    {
        "category": "Charts",
        "items": [
            {
                "question": "What are the density chart and word cloud useful for?",
                "answer": "The density chart helps locate high-interaction moments in a video. The word cloud summarizes recurring audience keywords. Together they are useful for video review, topic research, and audience-response analysis.",
            },
            {
                "question": "Why do some videos have fewer stats?",
                "answer": "The available data depends on what can be publicly fetched. New videos, low-danmaku videos, access restrictions, or temporary upstream issues may reduce the data shown here.",
            },
        ],
    },
    {
        "category": "AI Analysis",
        "items": [
            {
                "question": "What is the difference between danmaku analysis and subtitle deep analysis?",
                "answer": "Danmaku analysis uses comment data to summarize audience attitude, interaction patterns, and hot topics. Subtitle deep analysis combines an uploaded TXT subtitle with danmaku data, which is better for reviewing structure, key moments, and content strategy.",
            },
            {
                "question": "Can I use AI analysis without subtitles?",
                "answer": "Yes. Danmaku content analysis works without subtitles. Subtitle deep analysis requires a TXT subtitle file because it reads both the subtitle text and the danmaku data.",
            },
        ],
    },
    {
        "category": "Custom API and Privacy",
        "items": [
            {
                "question": "Will my custom API key be saved on the server?",
                "answer": "No. Your custom Base URL, model name, and API key are stored only in your browser's local storage. When custom API mode is enabled, the browser calls your OpenAI-compatible endpoint directly. Clearing browser data, switching devices, or using private browsing can remove the configuration.",
            },
            {
                "question": "What is the difference between built-in AI and custom API mode?",
                "answer": "Built-in AI uses the server-side model configuration and requires no key from you, but tasks share the public queue. With custom API mode, the server prepares danmaku, subtitle text, and prompts, while your browser calls the endpoint you configured. This can improve model choice, speed, queueing, context length, and long-video capacity if your endpoint supports it.",
            },
        ],
    },
    {
        "category": "Sharing and Troubleshooting",
        "items": [
            {
                "question": "What does a shared report link save?",
                "answer": "A shared report saves a structured snapshot of the current result page so video info, charts, and AI analysis can be reopened later. Raw danmaku files and uploaded subtitle text are not kept as long-term public report data.",
            },
            {
                "question": "Why can a shared report link expire?",
                "answer": "Shared reports have a retention period, currently about 30 days depending on site configuration. For long-term sharing, keep the standard result URL as well, such as /result?bvid=BV_ID.",
            },
        ],
    },
]

ENGLISH_UI_REPLACEMENTS = (
    ("小刘BOT danmuku", "Xiaoliu BOT danmuku"),
    ("小刘BOT", "Xiaoliu BOT"),
    ("跳到常见问题", "Skip to FAQ"),
    ("跳到插件下载", "Skip to plugin download"),
    ("跳到弹幕查询工具", "Skip to danmaku tool"),
    (">开始<", ">Start<"),
    (">插件<", ">Plugin<"),
    (">博客<", ">Blog<"),
    ("站点导航", "Site navigation"),
    ("常见问题", "Frequently Asked Questions"),
    ("关于 B站弹幕查询、下载、统计图表、AI 分析和分享报告的使用说明。", "Usage notes for Bilibili danmaku search, downloads, charts, AI analysis, and shared reports."),
    ("bilibili轻量字幕助手", "bilibili Lite Subtitle Assistant"),
    ("适用于 Chrome 和 Chromium 内核浏览器，可辅助获取用于本站「字幕深度分析」功能的视频字幕。<br>也可独立使用，在 B 站视频页查看、搜索、复制、下载和翻译可用字幕，支持浮窗字幕，并支持双击字幕跳转到对应播放时间。", "A Chrome and Chromium extension for collecting subtitles used by this site's subtitle deep analysis.<br>It also works standalone on Bilibili video pages: view, search, copy, download, and translate subtitles, use an overlay subtitle window, and double-click a line to jump to that timestamp."),
    ("bilibili评论导出助手", "bilibili Comment Export Assistant"),
    ("适用于 Chrome 和 Chromium 内核浏览器，可辅助获取用于本站「评论联合分析」功能的视频评论。<br>也可独立使用，在 B 站视频页抓取、搜索、复制、下载评论，支持置顶评论、一级评论、楼中楼回复，并可导出 Markdown 和 JSON 文件。", "A Chrome and Chromium extension for collecting video comments used by this site's comment joint analysis.<br>It also works standalone on Bilibili video pages: capture, search, copy, and download comments, including pinned comments, top-level comments, and nested replies, with Markdown and JSON export."),
    ("插件安装、下载与项目地址", "Plugin install, download, and project links"),
    ("Chrome 商店", "Chrome Web Store"),
    ("从 Chrome Web Store 一键安装，后续自动更新。", "Install from the Chrome Web Store and receive automatic updates."),
    ("下载插件包", "Download Package"),
    ("下载后解压，在 Chrome 扩展程序页面加载解压后的文件夹。", "Download, unzip, then load the extracted folder on Chrome's extensions page."),
    ("GitHub 仓库", "GitHub Repository"),
    ("源码", "Source"),
    ("查看源码、README 和本地安装说明。", "View source code, README, and local install notes."),
    ("手动安装教程", "Manual Installation"),
    ("使用教程", "How to Use"),
    ("下载并解压插件包", "Download and unzip the package"),
    ("点击上方“下载插件包”，把压缩包解压到一个固定目录，后续不要随意移动这个目录。", "Click “Download Package” above, unzip the archive to a fixed folder, and avoid moving that folder later."),
    ("打开扩展程序页面", "Open the extensions page"),
    ("在地址栏输入 ", "Type "),
    ("，进入扩展程序页面。", " in the address bar to open the extensions page."),
    ("加载未打包的扩展程序", "Load the unpacked extension"),
    ("打开开发者模式，点击“加载未打包的扩展程序”，选择刚才解压出来、包含 ", "Turn on Developer mode, click “Load the unpacked extension”, then choose the extracted folder that contains "),
    (" 的文件夹。", "."),
    ("Chrome 扩展程序页面中的加载未打包的扩展程序按钮", "Chrome extensions page with the Load unpacked button"),
    ("确认加载成功", "Confirm the extension is loaded"),
    ("加载成功后，扩展程序列表中会出现哔哩哔哩轻量字幕助手。", "Once loaded, bilibili Lite Subtitle Assistant will appear in your extensions list."),
    ("哔哩哔哩轻量字幕助手已加载到 Chrome 扩展程序列表", "bilibili Lite Subtitle Assistant loaded in the Chrome extensions list"),
    ("在视频页查看字幕", "View subtitles on a video page"),
    ("打开任意 B 站视频页，等待页面加载完成后，在右侧弹幕区域查看字幕列表。", "Open any Bilibili video page, wait for it to load, then view the subtitle list in the side panel."),
    ("查看、翻译或下载字幕", "View, translate, or download subtitles"),
    ("选择字幕语言；需要翻译时配置模型并点击“翻译为”；也可以点击“下载 TXT”保存字幕文件，或点击“AI分析”打开本站字幕深度分析页面。", "Choose a subtitle language; configure a model and click “Translate to” when translation is needed; you can also click “Download TXT” to save subtitles or click “AI Analysis” to open this site's subtitle deep analysis page."),
    ("B 站视频页中的字幕助手面板", "Subtitle assistant panel on a Bilibili video page"),
    ("上传字幕并开始深度分析", "Upload subtitles and start deep analysis"),
    ("向下滑动到字幕深度分析卡片，点击“上传字幕文件”，选择刚才下载的 TXT 字幕文件，然后点击“开始深度分析”。", "Scroll to the Subtitle Deep Analysis card, click “Upload Subtitle File”, choose the TXT subtitle file you just downloaded, then click “Start Deep Analysis”."),
    ("AI 弹幕分析页面中的字幕深度分析上传入口", "Subtitle Deep Analysis upload area on the AI danmaku analysis page"),
    ("加载成功后，扩展程序列表中会出现 bilibili评论导出助手。", "Once loaded, bilibili Comment Export Assistant will appear in your extensions list."),
    ("在视频页抓取评论", "Capture comments on a video page"),
    ("打开任意 B 站视频页，等待页面加载完成后，使用插件识别当前视频并抓取评论。", "Open any Bilibili video page, wait for it to load, then use the extension to identify the current video and capture comments."),
    ("搜索、复制或下载评论", "Search, copy, or download comments"),
    ("按需要选择评论排序和回复抓取方式；可以搜索关键词、复制评论文本，也可以导出 Markdown 或 JSON 文件。", "Choose the comment order and reply capture mode as needed; you can search keywords, copy comment text, or export Markdown or JSON files."),
    ("上传评论并开始联合分析", "Upload comments and start joint analysis"),
    ("向下滑动到评论联合分析卡片，点击“上传评论 JSON”，选择刚才导出的 JSON 文件，然后点击“开始联合分析”。", "Scroll to the Comment Joint Analysis card, click “Upload Comment JSON”, choose the JSON file you just exported, then click “Start Joint Analysis”."),
    ("如需字幕文件可通过 ", "Need a subtitle file? Use the "),
    ("如需评论文件可通过 ", "Need a comment file? Use the "),
    ("本站提供的插件", "site plugin"),
    (" 获取；", ". "),
    (" 获取。", "."),
    ("弹幕解析结果", "Danmaku Analysis Result"),
    ("提供弹幕文件下载和AI智能分析。", "Download danmaku files and run AI-assisted analysis."),
    ("输入新的 BV 号或视频链接，可以重新解析。", "Enter a new BV ID or video URL to parse another video."),
    ("B站弹幕查询、", "Bilibili Danmaku"),
    ("解析与下载", "Search, Download and Analyze"),
    ("输入 BV 号或视频链接，下载弹幕文件，并查看统计和内容分析。", "Paste a BV ID or Bilibili video URL to download danmaku files, view charts, and analyze audience reactions."),
    ("适合弹幕下载、弹幕解析、视频复盘和内容研究。", "Useful for danmaku downloads, video review, audience research, and content analysis."),
    ("B站弹幕查询表单", "Bilibili danmaku search form"),
    ("开始查询", "Start Search"),
    ("支持 BV 号和完整视频链接", "Supports BV IDs and full video links"),
    ("请输入视频 BV 号或 Bilibili 视频链接", "Enter a BV ID or Bilibili video URL"),
    ("粘贴 BV 号或 Bilibili 视频链接", "Paste a BV ID or Bilibili video URL"),
    ("重新解析", "Parse Again"),
    ("解析弹幕", "Parse Danmaku"),
    ("支持格式", "Supported formats"),
    ("带参数的视频链接", "Video URLs with query parameters"),
    ("正在解析弹幕数据，请稍候...", "Parsing danmaku data, please wait..."),
    ("视频基本信息", "Video Information"),
    ("视频互动数据", "Video engagement stats"),
    ("播放", "Views"),
    ("弹幕", "Danmaku"),
    ("点赞", "Likes"),
    ("投币", "Coins"),
    ("收藏", "Favorites"),
    ("分享", "Shares"),
    ("视频简介", "Description"),
    ("弹幕统计图表", "Danmaku charts"),
    ("弹幕密度分布", "Danmaku Density"),
    ("弹幕词云", "Danmaku Word Cloud"),
    ("弹幕高频词云", "High-frequency danmaku word cloud"),
    ("发送日期分布", "Send Date Distribution"),
    ("发送时间分布", "Send Time Distribution"),
    ("AI 弹幕分析", "AI Danmaku Analysis"),
    ("自主配置大模型 API", "Custom Model API"),
    ("未配置", "Not configured"),
    ("模型配置", "Model Settings"),
    ("兼容 OpenAI Chat Completions 格式", "Compatible with the OpenAI Chat Completions format"),
    ("示例：", "Example: "),
    ("例如：", "Example: "),
    ("获取 API Key", "Get API Key"),
    ("采样配置", "Sampling Settings"),
    ("采样方式", "Sampling mode"),
    ("全量采样", "Full sampling"),
    ("均衡采样", "Balanced sampling"),
    ("普通分析最大样本数", "Max samples for standard analysis"),
    ("深度分析最大样本数", "Max samples for deep analysis"),
    ("开头保留样本数", "Opening samples to keep"),
    ("高峰数量", "Number of peaks"),
    ("高峰窗口秒数", "Peak window in seconds"),
    ("每个高峰采样数", "Samples per peak"),
    ("你的自定义 API 配置仅保存在当前浏览器的本地存储中。", "Your custom API settings are stored only in this browser's local storage."),
    ("我们的服务器不会保存你的 API Key。清理浏览器数据、切换设备或无痕模式下，这些配置可能失效，请妥善保管你的 Key。", "The server does not save your API key. Browser cleanup, device changes, or private browsing can remove these settings, so keep your key safe."),
    ("保存", "Save"),
    ("清空", "Clear"),
    ("通过本地浏览器调用 / 不进入服务器队列 / 自定义上下文长度和采样模式", "Called from your browser / no server queue / custom context length and sampling mode"),
    ("AI弹幕分析方式", "AI danmaku analysis mode"),
    ("弹幕内容分析", "Danmaku Content Analysis"),
    ("字幕深度分析", "Subtitle Deep Analysis"),
    ("基于当前弹幕数据生成内容概览、观众态度、互动特征与热点话题。", "Generate content overview, audience attitude, interaction patterns, and hot topics from current danmaku data."),
    ("分析弹幕内容", "Analyze Danmaku"),
    ("上传字幕文本后，结合弹幕与字幕生成结构拆解、关键时刻和内容建议。", "Upload subtitle text, then combine subtitles with danmaku to produce structure breakdowns, key moments, and content suggestions."),
    ("上传字幕文件", "Upload Subtitle File"),
    ("开始深度分析", "Start Deep Analysis"),
    ("处理状态", "Processing Status"),
    ("B站弹幕查询、解析、下载和分析工具。", "Bilibili danmaku search, download, and analysis tool."),
    ("请作者喝杯咖啡赞赏码", "Coffee support QR code"),
    ("关闭分享卡片预览", "Close share card preview"),
    ("请作者喝杯咖啡", "Buy the author a coffee"),
    ("关于", "About"),
    ("关闭", "Close"),
    ("弹幕解析结果分享卡片预览", "Danmaku analysis share card preview"),
    ("弹幕解析结果分享卡片", "Danmaku analysis share card"),
    ("复制链接", "Copy Link"),
    ("扫码可直接打开当前分享", "Scan to open this share"),
    ("下载分享卡片", "Download share card"),
    ("分享卡片", "Share card"),
    ("下载图片", "Download image"),
    ("Bilibili弹幕解析下载", "Bilibili Danmaku Analysis"),
    ("AI 弹幕分析", "AI Danmaku Analysis"),
    ("暂无内容", "No content yet"),
    ("暂无数据", "No data"),
    ("暂无建议", "No suggestions"),
    ("分享报告", "Share Report"),
    ("保留当前分析结果，生成一个可分享的结果页链接。", "Save the current analysis result and generate a shareable result-page link."),
    ("报告将在服务器存储 ${retentionDays()} 天，到期后链接会失效。", "The report will be stored on the server for ${retentionDays()} days. The link expires afterward."),
    ("正在生成分享卡片", "Generating share card"),
    ("二维码生成失败", "QR code generation failed"),
    ("二维码暂时不可用", "QR code is temporarily unavailable"),
    ("分享卡片暂时不可用", "Share card is temporarily unavailable"),
    ("仍在生成分享卡片，请稍候...", "Still generating share card, please wait..."),
    ("分享卡片生成超时", "Share card generation timed out"),
    ("分享链接已复制", "Share link copied"),
    ("图片生成失败", "Image generation failed"),
    ("复制失败，请手动复制链接", "Copy failed. Please copy the link manually."),
    ("自动复制失败，请手动复制链接", "Automatic copy failed. Please copy the link manually."),
    ("成功下载", "Downloaded"),
    ("成功获取", "Fetched"),
    ("已载入分享报告。若需重新获取弹幕文件或重新分析，请点击上方重新解析。", "Shared report loaded. To fetch danmaku files again or rerun analysis, click Parse Again above."),
    ("正在载入弹幕分析报告...", "Loading danmaku analysis report..."),
    ("正在载入深度分析报告...", "Loading deep analysis report..."),
    ("分享报告已过期或不存在", "The shared report has expired or does not exist"),
    ("当前页面是分享报告，请先点击重新解析，获取当前弹幕文件后再继续分析。", "This is a shared report. Click Parse Again first to fetch current danmaku files before continuing analysis."),
    ("正在生成分享链接...", "Generating share link..."),
    ("分享报告保存失败，请稍后重试", "Failed to save the shared report. Please try again later."),
    ("分享报告保存失败", "Failed to save the shared report"),
    ("连接失败", "Connection failed"),
    ("自定义接口调用失败", "Custom endpoint call failed"),
    ("连接测试超时，请检查接口地址或网络状态", "Connection test timed out. Check the endpoint URL or network."),
    ("测试中", "Testing"),
    ("测试失败", "Test failed"),
    ("保存失败", "Save failed"),
    ("请输入有效的BV号或视频链接", "Enter a valid BV ID or video URL"),
    ("弹幕获取失败，请稍后重试", "Failed to fetch danmaku. Please try again later."),
    ("下载失败：", "Download failed: "),
    ("任务状态获取失败，请稍后重试", "Failed to get task status. Please try again later."),
    ("任务状态获取失败", "Failed to get task status"),
    ("任务执行失败", "Task failed"),
    ("分析材料准备失败，请稍后再试", "Failed to prepare analysis materials. Please try again later."),
    ("分析材料准备失败", "Failed to prepare analysis materials"),
    ("自定义接口未配置或未通过连通测试", "Custom endpoint is not configured or has not passed the connection test"),
    ("当前正在使用自主配置大模型 API", "Using your custom model API"),
    ("正在整理弹幕样本...", "Preparing danmaku samples..."),
    ("正在调用自定义文本分析服务...", "Calling the custom text analysis service..."),
    ("自定义接口已返回结果，正在整理弹幕分析报告...", "Custom endpoint returned results. Preparing danmaku analysis report..."),
    ("弹幕分析报告已生成", "Danmaku analysis report generated"),
    ("正在整理字幕与弹幕样本...", "Preparing subtitle and danmaku samples..."),
    ("自定义接口已返回结果，正在整理深度分析报告...", "Custom endpoint returned results. Preparing deep analysis report..."),
    ("深度分析报告已生成", "Deep analysis report generated"),
    ("正在分析中...", "Analyzing..."),
    ("内容分析请求失败，请稍后重试", "Content analysis request failed. Please try again later."),
    ("文本分析服务已返回结果，正在整理弹幕分析报告...", "Text analysis service returned results. Preparing danmaku analysis report..."),
    ("分析失败", "Analysis failed"),
    ("内容分析失败：", "Content analysis failed: "),
    ("正在上传...", "Uploading..."),
    ("字幕上传失败，请稍后重试", "Subtitle upload failed. Please try again later."),
    ("上传失败：", "Upload failed: "),
    ("正在进行深度分析...", "Running deep analysis..."),
    ("深度分析请求失败，请稍后重试", "Deep analysis request failed. Please try again later."),
    ("文本分析服务已返回结果，正在整理深度分析报告...", "Text analysis service returned results. Preparing deep analysis report..."),
    ("深度分析失败：", "Deep analysis failed: "),
)


def _video_cover_proxy_url(bvid: str) -> str:
    return f"{_site_base_url()}/api/v2/video-cover/{bvid}.jpg"


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
            "site": _request_site(),
            "host": _request_host(),
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


def _latest_record_with_comment(bvid: str) -> dict | None:
    for record in default_store.records_for_bvid(bvid):
        if default_store.latest_comment_export(bvid, record.get("analysis_id")):
            return record
    return None


def _prepare_comment_analysis_bundle(
    bvid: str,
    analysis_id: str | None,
    analysis_options: dict,
) -> dict:
    danmaku_path = default_store.latest_danmaku_txt(bvid, analysis_id)
    if not danmaku_path:
        raise FileNotFoundError("未找到弹幕文件，请重新下载")
    comment_export = default_store.latest_comment_export(bvid, analysis_id)
    if not comment_export:
        raise FileNotFoundError("未找到评论 JSON，请重新上传")
    CommentJointAnalysis.validate_comment_export(comment_export, bvid)
    video_info = get_video_info(bvid)
    if not video_info:
        raise RuntimeError("获取视频信息失败")
    danmaku_list = AIAnalyzer.parse_danmaku_txt(str(danmaku_path))
    if not danmaku_list:
        raise RuntimeError("读取弹幕文件失败")
    analysis_result = analyze_danmaku(danmaku_list)
    bundle = CommentJointAnalysis.prepare_comment_request(
        video_info,
        danmaku_list,
        analysis_result["time_density"],
        comment_export,
        analysis_options=analysis_options,
        with_logs=False,
    )
    return {
        "prompt": bundle["prompt"],
        "sample_mode": bundle["sample_mode"],
        "sample_count": bundle["sample_count"],
        "peak_count": bundle["peak_count"],
        "comment_summary": bundle["comment_summary"],
        "analysis_id": analysis_id,
        "bvid": bvid,
    }


def _latest_record_with_full_materials(bvid: str) -> dict | None:
    for record in default_store.records_for_bvid(bvid, require_subtitle=True):
        analysis_id = record.get("analysis_id")
        if default_store.latest_comment_export(bvid, analysis_id):
            return record
    return None


def _prepare_full_analysis_bundle(
    bvid: str,
    analysis_id: str | None,
    analysis_options: dict,
) -> dict:
    subtitle_path = default_store.latest_subtitle(bvid, analysis_id)
    if not subtitle_path:
        raise FileNotFoundError("未找到字幕文件，请重新上传")
    danmaku_path = default_store.latest_danmaku_txt(bvid, analysis_id)
    if not danmaku_path:
        raise FileNotFoundError("未找到弹幕文件，请重新下载")
    comment_export = default_store.latest_comment_export(bvid, analysis_id)
    if not comment_export:
        raise FileNotFoundError("未找到评论 JSON，请重新上传")
    CommentJointAnalysis.validate_comment_export(comment_export, bvid)
    subtitle_content = subtitle_path.read_text(encoding="utf-8")
    video_info = get_video_info(bvid)
    if not video_info:
        raise RuntimeError("获取视频信息失败")
    danmaku_list = AIAnalyzer.parse_danmaku_txt(str(danmaku_path))
    if not danmaku_list:
        raise RuntimeError("读取弹幕文件失败")
    analysis_result = analyze_danmaku(danmaku_list)
    bundle = FullComprehensiveAnalysis.prepare_full_request(
        video_info,
        subtitle_content,
        danmaku_list,
        analysis_result["time_density"],
        comment_export,
        analysis_options=analysis_options,
        with_logs=False,
    )
    return {
        "prompt": bundle["prompt"],
        "sample_mode": bundle["sample_mode"],
        "sample_count": bundle["sample_count"],
        "peak_count": bundle["peak_count"],
        "comment_summary": bundle["comment_summary"],
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
    return Response("", status=301, headers={"Location": f"{_site_base_url()}/plugin"})


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
    site_base_url = _site_base_url()
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
            "Disallow: /upload_comments",
            "Disallow: /analyze_content",
            "Disallow: /deep_analysis",
            "Disallow: /comment_analysis",
            "Disallow: /subtitles/",
            "",
            f"Sitemap: {site_base_url}/sitemap.xml",
            "",
        ]
    )
    return Response(content, content_type="text/plain; charset=utf-8")


@app.route("/sitemap.xml")
def sitemap_xml():
    site_base_url = _site_base_url()
    url_blocks = []
    for page in SITEMAP_PAGES:
        loc = escape(f"{site_base_url}{page['path']}", quote=True)
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
    language = _request_language()
    site_base_url = _site_base_url(language)
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
        language=language,
        site_base_url=site_base_url,
    )
    html = render_template(
        template_name,
        client_csrf_token=csrf_token,
        analysis_config=get_analysis_config(),
        report_config=default_report_store.public_config(),
        faq_items=FAQ_CONTENT_EN if language == "en" else FAQ_CONTENT,
        plugin_info={
            "store_version": PLUGIN_STORE_VERSION,
            "download_version": PLUGIN_DOWNLOAD_VERSION,
            "store_url": PLUGIN_CHROME_WEB_STORE_URL,
            "github_url": PLUGIN_GITHUB_URL,
            "download_url": f"{site_base_url}{PLUGIN_DOWNLOAD_PATH}",
            "download_filename": PLUGIN_DOWNLOAD_FILENAME,
        },
        comment_plugin_info={
            "store_version": COMMENT_PLUGIN_STORE_VERSION,
            "download_version": COMMENT_PLUGIN_DOWNLOAD_VERSION,
            "store_url": COMMENT_PLUGIN_CHROME_WEB_STORE_URL,
            "github_url": COMMENT_PLUGIN_GITHUB_URL,
            "download_url": f"{site_base_url}{COMMENT_PLUGIN_DOWNLOAD_PATH}",
            "download_filename": COMMENT_PLUGIN_DOWNLOAD_FILENAME,
        },
        report_preview=report_preview,
        report_video_info=report_video_info,
        is_result_page=is_result_page,
        is_faq_page=is_faq_page,
        is_plugin_page=is_plugin_page,
        initial_bvid=render_initial_bvid,
        initial_report_id=initial_report_id,
        seo=seo_context,
    )
    if language == "en":
        html = _apply_english_html_overlay(html)
    response = app.make_response(html)
    response.set_cookie(
        _CSRF_COOKIE,
        csrf_token,
        max_age=24 * 60 * 60,
        secure=request.is_secure,
        samesite="Strict",
        httponly=False,
    )
    return response


def _apply_english_html_overlay(html: str) -> str:
    for source, target in sorted(ENGLISH_UI_REPLACEMENTS, key=lambda item: len(item[0]), reverse=True):
        html = html.replace(source, target)
    return html


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
    *,
    language: str = "zh",
    site_base_url: str = SITE_BASE_URL,
) -> dict:
    is_english = language == "en"
    home_title = (
        "Bilibili Danmaku Viewer, Downloader and AI Analysis Tool | Xiaoliu BOT danmuku"
        if is_english
        else "B站弹幕查询、解析、下载与AI分析工具｜小刘BOT danmuku"
    )
    home_description = (
        "Paste a BV ID or Bilibili video URL to fetch video info, download danmaku CSV/TXT, view charts, and run AI audience analysis."
        if is_english
        else "输入 BV 号或 Bilibili 视频链接，查询视频信息，下载弹幕 CSV/TXT，并查看词云、时间分布和 AI 弹幕分析。"
    )
    home_keywords = (
        "Bilibili danmaku viewer,Bilibili comments downloader,danmaku download,BV danmaku search,danmaku word cloud,AI danmaku analysis,Bilibili audience analysis"
        if is_english
        else "B站弹幕查询,Bilibili弹幕解析,B站弹幕下载,弹幕分析,BV号弹幕查询,B站视频弹幕导出,弹幕词云,AI弹幕分析,免费弹幕查询,免费弹幕下载,免费B站弹幕查询,免费B站弹幕下载,免费Bilibili弹幕查询,免费Bilibili弹幕下载"
    )
    home_url = f"{site_base_url}/"
    default_share_image_url = f"{site_base_url}/static/og-default.jpg"
    share_image_url = default_share_image_url
    context = {
        "title": home_title,
        "description": home_description,
        "keywords": home_keywords,
        "canonical_url": home_url,
        "og_url": home_url,
        "image_url": share_image_url,
        "lang": "en" if is_english else "zh-CN",
        "locale": "en_US" if is_english else "zh_CN",
        "site_name": "Xiaoliu BOT danmuku" if is_english else "小刘BOT danmuku",
        "alternates": _alternate_links("/"),
        "language_switch": _language_switch_links(),
        "robots": "index,follow",
        "bvid": "",
        "video_title": "",
        "structured_data": {
            "@context": "https://schema.org",
            "@type": "WebApplication",
            "name": "Xiaoliu BOT danmuku" if is_english else "小刘BOT danmuku",
            "url": home_url,
            "applicationCategory": "UtilityApplication",
            "operatingSystem": "Web",
            "description": (
                "A Bilibili danmaku search, download, charting, and AI analysis tool for BV IDs and Bilibili video URLs."
                if is_english
                else "B站弹幕查询、解析、下载和 AI 分析工具，支持 BV 号与 Bilibili 视频链接。"
            ),
            "inLanguage": "en-US" if is_english else "zh-CN",
        },
    }
    if is_plugin_page:
        plugin_url = f"{site_base_url}/plugin"
        plugin_title = (
            "bilibili Lite Subtitle Assistant and Comment Export Assistant | Xiaoliu BOT danmuku"
            if is_english
            else "bilibili轻量字幕助手与评论导出助手｜B站字幕评论Chrome插件｜小刘BOT danmuku"
        )
        plugin_description = (
            "Chrome and Chromium extensions for exporting Bilibili subtitles and comments, then importing them into Xiaoliu BOT danmuku for subtitle deep analysis and comment joint analysis."
            if is_english
            else "面向 Chrome 和 Chromium 浏览器的 B站字幕与评论导出插件，可在视频页下载字幕、抓取评论，并导入小刘BOT danmuku 进行字幕深度分析和评论联合分析。"
        )
        plugin_keywords = (
            "Bilibili subtitle extension,Bilibili comment export,Bilibili comment downloader,Chrome subtitle extension,bilibili Lite Subtitle Assistant,AI subtitle analysis"
            if is_english
            else "哔哩哔哩字幕插件,B站字幕下载,B站评论导出,Bilibili评论下载,B站字幕插件,Chrome字幕插件,Chrome扩展,浏览器插件,字幕助手,评论助手,AI弹幕分析,小刘BOT danmuku"
        )
        context.update(
            {
                "title": plugin_title,
                "description": plugin_description,
                "keywords": plugin_keywords,
                "canonical_url": plugin_url,
                "og_url": plugin_url,
                "image_url": default_share_image_url,
                "alternates": _alternate_links("/plugin"),
                "robots": "index,follow",
                "structured_data": {
                    "@context": "https://schema.org",
                    "@type": "CollectionPage",
                    "name": plugin_title,
                    "url": plugin_url,
                    "description": plugin_description,
                    "inLanguage": "en-US" if is_english else "zh-CN",
                    "publisher": {
                        "@type": "Organization",
                        "name": "Xiaoliu BOT danmuku" if is_english else "小刘BOT danmuku",
                        "url": site_base_url,
                    },
                    "isPartOf": {
                        "@type": "WebApplication",
                        "name": "Xiaoliu BOT danmuku" if is_english else "小刘BOT danmuku",
                        "url": site_base_url,
                    },
                    "mainEntity": {
                        "@type": "ItemList",
                        "itemListElement": [
                            {
                                "@type": "ListItem",
                                "position": 1,
                                "item": {
                                    "@type": "SoftwareApplication",
                                    "name": "bilibili Lite Subtitle Assistant" if is_english else "bilibili轻量字幕助手",
                                    "url": plugin_url,
                                    "downloadUrl": f"{site_base_url}{PLUGIN_DOWNLOAD_PATH}",
                                    "softwareVersion": PLUGIN_DOWNLOAD_VERSION,
                                    "applicationCategory": "BrowserApplication",
                                    "operatingSystem": "Chrome, Chromium",
                                    "browserRequirements": "Chrome or Chromium browser" if is_english else "Chrome 或 Chromium 内核浏览器",
                                    "description": "Bilibili subtitle export extension for subtitle deep analysis." if is_english else "用于字幕深度分析的 B站字幕导出插件。",
                                    "isAccessibleForFree": True,
                                    "sameAs": PLUGIN_GITHUB_URL,
                                    "offers": {
                                        "@type": "Offer",
                                        "price": "0",
                                        "priceCurrency": "CNY",
                                        "availability": "https://schema.org/InStock",
                                    },
                                },
                            },
                            {
                                "@type": "ListItem",
                                "position": 2,
                                "item": {
                                    "@type": "SoftwareApplication",
                                    "name": "bilibili Comment Export Assistant" if is_english else "bilibili评论导出助手",
                                    "url": plugin_url,
                                    "downloadUrl": f"{site_base_url}{COMMENT_PLUGIN_DOWNLOAD_PATH}",
                                    "softwareVersion": COMMENT_PLUGIN_DOWNLOAD_VERSION,
                                    "applicationCategory": "BrowserApplication",
                                    "operatingSystem": "Chrome, Chromium",
                                    "browserRequirements": "Chrome or Chromium browser" if is_english else "Chrome 或 Chromium 内核浏览器",
                                    "description": "Bilibili comment export extension for comment joint analysis." if is_english else "用于评论联合分析的 B站评论导出插件。",
                                    "isAccessibleForFree": True,
                                    "sameAs": COMMENT_PLUGIN_GITHUB_URL,
                                    "offers": {
                                        "@type": "Offer",
                                        "price": "0",
                                        "priceCurrency": "CNY",
                                        "availability": "https://schema.org/InStock",
                                    },
                                },
                            },
                        ],
                    },
                },
            }
        )
        return context

    if is_faq_page:
        faq_url = f"{site_base_url}/faq"
        faq_title = (
            "FAQ | Free Bilibili Danmaku Search, Download and AI Analysis | Xiaoliu BOT danmuku"
            if is_english
            else "常见问题 FAQ｜免费B站弹幕查询、下载与AI分析｜小刘BOT danmuku"
        )
        faq_description = (
            "Frequently asked questions for Xiaoliu BOT danmuku: Bilibili danmaku search, downloads, CSV/TXT formats, charts, AI analysis, custom APIs, and shared reports."
            if is_english
            else "小刘BOT danmuku 常见问题，了解 B站弹幕查询、Bilibili 弹幕下载、CSV/TXT 格式、弹幕词云、AI 弹幕分析、自主配置 API 和分享报告。"
        )
        faq_keywords = (
            "Bilibili danmaku FAQ,danmaku download help,Bilibili danmaku tutorial,AI danmaku analysis FAQ,CSV danmaku download,TXT danmaku download"
            if is_english
            else "B站弹幕FAQ,Bilibili弹幕下载问题,B站弹幕查询教程,AI弹幕分析FAQ,弹幕词云,CSV弹幕下载,TXT弹幕下载,小刘BOT danmuku,免费弹幕查询,免费弹幕下载,免费B站弹幕查询,免费B站弹幕下载,免费Bilibili弹幕查询,免费Bilibili弹幕下载"
        )
        faq_content = FAQ_CONTENT_EN if is_english else FAQ_CONTENT
        questions = [
            {
                "@type": "Question",
                "name": item["question"],
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": item["answer"],
                },
            }
            for group in faq_content
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
                "alternates": _alternate_links("/faq"),
                "robots": "index,follow",
                "structured_data": {
                    "@context": "https://schema.org",
                    "@type": "FAQPage",
                    "name": "Xiaoliu BOT danmuku FAQ" if is_english else "小刘BOT danmuku 常见问题",
                    "url": faq_url,
                    "description": faq_description,
                    "mainEntity": questions,
                    "inLanguage": "en-US" if is_english else "zh-CN",
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
        title = (
            f"{title_subject} | {bvid} | Bilibili Danmaku Analysis | Xiaoliu BOT danmuku"
            if is_english
            else f"{title_subject}｜{bvid}｜Bilibili弹幕解析下载｜小刘BOT danmuku"
        )
        description = (
            f"Analyze Bilibili danmaku for {video_title} ({bvid}), download CSV/TXT files, and view word cloud, density charts, engagement stats, and AI analysis."
            if is_english
            else f"在线解析《{video_title}》（{bvid}）的 Bilibili 弹幕，下载 CSV/TXT，查看词云、时间分布、互动统计和 AI 弹幕分析。"
        )
        keywords = f"{video_title},{bvid},{home_keywords}"
        canonical_url = f"{site_base_url}/result?bvid={bvid}"
    elif bvid:
        title = (
            f"{bvid} | Bilibili Danmaku Analysis | Xiaoliu BOT danmuku"
            if is_english
            else f"{bvid}｜Bilibili弹幕解析下载｜小刘BOT danmuku"
        )
        description = (
            f"Analyze Bilibili danmaku for {bvid}, download CSV/TXT files, and view word cloud, density charts, engagement stats, and AI analysis."
            if is_english
            else f"在线解析 {bvid} 的 Bilibili 弹幕，下载 CSV/TXT，查看词云、时间分布、互动统计和 AI 弹幕分析。"
        )
        keywords = f"{bvid},{home_keywords}"
        canonical_url = f"{site_base_url}/result?bvid={bvid}"
    else:
        title = (
            "Bilibili Danmaku Analysis Result | Xiaoliu BOT danmuku"
            if is_english
            else "Bilibili弹幕解析结果｜小刘BOT danmuku"
        )
        description = home_description
        keywords = home_keywords
        canonical_url = f"{site_base_url}/result"

    page_data = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "url": canonical_url,
        "description": description,
        "isPartOf": {
            "@type": "WebSite",
            "name": "Xiaoliu BOT danmuku" if is_english else "小刘BOT danmuku",
            "url": site_base_url,
        },
        "inLanguage": "en-US" if is_english else "zh-CN",
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
            "alternates": _alternate_links(f"/result?bvid={bvid}") if bvid else _alternate_links("/result"),
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

    result = get_danmaku(bvid, site=_request_site(), host=_request_host())
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

        default_store.save_subtitle(subtitle_file, bvid, analysis_id, site=_request_site(), host=_request_host())
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


@app.route("/upload_comments", methods=["POST"])
def upload_comments():
    try:
        guard = _guard_post("upload")
        if guard:
            return guard

        if "comments" not in request.files:
            return jsonify({"success": False, "message": "未找到上传文件"}), 400

        analysis_id = (request.form.get("analysis_id") or "").strip() or None
        bvid = extract_bvid(request.form.get("bvid", ""))
        comments_file = request.files["comments"]
        if not comments_file or not bvid:
            return jsonify({"success": False, "message": "参数不完整"}), 400

        original_name = comments_file.filename or ""
        if original_name and not original_name.lower().endswith(".json"):
            return jsonify({"success": False, "message": "仅支持 JSON 评论文件"}), 400
        if not analysis_id:
            record = default_store.latest_record_for_bvid(bvid)
            if record:
                analysis_id = record.get("analysis_id")
        if not analysis_id:
            return jsonify({"success": False, "message": "请先解析弹幕后再上传评论"}), 400

        try:
            export_data = json.loads(comments_file.read().decode("utf-8-sig"))
        except Exception:
            return jsonify({"success": False, "message": "评论 JSON 解析失败"}), 400

        CommentJointAnalysis.validate_comment_export(export_data, bvid)
        default_store.save_comment_export(
            export_data,
            bvid,
            analysis_id,
            site=_request_site(),
            host=_request_host(),
        )
        summary = CommentJointAnalysis.comment_summary(export_data)
        return jsonify(
            {
                "success": True,
                "message": "评论 JSON 上传成功",
                "analysis_id": analysis_id,
                "summary": summary,
                "meta": {"schema_version": "1.1", "bvid": bvid, "analysis_id": analysis_id},
            }
        )
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        log_error(f"评论 JSON 上传出错：{exc}")
        return jsonify({"success": False, "message": "评论上传失败，请稍后再试"}), 500


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


@app.route("/comment_analysis", methods=["POST"])
def comment_analysis_route():
    try:
        guard = _guard_post("analysis")
        if guard:
            return guard

        data = request.get_json(silent=True) or {}
        bvid, analysis_id = _resolve_bvid_and_analysis_id(data)
        if not bvid:
            return jsonify({"success": False, "message": "未提供有效BV号"}), 400

        if not analysis_id:
            record = _latest_record_with_comment(bvid)
            if record:
                analysis_id = record.get("analysis_id")

        if not default_store.latest_danmaku_txt(bvid, analysis_id):
            return jsonify({"success": False, "message": "未找到弹幕文件"}), 404
        if not default_store.latest_comment_export(bvid, analysis_id):
            return jsonify({"success": False, "message": "未找到评论 JSON，请重新上传"}), 404
        job = _start_analysis_job("comment_analysis", bvid, analysis_id)
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
        log_error(f"评论联合分析出错：{exc}")
        return jsonify({"success": False, "message": "分析失败，请稍后再试"}), 500


@app.route("/full_analysis", methods=["POST"])
def full_analysis_route():
    try:
        guard = _guard_post("analysis")
        if guard:
            return guard

        data = request.get_json(silent=True) or {}
        bvid, analysis_id = _resolve_bvid_and_analysis_id(data)
        if not bvid:
            return jsonify({"success": False, "message": "未提供有效BV号"}), 400

        if not analysis_id:
            record = _latest_record_with_full_materials(bvid)
            if record:
                analysis_id = record.get("analysis_id")

        if not default_store.latest_danmaku_txt(bvid, analysis_id):
            return jsonify({"success": False, "message": "未找到弹幕文件"}), 404
        if not default_store.latest_subtitle(bvid, analysis_id):
            return jsonify({"success": False, "message": "未找到字幕文件，请重新上传"}), 404
        if not default_store.latest_comment_export(bvid, analysis_id):
            return jsonify({"success": False, "message": "未找到评论 JSON，请重新上传"}), 404
        job = _start_analysis_job("full_analysis", bvid, analysis_id)
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
        log_error(f"全量综合分析出错：{exc}")
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


@app.route("/api/v2/custom-analysis/comment", methods=["POST"])
def prepare_custom_comment_analysis():
    try:
        guard = _guard_post("analysis")
        if guard:
            return guard

        data = request.get_json(silent=True) or {}
        bvid, analysis_id = _resolve_bvid_and_analysis_id(data)
        if not bvid:
            return jsonify({"success": False, "message": "未提供有效BV号"}), 400
        if not analysis_id:
            record = _latest_record_with_comment(bvid)
            if record:
                analysis_id = record.get("analysis_id")
        bundle = _prepare_comment_analysis_bundle(
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
        log_error(f"自定义评论联合分析材料准备失败：{exc}")
        return jsonify({"success": False, "message": "分析材料准备失败，请稍后再试"}), 500


@app.route("/api/v2/custom-analysis/full", methods=["POST"])
def prepare_custom_full_analysis():
    try:
        guard = _guard_post("analysis")
        if guard:
            return guard

        data = request.get_json(silent=True) or {}
        bvid, analysis_id = _resolve_bvid_and_analysis_id(data)
        if not bvid:
            return jsonify({"success": False, "message": "未提供有效BV号"}), 400
        if not analysis_id:
            record = _latest_record_with_full_materials(bvid)
            if record:
                analysis_id = record.get("analysis_id")
        bundle = _prepare_full_analysis_bundle(
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
        log_error(f"自定义全量综合分析材料准备失败：{exc}")
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
            site=_request_site(),
            host=_request_host(),
            analysis_id=(data.get("analysis_id") or "").strip() or None,
            snapshot=data.get("snapshot"),
            content_analysis=data.get("content_analysis"),
            deep_analysis=data.get("deep_analysis"),
            comment_analysis=data.get("comment_analysis"),
            full_analysis=data.get("full_analysis"),
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
        request.args.get("site", "all"),
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
