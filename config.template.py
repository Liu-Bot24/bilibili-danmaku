from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_MODEL_PROVIDER = "example-openai-compatible"
DEFAULT_FALLBACK_ORDER = [
    "example-openai-compatible",
]
DEFAULT_ANALYSIS_CONFIG: dict[str, int] = {
    "content_max_samples": 1000,
    "deep_max_samples": 1000,
    "head_samples": 200,
    "peak_bucket_count": 10,
    "peak_window_seconds": 10,
    "peak_samples_per_bucket": 20,
    "peak_preview_limit": 50,
    "progress_interval_seconds": 15,
    "max_concurrent_jobs": 3,
    "content_max_concurrent_jobs": 2,
    "deep_max_concurrent_jobs": 1,
    "running_job_timeout_seconds": 900,
}
# Copy this file to config.py after deployment.
# Keep real keys in environment variables or in BILI_DANMAKU_SECRET_FILE.
CONFIG_ROOT = Path(__file__).resolve().parent
SECRET_FILE = Path(
    os.getenv(
        "BILI_DANMAKU_SECRET_FILE",
        str(CONFIG_ROOT / ".secrets" / "bilibili_danmaku.secrets.json"),
    )
)
MODEL_CONFIG_FILE = Path(
    os.getenv(
        "BILI_DANMAKU_MODEL_CONFIG_FILE",
        str(CONFIG_ROOT / ".secrets" / "bilibili_danmaku.model_config.json"),
    )
)

MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "example-openai-compatible": {
        "label": "Example OpenAI-Compatible Model",
        "model": os.getenv("BILI_DANMAKU_EXAMPLE_MODEL", "your-model-name"),
        "endpoint": os.getenv(
            "BILI_DANMAKU_EXAMPLE_ENDPOINT",
            "https://example.com/v1/chat/completions",
        ),
        "key_env": "BILI_DANMAKU_EXAMPLE_KEY",
        "context_window": 128000,
        "max_prompt_chars": 100000,
        "max_tokens": 6000,
        "temperature": 0.9,
        "connect_timeout": 10,
        "read_timeout": 180,
        "request_deadline": 220,
        "max_retries": 0,
    },
}

DEFAULT_CONNECT_TIMEOUT = float(os.getenv("BILI_DANMAKU_LLM_CONNECT_TIMEOUT", "5"))
DEFAULT_READ_TIMEOUT = float(os.getenv("BILI_DANMAKU_LLM_READ_TIMEOUT", "45"))
DEFAULT_REQUEST_DEADLINE = float(os.getenv("BILI_DANMAKU_LLM_REQUEST_DEADLINE", "50"))
DEFAULT_MAX_RETRIES = int(os.getenv("BILI_DANMAKU_LLM_MAX_RETRIES", "1"))
DEFAULT_RETRY_BACKOFF = float(os.getenv("BILI_DANMAKU_LLM_RETRY_BACKOFF", "0.5"))

# Backward-compatible name used by legacy imports. Keep below uWSGI harakiri=60.
DEFAULT_TIMEOUT = DEFAULT_READ_TIMEOUT


def _load_secret_file() -> dict[str, str]:
    if not SECRET_FILE.exists():
        return {}
    try:
        data = json.loads(SECRET_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(key): str(value) for key, value in data.items() if value}


def _load_model_config_file() -> dict[str, Any]:
    if not MODEL_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(MODEL_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid AI model config file: {MODEL_CONFIG_FILE}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"AI model config file must be a JSON object: {MODEL_CONFIG_FILE}")
    return data


def _as_provider_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _validate_provider(provider_name: str) -> str:
    if provider_name not in MODEL_CONFIGS:
        raise RuntimeError(f"Unknown AI provider: {provider_name}")
    return provider_name


def _valid_provider(provider_name: Any) -> str | None:
    name = str(provider_name or "").strip()
    return name if name in MODEL_CONFIGS else None


