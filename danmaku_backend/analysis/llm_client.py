from __future__ import annotations

import ast
import json
from queue import Empty, Queue
import re
from threading import Thread
import time
from typing import Any

import requests

from config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_REQUEST_DEADLINE,
    DEFAULT_RETRY_BACKOFF,
    get_ai_config,
    get_analysis_config,
    get_fallback_order,
)
from logger import log_api, log_error, log_info


class LLMClient:
    retry_statuses = {429, 500, 502, 503, 504}
    quote_system_prompt = "如果要使用引号，请使用直角引号「」。"

    def __init__(
        self,
        provider: str | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        deadline: float = DEFAULT_REQUEST_DEADLINE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    ):
        self.provider = provider
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.deadline = deadline
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.session = requests.Session()

    def chat_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any] | None:
        messages = [{"role": "system", "content": system_prompt or self.quote_system_prompt}]
        messages.append({"role": "user", "content": prompt})

        providers = get_fallback_order(self.provider)
        failures: list[str] = []
        for index, provider_name in enumerate(providers):
            try:
                config = get_ai_config(provider_name)
            except RuntimeError:
                log_error("文本分析服务配置不完整，正在尝试备用通道...")
                failures.append(provider_name)
                continue

            result = self._chat_json_with_config(
                config,
                [dict(message) for message in messages],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if result is not None:
                return result

            failures.append(provider_name)
            if index < len(providers) - 1:
                log_error("当前文本分析通道暂时不可用，正在切换备用通道...")

        log_error("所有文本分析通道暂时不可用，请稍后重试")
        return None

    def _chat_json_with_config(
        self,
        config: dict[str, Any],
        messages: list[dict[str, str]],
        *,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any] | None:
        payload = {
            "model": config["model"],
            "messages": messages,
            "temperature": config["temperature"] if temperature is None else temperature,
            "max_tokens": config["max_tokens"] if max_tokens is None else max_tokens,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['key']}",
        }
        connect_timeout = float(config.get("connect_timeout", self.connect_timeout))
        read_timeout = float(config.get("read_timeout", self.read_timeout))
        deadline = float(config.get("request_deadline", self.deadline))
        max_retries = int(config.get("max_retries", self.max_retries))
        retry_backoff = float(config.get("retry_backoff", self.retry_backoff))
        progress_interval = get_analysis_config()["progress_interval_seconds"]

        started_at = time.monotonic()
        last_error: Exception | None = None
        log_api("正在调用文本分析服务...")
        for attempt in range(max_retries + 1):
            remaining = deadline - (time.monotonic() - started_at)
            if remaining <= 0:
                break
            timeout = (connect_timeout, min(read_timeout, max(1.0, remaining)))
            try:
                response = self._post_with_progress(
                    config["endpoint"],
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                    deadline=remaining,
                    progress_interval=progress_interval,
                )
                if response.status_code in self.retry_statuses and attempt < max_retries:
                    time.sleep(min(retry_backoff, max(0.0, remaining)))
                    continue
                if response.status_code != 200:
                    log_error(f"文本分析服务返回错误: {response.status_code}")
                    return None
                log_api("文本分析服务已返回结果，正在整理报告...")
                data = response.json()
                message = data["choices"][0]["message"]
                content = (
                    message.get("content")
                    or message.get("reasoning_content")
                    or message.get("reasoning")
                    or ""
                )
                parsed = self.extract_json(content)
                if parsed is not None:
                    return parsed
                return None
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                time.sleep(retry_backoff)
        log_error(LLMClient._describe_request_error(last_error))
        return None

    @staticmethod
    def _post_with_progress(
        endpoint: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: tuple[float, float],
        deadline: float,
        progress_interval: int,
    ) -> requests.Response:
        result_queue: Queue[tuple[str, Any]] = Queue(maxsize=1)

        def worker() -> None:
            try:
                response = requests.post(endpoint, headers=headers, json=json, timeout=timeout)
                result_queue.put(("response", response))
            except Exception as exc:
                result_queue.put(("error", exc))

        thread = Thread(target=worker, daemon=True)
        thread.start()

        started_at = time.monotonic()
        next_progress_at = max(1, progress_interval)
        while True:
            elapsed = time.monotonic() - started_at
            remaining = deadline - elapsed
            if remaining <= 0:
                raise requests.exceptions.Timeout("request deadline exceeded")
            try:
                kind, value = result_queue.get(timeout=min(1.0, max(0.1, remaining)))
            except Empty:
                elapsed = time.monotonic() - started_at
                if elapsed >= next_progress_at:
                    log_info(f"文本分析仍在执行，已等待 {int(elapsed)} 秒")
                    next_progress_at += max(1, progress_interval)
                continue
            if kind == "response":
                return value
            raise value

    def _describe_request_error(exc: Exception | None) -> str:
        if exc is None:
            return "文本分析服务未在预期时间内返回"
        if isinstance(exc, requests.exceptions.Timeout):
            return "文本分析服务响应超时"
        if isinstance(exc, requests.exceptions.ConnectionError):
            return "文本分析服务连接失败"
        return f"文本分析服务调用异常: {exc.__class__.__name__}"

    @staticmethod
    def _strip_code_fences(content: str) -> str:
        return (content or "").replace("```json", "").replace("```", "").strip()

    @staticmethod
    def _normalize_json_like_text(text: str) -> str:
        normalized = (text or "").strip()
        replacements = {
            "“": '"',
            "”": '"',
            "‘": "'",
            "’": "'",
            "（": "(",
            "）": ")",
        }
        for source, target in replacements.items():
            normalized = normalized.replace(source, target)
        normalized = re.sub(r",\s*([}\]])", r"\1", normalized)
        return normalized.strip()

    @staticmethod
    def _parse_dict_candidate(text: str) -> dict[str, Any] | None:
        candidate = (text or "").strip()
        if not candidate:
            return None
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

        try:
            parsed = ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        return None

    @staticmethod
    def _extract_balanced_object(text: str, start: int) -> str | None:
        if start < 0 or start >= len(text) or text[start] != "{":
            return None
        depth = 0
        in_string = False
        escape = False
        quote_char = ""
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == quote_char:
                    in_string = False
                continue
            if char in {'"', "'"}:
                in_string = True
                quote_char = char
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return None

    @staticmethod
    def extract_json(content: str) -> dict[str, Any] | None:
        decoder = json.JSONDecoder()
        raw_text = LLMClient._strip_code_fences(content)

        def try_parse(text: str) -> dict[str, Any] | None:
            direct = LLMClient._parse_dict_candidate(text)
            if direct is not None:
                return direct

            candidates = [match.start() for match in re.finditer(r"\{", text)]
            for start in candidates:
                block = LLMClient._extract_balanced_object(text, start)
                if block:
                    parsed = LLMClient._parse_dict_candidate(block)
                    if parsed is not None:
                        return parsed
                try:
                    parsed, _ = decoder.raw_decode(text[start:])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
            return None

        parsed = try_parse(raw_text)
        if parsed is not None:
            return parsed

        normalized = LLMClient._normalize_json_like_text(raw_text)
        if normalized != raw_text:
            parsed = try_parse(normalized)
            if parsed is not None:
                return parsed

        log_error("分析结果格式不完整，无法生成报告")
        return None
