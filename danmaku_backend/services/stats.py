from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from danmaku_backend.analysis.word_cloud import analyze_word_frequency


def analyze_danmaku(danmaku_list: list[dict[str, Any]]) -> dict[str, Any]:
    total_count = len(danmaku_list)
    time_distribution = Counter(int(float(item["appear_time"])) for item in danmaku_list)
    time_density = [{"time": time_value, "count": count} for time_value, count in sorted(time_distribution.items())]

    try:
        hour_distribution = Counter(datetime.fromtimestamp(int(item["timestamp"])).hour for item in danmaku_list)
    except KeyError:
        hour_distribution = Counter(int(float(item["appear_time"]) / 3600) % 24 for item in danmaku_list)

    hour_data = [{"hour": hour, "count": hour_distribution.get(hour, 0)} for hour in range(24)]
    peak_times = sorted(time_distribution.items(), key=lambda item: item[1], reverse=True)[:5]
    peak_danmaku: list[dict[str, Any]] = []
    for peak_time, _count in peak_times:
        peak_danmaku.extend(
            item for item in danmaku_list if int(float(item["appear_time"])) == peak_time
        )

    texts = [item.get("text") or "" for item in danmaku_list]
    return {
        "total_count": total_count,
        "time_density": time_density,
        "hour_distribution": hour_data,
        "peak_danmaku": peak_danmaku[:10],
        "word_cloud": analyze_word_frequency(texts),
    }