def _normalize_fallback_order(active_provider: str, fallback_order: list[str]) -> list[str]:
    normalized: list[str] = []
    for provider_name in [active_provider, *fallback_order, *DEFAULT_FALLBACK_ORDER]:
        provider_name = _valid_provider(provider_name)
        if provider_name and provider_name not in normalized:
            normalized.append(provider_name)
    return normalized


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _analysis_config(file_config: dict[str, Any]) -> dict[str, int]:
    raw_config = file_config.get("analysis", {})
    if not isinstance(raw_config, dict):
        raw_config = {}
    config = dict(DEFAULT_ANALYSIS_CONFIG)
    for key, default in DEFAULT_ANALYSIS_CONFIG.items():
        config[key] = _positive_int(raw_config.get(key), default)
    return config


def get_model_runtime_config() -> dict[str, Any]:
    file_config = _load_model_config_file()
    active_provider = (
        os.getenv("BILI_DANMAKU_MODEL")
        or os.getenv("LLM_ACTIVE_PROVIDER")
        or file_config.get("active_provider")
        or file_config.get("provider")
        or DEFAULT_MODEL_PROVIDER
    )

    env_fallback_order = _as_provider_list(os.getenv("BILI_DANMAKU_FALLBACK_ORDER"))
    file_fallback_order = _as_provider_list(file_config.get("fallback_order") or file_config.get("fallbacks"))
    fallback_order = env_fallback_order or file_fallback_order or list(DEFAULT_FALLBACK_ORDER)

    normalized_order = _normalize_fallback_order(str(active_provider), fallback_order)
    normalized_active_provider = _valid_provider(active_provider) or normalized_order[0]

    return {
        "active_provider": normalized_active_provider,
        "fallback_order": normalized_order,
        "provider_overrides": file_config.get("provider_overrides", {})
        if isinstance(file_config.get("provider_overrides"), dict)
        else {},
        "analysis": _analysis_config(file_config),
        "config_file": str(MODEL_CONFIG_FILE),
    }


def get_active_provider() -> str:
    return get_model_runtime_config()["active_provider"]


def get_fallback_order(provider: str | None = None) -> list[str]:
    runtime_config = get_model_runtime_config()
    if provider is None:
        return list(runtime_config["fallback_order"])
    return _normalize_fallback_order(provider, runtime_config["fallback_order"])


def get_analysis_config() -> dict[str, int]:
    return dict(get_model_runtime_config()["analysis"])


def get_ai_config(provider: str | None = None) -> dict[str, Any]:
    provider_name = _validate_provider(provider or get_active_provider())

    config = dict(MODEL_CONFIGS[provider_name])
    provider_overrides = get_model_runtime_config().get("provider_overrides", {})
    overrides = provider_overrides.get(provider_name, {})
    if isinstance(overrides, dict):
        config.update(overrides)
    secrets = _load_secret_file()
    key_env = config["key_env"]
    api_key = (
        os.getenv(key_env)
        or os.getenv("BILI_DANMAKU_API_KEY")
        or secrets.get(key_env)
        or secrets.get(provider_name)
        or secrets.get("api_key")
    )
    if not api_key:
        raise RuntimeError(f"Missing API key for AI provider: {provider_name}")
    config["key"] = api_key
    config["provider"] = provider_name
    return config


def maybe_ai_config(provider: str | None = None) -> dict[str, Any]:
    try:
        return get_ai_config(provider)
    except RuntimeError:
        provider_name = provider or DEFAULT_MODEL_PROVIDER
        config = dict(MODEL_CONFIGS.get(provider_name, {}))
        config["key"] = ""
        config["provider"] = provider_name
        return config


AI_CONFIG = maybe_ai_config()
DEFAULT_HEADERS = {"Content-Type": "application/json"}
if AI_CONFIG.get("key"):
    DEFAULT_HEADERS["Authorization"] = f"Bearer {AI_CONFIG['key']}"


def get_app_access_token() -> str:
    secrets = _load_secret_file()
    return (
        os.getenv("BILI_DANMAKU_APP_TOKEN")
        or secrets.get("BILI_DANMAKU_APP_TOKEN")
        or secrets.get("app_access_token")
        or ""
    )


def get_baidu_submit_token() -> str:
    secrets = _load_secret_file()
    return (
        os.getenv("BILI_DANMAKU_BAIDU_SUBMIT_TOKEN")
        or secrets.get("BILI_DANMAKU_BAIDU_SUBMIT_TOKEN")
        or secrets.get("baidu_submit_token")
        or ""
    )
