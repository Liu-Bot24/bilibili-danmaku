from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

import requests

from logger import log_api, log_error, log_info, log_success


BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
BILIBILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}
_REGION_NAME_CACHE: dict[int, str] = {}
_REGION_NAME_FALLBACK = {
    86: "特摄",
    138: "搞笑",
    203: "热点",
    207: "财经商业",
    230: "软件应用",
    253: "动漫杂谈",
}


def extract_bvid(input_str: str) -> Optional[str]:
    """Extract and validate a Bilibili BV id from raw input or a video URL."""
    if not input_str:
        return None
    match = BV_RE.search(input_str.strip())
    return match.group(0) if match else None


def _request_json(url: str) -> dict[str, Any]:
    response = requests.get(url, headers=BILIBILI_HEADERS, timeout=10)
    log_api(f"API响应状态码: {response.status_code}")
    response.raise_for_status()
    return response.json()


def _https_url(url: Any) -> str:
    text = str(url or "").strip()
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith("http://"):
        return f"https://{text[7:]}"
    return text


def _region_name_from_tid(tid: Any) -> str:
    try:
        region_id = int(tid)
    except (TypeError, ValueError):
        return ""
    if region_id in _REGION_NAME_CACHE:
        return _REGION_NAME_CACHE[region_id]

    region_name = ""
    try:
        data = _request_json(
            f"https://api.bilibili.com/x/web-interface/dynamic/region?rid={region_id}"
        )
        archives = ((data.get("data") or {}).get("archives") or []) if data.get("code") == 0 else []
        for archive in archives:
            if int(archive.get("tid") or 0) == region_id and str(archive.get("tname") or "").strip():
                region_name = str(archive["tname"]).strip()
                break
        if not region_name and archives:
            region_name = str(archives[0].get("tname") or "").strip()
    except Exception as exc:
        log_error(f"通过分区ID获取分区名失败：{exc}")

    if not region_name:
        region_name = _REGION_NAME_FALLBACK.get(region_id, "")
    if region_name:
        _REGION_NAME_CACHE[region_id] = region_name
    return region_name


def get_video_info(bvid: str) -> Optional[dict[str, Any]]:
    """Fetch video metadata used by the UI and analysis prompts."""
    try:
        log_info("正在获取视频基础信息...")
        data = _request_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
        if data.get("code") != 0:
            log_error(f"获取视频信息失败，错误信息：{data.get('message', '未知错误')}")
            return None

        info = data["data"]
        duration = int(info["duration"])
        minutes = duration // 60
        seconds = duration % 60

        tags: list[str] = []
        tag_data = _request_json(f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}")
        if tag_data.get("code") == 0 and tag_data.get("data"):
            tags = [tag["tag_name"] for tag in tag_data["data"]]

        video_type = str(info.get("tname") or "").strip() or _region_name_from_tid(info.get("tid"))
        pages = info.get("pages") or []
        first_page = pages[0] if pages and isinstance(pages[0], dict) else {}
        cover_url = _https_url(info.get("pic"))
        first_frame_url = _https_url(first_page.get("first_frame"))
        season_cover_url = _https_url((info.get("ugc_season") or {}).get("cover"))

        result = {
            "title": info["title"],
            "description": info["desc"],
            "publish_time": datetime.fromtimestamp(info["pubdate"]).strftime("%Y-%m-%d %H:%M:%S"),
            "publish_time_iso": datetime.fromtimestamp(info["pubdate"]).isoformat(timespec="seconds"),
            "author": info["owner"]["name"],
            "view_count": info["stat"]["view"],
            "danmaku_count": info["stat"]["danmaku"],
            "duration": f"{minutes}:{seconds:02d}",
            "duration_seconds": duration,
            "type": video_type,
            "like_count": info["stat"]["like"],
            "coin_count": info["stat"]["coin"],
            "favorite_count": info["stat"]["favorite"],
            "share_count": info["stat"]["share"],
            "up_follower": info.get("owner", {}).get("follower", 0),
            "cover_url": cover_url,
            "web_cover_url": cover_url,
            "app_cover_url": cover_url,
            "first_frame_url": first_frame_url,
            "season_cover_url": season_cover_url,
            "tags": tags,
        }
        log_success(f"成功获取视频信息：《{result['title']}》")
        return result
    except Exception as exc:
        log_error(f"获取视频信息失败：{exc}")
        return None


def get_cid(bvid: str) -> Optional[int]:
    """Fetch the first page CID for a BV id."""
    try:
        data = _request_json(f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}")
        if data.get("code") == 0 and data.get("data"):
            return data["data"][0]["cid"]
        log_error(f"获取cid失败，错误信息：{data.get('message', '未知错误')}")
        return None
    except Exception as exc:
        log_error(f"获取cid失败：{exc}")
        return None
