from __future__ import annotations

from typing import Any

from danmaku_backend.analysis.ai_analyzer import AIAnalyzer
from danmaku_backend.analysis.llm_client import LLMClient
from danmaku_backend.services.artifacts import default_store
from logger import log_api, log_success, log_info


class DeepAnalysis:
    @staticmethod
    def build_deep_prompt(
        video_info: dict[str, Any],
        subtitle_content: str,
        danmaku_samples: str,
        peak_danmaku: list[str],
    ) -> str:
        return f"""分析一个B站视频的字幕和弹幕数据，以扁平化JSON格式返回：

{{
  "structure_time": "视频时间轴梳理（精确到秒，标注关键内容与转场）",
  "structure_analysis": "叙事节奏、转场衔接及情绪调动的分析",
  "commercial_value": "商业属性分析（需引用关键对话）及广告呈现方式",
  "creative_highlights": "创意亮点、元素运用及独特性分析",
  "key_moments": ["高度互动的关键时刻（mm:ss格式）"],
  "emotional_response": "用户情绪反应分析（引用典型弹幕）",
  "focus_points": "用户关注重点分析（结合互动数据）",
  "strategy": "营销策略、目标受众与传达效果分析",
  "communication": "传播亮点、用户反馈及传播潜力分析"
}}

分析对象信息：
标题：{video_info['title']}
UP主：{video_info['author']}
发布时间：{video_info['publish_time']}
分区：{video_info['type']}
时长：{video_info['duration']}
标签：{', '.join(video_info['tags'])}

字幕内容：
{subtitle_content}

弹幕样本：
{danmaku_samples}

高峰期弹幕：
{' | '.join(peak_danmaku)}"""

    @staticmethod
    def prepare_deep_request(
        video_info: dict[str, Any],
        subtitle_content: str,
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
            max_samples_key="deep_max_samples",
        )
        danmaku_samples, peak_danmaku = AIAnalyzer.sample_danmaku(
            danmaku_list,
            time_density,
            video_duration,
            max_samples=normalized_options["max_samples"],
            analysis_options=normalized_options,
            with_logs=with_logs,
        )
        prompt = DeepAnalysis.build_deep_prompt(
            video_info,
            subtitle_content,
            danmaku_samples,
            peak_danmaku,
        )
        sample_count = len([line for line in danmaku_samples.splitlines() if line.strip()])
        return {
            "prompt": prompt,
            "sample_mode": normalized_options["sample_mode"],
            "sample_count": sample_count,
            "peak_count": len(peak_danmaku),
            "sample_options": normalized_options,
        }

    @staticmethod
    def analyze_subtitle_and_danmaku(
        video_info: dict[str, Any],
        subtitle_content: str,
        danmaku_list: list[dict[str, Any]],
        time_density: list[dict[str, Any]],
        analysis_options: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        log_info("阶段 1/4：准备深度分析数据")
        log_info("正在读取字幕文本并整理视频信息")
        log_info(f"已读取 {len(danmaku_list)} 条弹幕，正在统计互动分布")
        request_bundle = DeepAnalysis.prepare_deep_request(
            video_info,
            subtitle_content,
            danmaku_list,
            time_density,
            analysis_options=analysis_options,
            with_logs=True,
        )
        log_info("阶段 3/4：合并字幕文本和弹幕样本")
        log_success("深度分析材料已整理完成")
        log_api("阶段 4/4：正在提交字幕与弹幕文本分析任务...")
        result = LLMClient().chat_json(request_bundle["prompt"])
        if result:
            log_success("深度分析完成，报告已生成")
        return result

    @staticmethod
    def save_subtitle(file, bvid: str, analysis_id: str | None = None):
        return default_store.save_subtitle(file, bvid, analysis_id)
