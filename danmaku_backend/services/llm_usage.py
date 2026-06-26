from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import datetime
from typing import Any

from danmaku_backend.runtime.logging_bus import current_job_context
from danmaku_backend.services.database import connect_state_db, ensure_state_db
from danmaku_backend.settings import STATE_DB_PATH


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _usage_value(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _as_int(usage.get(key))
        if value is not None:
            return value
    return None


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_chars = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    other_chars = max(0, len(text) - cjk_chars)
    return max(1, int(math.ceil(cjk_chars * 1.1 + other_chars / 4)))


def _messages_text(messages: list[dict[str, str]] | None) -> str:
    try:
        return json.dumps(messages or [], ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""


def _job_payload(job_id: str | None) -> tuple[str, dict[str, Any]]:
    if not job_id:
        return "", {}
    try:
        with connect_state_db(STATE_DB_PATH) as conn:
            row = conn.execute(
                "SELECT kind, payload_json FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
    except Exception:
        return "", {}
    if not row:
        return "", {}
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}
    return str(row["kind"] or ""), payload if isinstance(payload, dict) else {}


def _extract_usage(
    response_data: dict[str, Any],
    messages: list[dict[str, str]] | None,
    content: str,
) -> tuple[dict[str, int], bool, dict[str, Any]]:
    usage = response_data.get("usage")
    raw_usage = usage if isinstance(usage, dict) else {}

    prompt_tokens = _usage_value(raw_usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_value(raw_usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_value(raw_usage, "total_tokens")
    estimated = not bool(raw_usage)

    if prompt_tokens is None:
        prompt_tokens = _estimate_tokens(_messages_text(messages))
        estimated = True
    if completion_tokens is None:
        completion_tokens = _estimate_tokens(content or "")
        estimated = True
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
        estimated = True

    return (
        {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        estimated,
        raw_usage,
    )


def _log_usage_record_error(exc: Exception) -> None:
    try:
        from logger import log_error

        log_error(f"Token 消耗记录写入失败：{exc.__class__.__name__}")
    except Exception:
        pass


def record_llm_usage(
    provider: str,
    model: str,
    response_data: dict[str, Any],
    *,
    messages: list[dict[str, str]] | None = None,
    content: str = "",
) -> None:
    try:
        job_context = current_job_context()
        job_id = job_context[0] if job_context else ""
        kind, payload = _job_payload(job_id)
        usage, estimated, raw_usage = _extract_usage(response_data, messages, content)
        now = datetime.now().astimezone()
        values = (
            now.isoformat(timespec="seconds"),
            now.date().isoformat(),
            job_id or None,
            kind or None,
            str(provider or ""),
            str(model or ""),
            usage["prompt_tokens"],
            usage["completion_tokens"],
            usage["total_tokens"],
            1 if estimated else 0,
            str(payload.get("bvid") or ""),
            str(payload.get("analysis_id") or ""),
            str(payload.get("site") or ""),
            str(payload.get("host") or ""),
            json.dumps(raw_usage, ensure_ascii=False, sort_keys=True) if raw_usage else None,
        )
    except Exception as exc:
        _log_usage_record_error(exc)
        return

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            ensure_state_db(STATE_DB_PATH)
            with connect_state_db(STATE_DB_PATH) as conn:
                conn.execute(
                    """
                    INSERT INTO llm_usage_events (
                        ts, date, job_id, kind, provider, model,
                        prompt_tokens, completion_tokens, total_tokens, estimated,
                        bvid, analysis_id, site, host, raw_usage_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() or attempt >= 2:
                break
            time.sleep(0.25 * (attempt + 1))
        except Exception as exc:
            last_error = exc
            break
    if last_error:
        _log_usage_record_error(last_error)
