from __future__ import annotations

from typing import Any

from danmaku_backend.analysis.ai_analyzer import AIAnalyzer
from danmaku_backend.analysis.comment_analysis import CommentJointAnalysis
from danmaku_backend.analysis.llm_client import LLMClient
from logger import log_api, log_info, log_success


class FullComprehensiveAnalysis:
    @staticmethod
    def _has_text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _list_has_content(value: Any) -> bool:
        if not isinstance(value, list):
            return False
        for item in value:
            if FullComprehensiveAnalysis._has_text(item):
                return True
            if isinstance(item, dict) and any(
                FullComprehensiveAnalysis._has_text(part) for part in item.values()
            ):
                return True
        return False

    @staticmethod
    def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
        source = result if isinstance(result, dict) else {}
        content_expansion = source.get("content_expansion") or source.get("content_expression") or {}
        user_feedback = source.get("user_feedback") or source.get("feedback_alignment") or {}
        risk_block = source.get("differences_and_risks") or source.get("risks") or {}
        optimization = source.get("optimization_directions") or source.get("suggestions") or {}
        return {
            **source,
            "full_overview": source.get("full_overview") or source.get("overall_insight") or "",
            "content_expansion": {
                "mainline": content_expansion.get("mainline") or content_expansion.get("main_line") or "",
                "rhythm": content_expansion.get("rhythm") or content_expansion.get("rhythm_turning") or "",
            },
            "key_moments": source.get("key_moments") if isinstance(source.get("key_moments"), list) else [],
            "user_feedback": {
                "shared_topics": user_feedback.get("shared_topics")
                if isinstance(user_feedback.get("shared_topics"), list)
                else [],
                "difference_signals": user_feedback.get("difference_signals")
                if isinstance(user_feedback.get("difference_signals"), list)
                else user_feedback.get("differences")
                if isinstance(user_feedback.get("differences"), list)
                else [],
            },
            "differences_and_risks": {
                "feedback_difference": risk_block.get("feedback_difference")
                or risk_block.get("difference_summary")
                or "",
                "risk_points": risk_block.get("risk_points")
                if isinstance(risk_block.get("risk_points"), list)
                else [],
            },
            "optimization_directions": {
                "review": optimization.get("review") if isinstance(optimization.get("review"), list) else [],
                "topics": optimization.get("topics") if isinstance(optimization.get("topics"), list) else [],
                "editing": optimization.get("editing") if isinstance(optimization.get("editing"), list) else [],
            },
        }

    @staticmethod
    def is_complete_result(result: dict[str, Any]) -> bool:
        normalized = FullComprehensiveAnalysis.normalize_result(result)
        return all(
            [
                FullComprehensiveAnalysis._has_text(normalized.get("full_overview")),
                FullComprehensiveAnalysis._has_text(normalized["content_expansion"].get("mainline")),
                FullComprehensiveAnalysis._has_text(normalized["content_expansion"].get("rhythm")),
                FullComprehensiveAnalysis._list_has_content(normalized.get("key_moments")),
                FullComprehensiveAnalysis._list_has_content(normalized["user_feedback"].get("shared_topics")),
                FullComprehensiveAnalysis._list_has_content(
                    normalized["user_feedback"].get("difference_signals")
                ),
                FullComprehensiveAnalysis._has_text(
                    normalized["differences_and_risks"].get("feedback_difference")
                ),
                FullComprehensiveAnalysis._list_has_content(
                    normalized["differences_and_risks"].get("risk_points")
                ),
                FullComprehensiveAnalysis._list_has_content(
                    normalized["optimization_directions"].get("review")
                ),
                FullComprehensiveAnalysis._list_has_content(
                    normalized["optimization_directions"].get("topics")
                ),
                FullComprehensiveAnalysis._list_has_content(
                    normalized["optimization_directions"].get("editing")
                ),
            ]
        )

    @staticmethod
    def _duration_seconds(duration: str) -> int:
        parts = [int(part) for part in str(duration or "0:00").split(":") if part.isdigit()]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return max(parts[0], 1) if parts else 1

    @staticmethod
    def build_full_prompt(
        video_info: dict[str, Any],
        subtitle_content: str,
        danmaku_samples: str,
        peak_danmaku: list[str],
        comment_export: dict[str, Any],
    ) -> str:
        comment_payload = CommentJointAnalysis.compact_comment_export(comment_export)
        return f"""分析一个B站视频的字幕、弹幕与评论数据，生成「全量综合分析」报告。

分析目标：
1. 字幕代表视频内容本身和创作者表达结构，用来判断内容讲了什么、如何展开、哪些信息被重点呈现。
2. 弹幕代表观看过程中的即时反应，用来判断具体时间点的情绪、笑点、疑问和注意力变化。
3. 评论代表看完后的复盘讨论，用来判断用户最终记住了什么、延伸讨论了什么、哪些问题沉淀为长期反馈。
4. 全量综合分析必须把三类材料放在一起判断：内容表达、即时反馈、复盘反馈之间是否一致，差异对内容复盘和后续优化有什么价值。

输出要求：
1. 字段正文不要重复字段标题，不要写成「全量概览：……」「内容展开：……」「用户反馈：……」「优化方向：……」。
2. 不要泛泛写“弹幕即时、评论理性”；必须指出具体内容点、具体反馈差异和具体影响。
3. 风险点必须来自字幕、弹幕、评论三者错位或互证关系，不能写无来源的猜测。
4. 建议要能用于内容复盘、后续选题、剪辑互动，不要写空泛口号。
5. 必须返回一个完整的顶层JSON对象，顶层必须包含 full_overview、content_expansion、key_moments、user_feedback、differences_and_risks、optimization_directions 六个字段。
6. 不允许只返回 mainline、rhythm 等子字段；不允许省略数组字段；每个数组至少给出一个有内容的条目。

请以JSON格式返回：
{{
  "full_overview": "全量概览：概括字幕、弹幕、评论共同说明的核心结论，以及它为什么值得关注",
  "content_expansion": {{
    "mainline": "内容展开的主线：字幕/内容本身如何展开，用户理解这条主线的情况如何",
    "rhythm": "节奏转折：哪些内容节点带动了弹幕或评论反馈，节奏上的变化说明什么"
  }},
  "key_moments": [
    {{
      "time": "mm:ss",
      "title": "关键时刻标题",
      "content": "字幕或内容侧发生了什么",
      "danmaku": "弹幕侧如何即时反应",
      "comment": "评论侧如何复盘或延伸",
      "judgement": "这个时刻的综合判断和价值"
    }}
  ],
  "user_feedback": {{
    "shared_topics": ["字幕、弹幕、评论共同指向或互相验证的主题"],
    "difference_signals": [
      {{
        "title": "差异信号标题",
        "finding": "三类材料之间的具体差异或互证关系",
        "value": "这个信号对内容复盘、选题或剪辑有什么用"
      }}
    ]
  }},
  "differences_and_risks": {{
    "feedback_difference": "概括内容表达重点、观看即时反馈、看完复盘反馈之间的主要错位或一致性",
    "risk_points": [
      {{
        "risk": "可能造成误判的风险",
        "basis": "字幕、弹幕、评论中的对应依据",
        "impact": "为什么这个风险重要，会影响什么判断"
      }}
    ]
  }},
  "optimization_directions": {{
    "review": ["内容复盘方向"],
    "topics": ["后续选题方向"],
    "editing": ["剪辑互动方向"]
  }}
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

字幕内容：
{subtitle_content}

弹幕样本(按时间顺序)：
{danmaku_samples}

高峰期弹幕样本：
{' | '.join(peak_danmaku)}

评论导出JSON：
{comment_payload}"""

    @staticmethod
    def prepare_full_request(
        video_info: dict[str, Any],
        subtitle_content: str,
        danmaku_list: list[dict[str, Any]],
        time_density: list[dict[str, Any]],
        comment_export: dict[str, Any],
        analysis_options: dict[str, Any] | None = None,
        *,
        with_logs: bool = True,
    ) -> dict[str, Any]:
        video_duration = FullComprehensiveAnalysis._duration_seconds(video_info["duration"])
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
        prompt = FullComprehensiveAnalysis.build_full_prompt(
            video_info,
            subtitle_content,
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
    def analyze_all_sources(
        video_info: dict[str, Any],
        subtitle_content: str,
        danmaku_list: list[dict[str, Any]],
        time_density: list[dict[str, Any]],
        comment_export: dict[str, Any],
        analysis_options: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        log_info("阶段 1/4：准备全量综合分析数据")
        summary = CommentJointAnalysis.comment_summary(comment_export)
        log_info(
            f"已读取字幕文本、{len(danmaku_list)} 条弹幕、{summary['root_count']} 条直接评论和 {summary['reply_count']} 条楼中楼"
        )
        request_bundle = FullComprehensiveAnalysis.prepare_full_request(
            video_info,
            subtitle_content,
            danmaku_list,
            time_density,
            comment_export,
            analysis_options=analysis_options,
            with_logs=True,
        )
        log_info("阶段 3/4：对齐内容展开、即时反馈与复盘讨论")
        log_success("全量综合分析材料已整理完成")
        log_api("阶段 4/4：正在提交全量综合分析任务...")
        result = LLMClient().chat_json(
            request_bundle["prompt"],
            result_validator=FullComprehensiveAnalysis.is_complete_result,
        )
        if result:
            result = FullComprehensiveAnalysis.normalize_result(result)
            log_success("全量综合分析报告已生成")
        return result
