from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from config import get_analysis_config
from danmaku_backend.analysis.llm_client import LLMClient
from logger import log_api, log_error, log_info, log_success


class AIAnalyzer:
    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def normalize_sample_options(
        analysis_options: dict[str, Any] | None = None,
        *,
        max_samples_key: str = "content_max_samples",
    ) -> dict[str, Any]:
        sample_config = get_analysis_config()
        options = analysis_options or {}
        sample_mode = str(options.get("sample_mode") or "balanced").strip().lower()
        if sample_mode not in {"balanced", "full"}:
            sample_mode = "balanced"

        max_samples = AIAnalyzer._positive_int(
            options.get(max_samples_key),
            sample_config[max_samples_key],
        )
        normalized = {
            "sample_mode": sample_mode,
            "max_samples": max_samples,
            "head_samples": min(
                AIAnalyzer._positive_int(options.get("head_samples"), sample_config["head_samples"]),
                max_samples,
            ),
            "peak_bucket_count": AIAnalyzer._positive_int(
                options.get("peak_bucket_count"),
                sample_config["peak_bucket_count"],
            ),
            "peak_window_seconds": AIAnalyzer._positive_int(
                options.get("peak_window_seconds"),
                sample_config["peak_window_seconds"],
            ),
            "peak_samples_per_bucket": AIAnalyzer._positive_int(
                options.get("peak_samples_per_bucket"),
                sample_config["peak_samples_per_bucket"],
            ),
            "peak_preview_limit": AIAnalyzer._positive_int(
                options.get("peak_preview_limit"),
                sample_config["peak_preview_limit"],
            ),
        }
        return normalized

    @staticmethod
    def parse_danmaku_txt(file_path: str) -> list[dict[str, Any]] | None:
        danmaku_list: list[dict[str, Any]] = []
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                for line in file:
                    time_str = line[1:6]
                    text = line[8:].strip()
                    minutes, seconds = map(int, time_str.split(":"))
                    danmaku_list.append(
                        {
                            "text": text,
                            "appear_time": minutes * 60 + seconds,
                            "timestamp": int(datetime.now().timestamp()),
                        }
                    )
            return danmaku_list
        except Exception as exc:
            log_error(f"解析弹幕文件失败: {exc}")
            return None

    @staticmethod
    def sample_danmaku(
        danmaku_list: list[dict[str, Any]],
        time_density: list[dict[str, Any]],
        video_duration: int,
        max_samples: int | None = None,
        analysis_options: dict[str, Any] | None = None,
        with_logs: bool = True,
    ) -> tuple[str, list[str]]:
        normalized_options = AIAnalyzer.normalize_sample_options(analysis_options)
        if max_samples is not None:
            normalized_options["max_samples"] = max(1, int(max_samples))
            normalized_options["head_samples"] = min(
                normalized_options["head_samples"],
                normalized_options["max_samples"],
            )
        max_samples = normalized_options["max_samples"]
        head_limit = min(normalized_options["head_samples"], max_samples)
        peak_bucket_count = normalized_options["peak_bucket_count"]
        peak_window_seconds = normalized_options["peak_window_seconds"]
        peak_samples_per_bucket = normalized_options["peak_samples_per_bucket"]
        peak_preview_limit = normalized_options["peak_preview_limit"]
        sample_mode = normalized_options["sample_mode"]

        total_count = len(danmaku_list)
        if with_logs:
            log_info("阶段 2/4：整理弹幕文本样本")
        sorted_danmaku = sorted(danmaku_list, key=lambda item: item["appear_time"])

        peak_times = sorted(
            [(item["time"], item["count"]) for item in time_density],
            key=lambda item: item[1],
            reverse=True,
        )[:peak_bucket_count]
        peak_danmaku = [
            item["text"]
            for item in sorted_danmaku
            if any(
                abs(int(item["appear_time"]) - peak_time[0]) <= peak_window_seconds
                for peak_time in peak_times
            )
        ][:peak_preview_limit]

        if sample_mode == "full":
            if with_logs:
                log_info(f"采样策略：使用全量采样，保留全部 {total_count} 条弹幕文本")
                log_success(f"样本整理完成：将使用 {total_count} 条弹幕进入文本分析")
            danmaku_samples = "\n".join(
                f"[{item['appear_time']}秒] {item['text']}" for item in sorted_danmaku
            )
            return danmaku_samples, peak_danmaku

        if with_logs:
            log_info(
                f"采样策略：最多保留 {max_samples} 条，优先覆盖开头、互动高峰和分散样本"
            )

        if total_count <= max_samples:
            if with_logs:
                log_info(f"弹幕总数 {total_count} 条，未超过采样上限，使用全量弹幕文本")
            danmaku_samples = "\n".join(
                f"[{item['appear_time']}秒] {item['text']}" for item in sorted_danmaku
            )
            if with_logs:
                log_success(f"样本整理完成：将使用 {total_count} 条弹幕进入文本分析")
            return danmaku_samples, peak_danmaku

        if with_logs:
            log_info(f"弹幕总数 {total_count} 条，超过采样上限，目标样本 {max_samples} 条")
        sampled_danmaku = []
        sampled_indices = set()

        head_samples = sorted_danmaku[:head_limit]
        sampled_danmaku.extend(head_samples)
        sampled_indices.update(id(item) for item in head_samples)
        if with_logs:
            log_info(f"已保留开头弹幕 {len(head_samples)} 条")

        if with_logs:
            log_info(f"已识别互动高峰 {len(peak_times)} 个，正在抽取高峰片段")

        peak_samples = []
        for peak_time, _count in peak_times:
            time_samples = [
                item
                for item in sorted_danmaku
                if abs(int(item["appear_time"]) - peak_time) <= peak_window_seconds
                and id(item) not in sampled_indices
            ]
            if time_samples:
                new_samples = random.sample(
                    time_samples, min(len(time_samples), peak_samples_per_bucket)
                )
                peak_samples.extend(new_samples)
                sampled_danmaku.extend(new_samples)
                sampled_indices.update(id(item) for item in new_samples)

        if with_logs:
            log_info(f"已抽取高峰弹幕 {len(peak_samples)} 条")
        remaining_quota = max_samples - len(sampled_danmaku)
        if remaining_quota > 0:
            available_danmaku = [item for item in sorted_danmaku if id(item) not in sampled_indices]
            if available_danmaku:
                fill_samples = (
                    random.sample(available_danmaku, remaining_quota)
                    if len(available_danmaku) > remaining_quota
                    else available_danmaku
                )
                sampled_danmaku.extend(fill_samples)
                if with_logs:
                    log_info(f"已补充时间分散样本 {len(fill_samples)} 条")

        final_samples = sorted(sampled_danmaku, key=lambda item: item["appear_time"])
        if with_logs:
            log_success(f"样本整理完成：从 {total_count} 条弹幕中选取 {len(final_samples)} 条")
        return (
            "\n".join(f"[{item['appear_time']}秒] {item['text']}" for item in final_samples),
            [item["text"] for item in peak_samples],
        )

    @staticmethod
    def build_content_prompt(
        video_info: dict[str, Any],
        danmaku_samples: str,
        peak_danmaku: list[str],
    ) -> str:
        return f"""分析一个B站视频的弹幕数据并生成洞察报告。
视频信息：
标题：{video_info['title']}
UP主：{video_info['author']}
发布时间：{video_info['publish_time']}
视频分区：{video_info['type']}
时长：{video_info['duration']}
标签：{', '.join(video_info['tags'])}
播放量：{video_info['view_count']}
弹幕量：{video_info['danmaku_count']}

弹幕样本(按时间顺序)：
{danmaku_samples}

高峰期弹幕样本：
{' | '.join(peak_danmaku)}

请从以下角度分析并以JSON格式返回：
{{
  "content_summary": "基于弹幕反映推测视频的内容",
  "audience_response": "观众对视频内容的整体评价分析",
  "interaction_patterns": "观众互动方式和参与度的变化分析，如有集中性表现重点分析原因",
  "hot_topics": ["观众讨论最多的话题，数组形式"],
  "suggestions": ["基于观众反应分析他们对视频或作者的要求，数组形式"]
}}"""

    @staticmethod
    def prepare_content_request(
        video_info: dict[str, Any],
        danmaku_list: list[dict[str, Any]],
        time_density: list[dict[str, Any]],
        analysis_options: dict[str, Any] | None = None,
        *,
        with_logs: bool = True,
    ) -> dict[str, Any]:
        minutes, seconds = map(int, video_info["duration"].split(":"))
        video_duration = minutes * 60 + seconds
        normalized_options = AIAnalyzer.normalize_sample_options(
            analysis_options,
            max_samples_key="content_max_samples",
        )
        danmaku_samples, peak_danmaku = AIAnalyzer.sample_danmaku(
            danmaku_list,
            time_density,
            video_duration,
            max_samples=normalized_options["max_samples"],
            analysis_options=normalized_options,
            with_logs=with_logs,
        )
        prompt = AIAnalyzer.build_content_prompt(video_info, danmaku_samples, peak_danmaku)
        sample_count = len([line for line in danmaku_samples.splitlines() if line.strip()])
        return {
            "prompt": prompt,
            "sample_mode": normalized_options["sample_mode"],
            "sample_count": sample_count,
            "peak_count": len(peak_danmaku),
            "sample_options": normalized_options,
        }

    @staticmethod
    def analyze_content(
        video_info: dict[str, Any],
        danmaku_list: list[dict[str, Any]],
        time_density: list[dict[str, Any]],
        analysis_options: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        log_info("阶段 1/4：准备内容分析数据")
        log_info(f"已读取 {len(danmaku_list)} 条弹幕，正在统计互动分布")
        request_bundle = AIAnalyzer.prepare_content_request(
            video_info,
            danmaku_list,
            time_density,
            analysis_options=analysis_options,
            with_logs=True,
        )
        log_info("阶段 3/4：组织视频信息和弹幕样本")
        log_success("分析材料已整理完成")
        log_api("阶段 4/4：正在提交文本分析任务...")
        result = LLMClient().chat_json(request_bundle["prompt"])
        if result:
            log_success("弹幕分析报告已生成")
        return result
