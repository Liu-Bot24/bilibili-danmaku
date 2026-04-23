from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from danmaku_backend.services.artifacts import default_store
from danmaku_backend.services.bilibili import BILIBILI_HEADERS, get_cid, get_video_info
from danmaku_backend.services.stats import analyze_danmaku


def _load_cached_danmaku_list(csv_path: Path) -> list[dict[str, Any]]:
    danmaku_list: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            send_time = str(row.get("发送时间") or "").strip()
            parsed_time = None
            if send_time:
                try:
                    parsed_time = datetime.strptime(send_time, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    parsed_time = None
            timestamp = int(parsed_time.timestamp()) if parsed_time else 0
            danmaku_list.append(
                {
                    "text": str(row.get("弹幕内容") or ""),
                    "appear_time": float(row.get("出现时间(秒)") or 0),
                    "timestamp": timestamp,
                    "type": str(row.get("类型") or ""),
                    "size": str(row.get("字体大小") or ""),
                    "color": str(row.get("颜色") or ""),
                    "sender": str(row.get("发送者ID") or ""),
                    "send_time": send_time,
                    "send_time_iso": parsed_time.isoformat(timespec="seconds") if parsed_time else "",
                }
            )
    danmaku_list.sort(key=lambda item: item["appear_time"])
    return danmaku_list


def get_danmaku(bvid: str) -> dict[str, Any] | tuple[None, str]:
    video_info = get_video_info(bvid)
    if not video_info:
        return None, "获取视频信息失败"

    cached_record = default_store.latest_cached_danmaku_record(bvid)
    if cached_record:
        csv_path = default_store.download_dir / str(cached_record["csv_filename"])
        danmaku_list = _load_cached_danmaku_list(csv_path)
        return {
            "analysis_id": cached_record["analysis_id"],
            "video_info": video_info,
            "csv_filename": cached_record["csv_filename"],
            "txt_filename": cached_record["txt_filename"],
            "count": len(danmaku_list),
            "analysis": analyze_danmaku(danmaku_list),
            "danmaku_list": danmaku_list,
            "cache_hit": True,
        }

    cid = get_cid(bvid)
    if not cid:
        return None, "获取视频信息失败"

    try:
        response = requests.get(
            f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}",
            headers=BILIBILI_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        root = ET.fromstring(response.text)

        danmaku_list = []
        for item in root.findall("d"):
            attrs = item.get("p").split(",")
            appear_time = float(attrs[0])
            timestamp = int(attrs[4])
            danmaku_list.append(
                {
                    "text": item.text or "",
                    "appear_time": appear_time,
                    "timestamp": timestamp,
                    "type": attrs[1],
                    "size": attrs[2],
                    "color": f"#{int(attrs[3]):06x}",
                    "sender": attrs[6],
                    "send_time": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
                    "send_time_iso": datetime.fromtimestamp(timestamp).isoformat(timespec="seconds"),
                }
            )

        danmaku_list.sort(key=lambda item: item["appear_time"])
        export = default_store.save_danmaku_files(bvid, danmaku_list)
        return {
            "analysis_id": export.analysis_id,
            "video_info": video_info,
            "csv_filename": export.csv_filename,
            "txt_filename": export.txt_filename,
            "count": len(danmaku_list),
            "analysis": analyze_danmaku(danmaku_list),
            "danmaku_list": danmaku_list,
            "cache_hit": False,
        }
    except Exception as exc:
        return None, f"下载失败：{exc}"
