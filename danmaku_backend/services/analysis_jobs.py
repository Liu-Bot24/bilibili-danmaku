from __future__ import annotations

from typing import Any

from ai_analyzer import AIAnalyzer
from danmaku_backend.analysis.comment_analysis import CommentJointAnalysis
from danmaku_backend.analysis.full_analysis import FullComprehensiveAnalysis
from deep_analysis import DeepAnalysis
from danmaku_backend.runtime.logging_bus import job_logging_context
from danmaku_backend.services.artifacts import default_store
from danmaku_backend.services.bilibili import get_video_info
from danmaku_backend.services.jobs import default_job_store
from danmaku_backend.services.stats import analyze_danmaku
from logger import log_info


def content_analysis_result(
    bvid: str,
    analysis_id: str | None,
    analysis_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    log_info("内容分析任务已启动")
    file_path = default_store.latest_danmaku_txt(bvid, analysis_id)
    if not file_path:
        raise ValueError("未找到弹幕文件，请重新下载")

    log_info("正在加载本次弹幕文件")
    video_info = get_video_info(bvid)
    if not video_info:
        raise RuntimeError("获取视频信息失败")

    danmaku_list = AIAnalyzer.parse_danmaku_txt(str(file_path))
    if not danmaku_list:
        raise RuntimeError("读取弹幕文件失败")

    analysis_result = analyze_danmaku(danmaku_list)
    content_analysis = AIAnalyzer.analyze_content(
        video_info,
        danmaku_list,
        analysis_result["time_density"],
        analysis_options=analysis_options,
    )
    if not content_analysis:
        raise RuntimeError("AI分析失败")

    return {
        "success": True,
        "video_info": video_info,
        "analysis": content_analysis,
        "analysis_id": analysis_id,
        "meta": {"schema_version": "1.1", "bvid": bvid, "analysis_id": analysis_id},
    }


def deep_analysis_result(
    bvid: str,
    analysis_id: str | None,
    analysis_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    log_info("深度分析任务已启动")
    subtitle_path = default_store.latest_subtitle(bvid, analysis_id)
    if not subtitle_path:
        raise ValueError("未找到字幕文件")

    danmaku_path = default_store.latest_danmaku_txt(bvid, analysis_id)
    if not danmaku_path:
        raise ValueError("未找到弹幕文件")

    log_info("正在加载本次字幕文件")
    subtitle_content = subtitle_path.read_text(encoding="utf-8")
    log_info("正在加载本次弹幕文件")

    video_info = get_video_info(bvid)
    if not video_info:
        raise RuntimeError("获取视频信息失败")

    danmaku_list = AIAnalyzer.parse_danmaku_txt(str(danmaku_path))
    if not danmaku_list:
        raise RuntimeError("读取弹幕文件失败")

    analysis_result = analyze_danmaku(danmaku_list)
    deep_result = DeepAnalysis.analyze_subtitle_and_danmaku(
        video_info,
        subtitle_content,
        danmaku_list,
        analysis_result["time_density"],
        analysis_options=analysis_options,
    )
    if not deep_result:
        raise RuntimeError("AI分析失败")

    return {
        "success": True,
        "analysis": deep_result,
        "analysis_id": analysis_id,
        "meta": {"schema_version": "1.1", "bvid": bvid, "analysis_id": analysis_id},
    }


def comment_analysis_result(
    bvid: str,
    analysis_id: str | None,
    analysis_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    log_info("评论联合分析任务已启动")
    danmaku_path = default_store.latest_danmaku_txt(bvid, analysis_id)
    if not danmaku_path:
        raise ValueError("未找到弹幕文件，请重新下载")

    comment_export = default_store.latest_comment_export(bvid, analysis_id)
    if not comment_export:
        raise ValueError("未找到评论 JSON，请重新上传")
    CommentJointAnalysis.validate_comment_export(comment_export, bvid)

    log_info("正在加载本次弹幕文件和评论 JSON")
    video_info = get_video_info(bvid)
    if not video_info:
        raise RuntimeError("获取视频信息失败")

    danmaku_list = AIAnalyzer.parse_danmaku_txt(str(danmaku_path))
    if not danmaku_list:
        raise RuntimeError("读取弹幕文件失败")

    analysis_result = analyze_danmaku(danmaku_list)
    comment_result = CommentJointAnalysis.analyze_danmaku_and_comments(
        video_info,
        danmaku_list,
        analysis_result["time_density"],
        comment_export,
        analysis_options=analysis_options,
    )
    if not comment_result:
        raise RuntimeError("AI分析失败")

    return {
        "success": True,
        "analysis": comment_result,
        "analysis_id": analysis_id,
        "meta": {"schema_version": "1.1", "bvid": bvid, "analysis_id": analysis_id},
    }


def full_analysis_result(
    bvid: str,
    analysis_id: str | None,
    analysis_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    log_info("全量综合分析任务已启动")
    subtitle_path = default_store.latest_subtitle(bvid, analysis_id)
    if not subtitle_path:
        raise ValueError("未找到字幕文件，请重新上传")

    danmaku_path = default_store.latest_danmaku_txt(bvid, analysis_id)
    if not danmaku_path:
        raise ValueError("未找到弹幕文件，请重新下载")

    comment_export = default_store.latest_comment_export(bvid, analysis_id)
    if not comment_export:
        raise ValueError("未找到评论 JSON，请重新上传")
    CommentJointAnalysis.validate_comment_export(comment_export, bvid)

    log_info("正在加载本次字幕、弹幕文件和评论 JSON")
    subtitle_content = subtitle_path.read_text(encoding="utf-8")
    video_info = get_video_info(bvid)
    if not video_info:
        raise RuntimeError("获取视频信息失败")

    danmaku_list = AIAnalyzer.parse_danmaku_txt(str(danmaku_path))
    if not danmaku_list:
        raise RuntimeError("读取弹幕文件失败")

    analysis_result = analyze_danmaku(danmaku_list)
    full_result = FullComprehensiveAnalysis.analyze_all_sources(
        video_info,
        subtitle_content,
        danmaku_list,
        analysis_result["time_density"],
        comment_export,
        analysis_options=analysis_options,
    )
    if not full_result:
        raise RuntimeError("AI分析失败")

    return {
        "success": True,
        "analysis": full_result,
        "analysis_id": analysis_id,
        "meta": {"schema_version": "1.1", "bvid": bvid, "analysis_id": analysis_id},
    }


def run_analysis_job(job: dict[str, Any]) -> None:
    job_id = job["job_id"]
    kind = job["kind"]
    payload = job.get("payload") or {}
    bvid = payload.get("bvid")
    analysis_id = payload.get("analysis_id")
    analysis_options = payload.get("analysis_options") or {}
    lease_token = job.get("lease_token")

    with job_logging_context(job_id, lease_token=lease_token):
        try:
            if kind == "content_analysis":
                result = content_analysis_result(bvid, analysis_id, analysis_options)
            elif kind == "deep_analysis":
                result = deep_analysis_result(bvid, analysis_id, analysis_options)
            elif kind == "comment_analysis":
                result = comment_analysis_result(bvid, analysis_id, analysis_options)
            elif kind == "full_analysis":
                result = full_analysis_result(bvid, analysis_id, analysis_options)
            else:
                raise ValueError(f"未知任务类型: {kind}")
            default_job_store.mark_succeeded(job_id, result, lease_token=lease_token)
        except Exception as exc:
            default_job_store.mark_failed(job_id, str(exc), lease_token=lease_token)
