from __future__ import annotations

import json
from typing import Any

from danmaku_backend.analysis.ai_analyzer import AIAnalyzer
from danmaku_backend.analysis.llm_client import LLMClient
from logger import log_api, log_info, log_success


COMMENT_SCHEMA_VERSION = "bili-comment-capture/v1"
COMMENT_FORMAT = "bilibili-comment-ai-analysis/v1"


class CommentJointAnalysis:
    @staticmethod
    def validate_comment_export(export_data: Any, bvid: str | None = None) -> dict[str, Any]:
        if not isinstance(export_data, dict):
            raise ValueError("评论 JSON 格式无效")
        if export_data.get("schemaVersion") != COMMENT_SCHEMA_VERSION:
            raise ValueError("不是插件导出的评论 JSON")
        if export_data.get("format") != COMMENT_FORMAT:
            raise ValueError("评论 JSON format 不匹配")
        if not isinstance(export_data.get("threads"), list):
            raise ValueError("评论 JSON 缺少 threads")

        file_bvid = str((export_data.get("video") or {}).get("bvid") or "").strip()
        current_bvid = str(bvid or "").strip()
        if current_bvid and file_bvid and file_bvid != current_bvid:
            raise ValueError(f"评论文件对应 {file_bvid}，当前视频是 {current_bvid}")
        if current_bvid and not file_bvid:
            raise ValueError("评论 JSON 缺少视频 BV 号")
        return export_data

    @staticmethod
    def comment_summary(export_data: dict[str, Any]) -> dict[str, Any]:
        summary = export_data.get("summary") if isinstance(export_data.get("summary"), dict) else {}
        guide = export_data.get("exportGuide") if isinstance(export_data.get("exportGuide"), dict) else {}
        guide_counts = guide.get("counts") if isinstance(guide.get("counts"), dict) else {}
        threads = export_data.get("threads") if isinstance(export_data.get("threads"), list) else []

        def count_value(*values: Any) -> int:
            for value in values:
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed >= 0:
                    return parsed
            return 0

        video = export_data.get("video") if isinstance(export_data.get("video"), dict) else {}
        return {
            "bvid": str(video.get("bvid") or "").strip(),
            "root_count": count_value(
                summary.get("rootCommentCount"),
                guide_counts.get("directCommentCount"),
                len(threads),
            ),
            "reply_count": count_value(
                summary.get("nestedReplyCount"),
                guide_counts.get("nestedReplyCount"),
            ),
            "pinned_count": count_value(
                summary.get("pinnedCommentCount"),
                guide_counts.get("pinnedCommentCount"),
            ),
        }

    @staticmethod
    def compact_comment_export(export_data: dict[str, Any]) -> str:
        compact_payload = {
            "schemaVersion": export_data.get("schemaVersion"),
            "format": export_data.get("format"),
            "video": export_data.get("video") if isinstance(export_data.get("video"), dict) else {},
            "summary": export_data.get("summary") if isinstance(export_data.get("summary"), dict) else {},
            "captureContext": (
                export_data.get("captureContext")
                if isinstance(export_data.get("captureContext"), dict)
                else {}
            ),
            "exportGuide": (
                export_data.get("exportGuide") if isinstance(export_data.get("exportGuide"), dict) else {}
            ),
            "threads": export_data.get("threads") if isinstance(export_data.get("threads"), list) else [],
        }
        return json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def build_comment_prompt(
        video_info: dict[str, Any],
        danmaku_samples: str,
        peak_danmaku: list[str],
        comment_export: dict[str, Any],
    ) -> str:
        comment_payload = CommentJointAnalysis.compact_comment_export(comment_export)
        return f"""分析一个B站视频的弹幕与评论数据，生成「评论联合分析」报告。

分析目标：
1. 弹幕代表观看过程中的即时反应，适合判断具体片段的现场情绪、笑点、槽点和注意力变化。
2. 评论代表看完后的复盘讨论，适合判断用户沉淀下来的整体印象、争议点、延伸讨论和长期记忆点。
3. 需要交叉比较两类反馈：哪些主题在弹幕和评论中同时成立，哪些只在其中一侧出现，以及这些差异对内容复盘的意义。

输出要求：
1. 字段值正文不要重复字段标题，不要写成「综合洞察：……」「弹幕反应：……」「评论反馈：……」「分歧与风险：……」。
2. 分歧必须是同一议题下的实质对照，不能只写「弹幕更情绪化、评论更理性」这种泛泛判断。
3. 风险点必须来自弹幕和评论差异本身，必须说明依据和影响；不要把普通建议、泛泛争议或无根据猜测写成风险。

请以JSON格式返回：
{{
  "overall_insight": "弹幕和评论共同揭示的核心用户反馈，以及它为什么值得关注",
  "danmaku_reaction": "观看过程中的即时情绪、集中片段和典型反馈",
  "comment_feedback": "评论区沉淀出的复盘讨论、整体评价和延伸观点",
  "shared_topics": ["弹幕和评论都反复出现、能证明用户真实关注的主题"],
  "feedback_differences": [
    {{
      "topic": "存在差异的具体议题",
      "danmaku_view": "弹幕侧具体怎么表现，引用或概括典型弹幕信号",
      "comment_view": "评论侧具体怎么表现，引用或概括典型评论信号",
      "difference": "两侧反馈的实质性差异，以及这个差异说明了什么"
    }}
  ],
  "differences_and_risks": "对主要差异的总体判断；如果已经在 feedback_differences 中展开，这里只做简短归纳",
  "risk_points": [
    {{
      "risk": "由弹幕/评论差异带来的误判风险或运营风险",
      "basis": "这个风险对应的弹幕侧与评论侧证据",
      "impact": "为什么它重要，可能影响内容复盘、选题或沟通的哪一部分"
    }}
  ],
  "analysis_suggestions": ["基于交叉结果给出可用于复盘、选题、剪辑或内容优化的建议"]
}}

视频信息：
标题：{video_info['title']}
UP主：{video_info['author']}
发布时间：{video_info['publish_time']}
分区：{video_info['type']}
时长：{video_info['duration']}
标签：{', '.join(video_info['tags'])}
播放量：{video_info.get('view_count', 0)}
弹幕量：{video_info.get('danmaku_count', 0)}

弹幕样本(按时间顺序)：
{danmaku_samples}

高峰期弹幕样本：
{' | '.join(peak_danmaku)}

评论导出JSON：
{comment_payload}"""

    @staticmethod
    def prepare_comment_request(
        video_info: dict[str, Any],
        danmaku_list: list[dict[str, Any]],
        time_density: list[dict[str, Any]],
        comment_export: dict[str, Any],
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
        prompt = CommentJointAnalysis.build_comment_prompt(
            video_info,
            danmaku_samples,
            peak_danmaku,
            comment_export,
        )
        sample_count = len([line for line in danmaku_samples.splitlines() if line.strip()])
        return {
            "prompt": prompt,
            "sample_mode": normalized_options["sample_mode"],
            "sample_count": sample_count,
            "peak_count": len(peak_danmaku),
            "comment_summary": CommentJointAnalysis.comment_summary(comment_export),
            "sample_options": normalized_options,
        }

    @staticmethod
    def analyze_danmaku_and_comments(
        video_info: dict[str, Any],
        danmaku_list: list[dict[str, Any]],
        time_density: list[dict[str, Any]],
        comment_export: dict[str, Any],
        analysis_options: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        log_info("阶段 1/4：准备评论联合分析数据")
        summary = CommentJointAnalysis.comment_summary(comment_export)
        log_info(
            f"已读取 {len(danmaku_list)} 条弹幕、{summary['root_count']} 条直接评论和 {summary['reply_count']} 条楼中楼"
        )
        request_bundle = CommentJointAnalysis.prepare_comment_request(
            video_info,
            danmaku_list,
            time_density,
            comment_export,
            analysis_options=analysis_options,
            with_logs=True,
        )
        log_info("阶段 3/4：合并弹幕样本和评论数据")
        log_success("评论联合分析材料已整理完成")
        log_api("阶段 4/4：正在提交评论联合分析任务...")
        result = LLMClient().chat_json(request_bundle["prompt"])
        if result:
            log_success("评论联合分析报告已生成")
        return result
