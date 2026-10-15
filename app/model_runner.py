"""Run a prompt package through a model adapter and validate the response."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, sleep as default_sleep
from typing import Any, Callable, Protocol

from app.paths import PROJECT_ROOT


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "model_outputs"
ADAPTER_CHOICES = ("mock", "deepseek")
MODEL_CALL_METRIC_KEYS = (
    "http_status",
    "provider",
    "model",
    "reasoning_effort",
    "thinking_mode",
    "status",
    "latency_seconds",
    "request_attempts",
    "retry_count",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_prompt_tokens",
    "cache_miss_prompt_tokens",
    "reasoning_tokens",
    "cost_usd",
    "request_id",
    "finish_reason",
    "error_type",
)


class ModelAdapter(Protocol):
    def generate(self, prompt_package: dict[str, Any]) -> dict[str, Any] | str:
        """Generate one response for a prepared prompt package."""


def _value(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_model_call_metrics(adapter: ModelAdapter) -> dict[str, Any] | None:
    getter = getattr(adapter, "get_last_call_metrics", None)
    if not callable(getter):
        return None
    metrics = getter()
    return dict(metrics) if metrics is not None else None


def reset_model_call_metrics(adapter: ModelAdapter) -> None:
    resetter = getattr(adapter, "reset_last_call_metrics", None)
    if callable(resetter):
        resetter()


class ModelConfigurationError(RuntimeError):
    """Raised when a real model adapter is not configured safely."""


class ModelInvocationError(RuntimeError):
    """Raised when a model request fails or returns unusable output."""


@dataclass(frozen=True)
class DeepSeekConfig:
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    reasoning_effort: str = "low"
    max_tokens: int = 8192
    timeout_seconds: float = 180.0
    max_retries: int = 2

    @classmethod
    def from_environment(cls) -> "DeepSeekConfig":
        reasoning_effort = os.environ.get(
            "DEEPSEEK_REASONING_EFFORT",
            "low",
        ).lower()
        if reasoning_effort not in {"low", "high", "max"}:
            raise ModelConfigurationError(
                "DEEPSEEK_REASONING_EFFORT 只能是 low、high 或 max"
            )
        try:
            max_tokens = int(os.environ.get("DEEPSEEK_MAX_TOKENS", "8192"))
            timeout_seconds = float(
                os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "180")
            )
            max_retries = int(os.environ.get("DEEPSEEK_MAX_RETRIES", "2"))
        except ValueError as error:
            raise ModelConfigurationError(
                "DeepSeek 数值配置必须是合法数字"
            ) from error
        if max_tokens <= 0 or timeout_seconds <= 0 or max_retries < 0:
            raise ModelConfigurationError(
                "DeepSeek 的 max_tokens、timeout 必须大于 0，重试次数不能为负"
            )
        return cls(
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
            base_url=os.environ.get(
                "DEEPSEEK_BASE_URL",
                "https://api.deepseek.com",
            ).rstrip("/"),
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )


class DeepSeekAdapter:
    """Call DeepSeek through its OpenAI-compatible Chat Completions API."""

    def __init__(
        self,
        config: DeepSeekConfig | None = None,
        client: Any | None = None,
        sleep_func: Callable[[float], None] = default_sleep,
        api_key: str | None = None,
    ) -> None:
        self.config = config or DeepSeekConfig.from_environment()
        self._sleep = sleep_func
        self._last_call_metrics: dict[str, Any] | None = None
        self._thinking_mode = "enabled"
        if client is not None:
            self.client = client
            return

        api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ModelConfigurationError(
                "未设置 DEEPSEEK_API_KEY。请在当前 PowerShell 运行："
                "$env:DEEPSEEK_API_KEY=\"你的Key\"。不要把 Key 写进代码或提交到 Git。"
            )
        try:
            from openai import OpenAI
        except ImportError as error:
            raise ModelConfigurationError(
                "缺少 openai SDK，请运行 python -m pip install -r requirements.txt"
            ) from error

        self.client = OpenAI(
            api_key=api_key,
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
            # Retries are handled here so the actual retry count is observable.
            max_retries=0,
        )

    def reset_last_call_metrics(self) -> None:
        self._last_call_metrics = None

    def get_last_call_metrics(self) -> dict[str, Any] | None:
        if self._last_call_metrics is None:
            return None
        return dict(self._last_call_metrics)

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        status_code = getattr(error, "status_code", None)
        if status_code in {408, 409, 429}:
            return True
        if isinstance(status_code, int) and status_code >= 500:
            return True
        return isinstance(error, (TimeoutError, ConnectionError)) or (
            type(error).__name__
            in {
                "APIConnectionError",
                "APITimeoutError",
                "RateLimitError",
                "InternalServerError",
            }
        )

    def _response_metrics(
        self,
        response: Any,
        *,
        started_at: float,
        attempts: int,
        status: str,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        usage = _value(response, "usage")
        prompt_details = _value(usage, "prompt_tokens_details")
        completion_details = _value(usage, "completion_tokens_details")
        cached_prompt_tokens = _value(usage, "prompt_cache_hit_tokens")
        if cached_prompt_tokens is None:
            cached_prompt_tokens = _value(prompt_details, "cached_tokens")
        reasoning_tokens = _value(usage, "reasoning_tokens")
        if reasoning_tokens is None:
            reasoning_tokens = _value(
                completion_details,
                "reasoning_tokens",
            )
        cost = _value(usage, "cost_usd")
        if cost is None:
            cost = _value(usage, "total_cost")
        choices = _value(response, "choices") or []
        finish_reason = (
            _value(choices[0], "finish_reason") if choices else None
        )
        return {
            "provider": "deepseek",
            "model": _value(response, "model") or self.config.model,
            "reasoning_effort": self.config.reasoning_effort if self._thinking_mode == "enabled" else None,
            "thinking_mode": self._thinking_mode,
            "status": status,
            "latency_seconds": round(perf_counter() - started_at, 6),
            "request_attempts": attempts,
            "retry_count": max(0, attempts - 1),
            "prompt_tokens": _optional_int(_value(usage, "prompt_tokens")),
            "completion_tokens": _optional_int(
                _value(usage, "completion_tokens")
            ),
            "total_tokens": _optional_int(_value(usage, "total_tokens")),
            "cached_prompt_tokens": _optional_int(cached_prompt_tokens),
            "cache_miss_prompt_tokens": _optional_int(
                _value(usage, "prompt_cache_miss_tokens")
            ),
            "reasoning_tokens": _optional_int(reasoning_tokens),
            "cost_usd": _optional_float(cost),
            "request_id": (
                _value(response, "_request_id")
                or _value(response, "request_id")
                or _value(response, "id")
            ),
            "finish_reason": finish_reason,
            "error_type": error_type,
        }

    def generate(self, prompt_package: dict[str, Any]) -> str:
        self.reset_last_call_metrics()
        # This option is set by our prompt builder, never parsed from log text.
        self._thinking_mode = prompt_package.get("thinking_mode", "enabled")
        if self._thinking_mode not in {"enabled", "disabled"}:
            raise ModelConfigurationError("thinking_mode 必须为 enabled 或 disabled")
        thinking_options = {"extra_body": {"thinking": {"type": self._thinking_mode}}}
        if self._thinking_mode == "enabled":
            thinking_options["reasoning_effort"] = self.config.reasoning_effort
        started_at = perf_counter()
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=prompt_package["messages"],
                    response_format={"type": "json_object"},
                    **thinking_options,
                    max_tokens=self.config.max_tokens,
                    stream=False,
                )
                break
            except Exception as error:
                retryable = self._is_retryable(error)
                if retryable and attempts <= self.config.max_retries:
                    self._sleep(min(2 ** (attempts - 1), 8))
                    continue
                self._last_call_metrics = self._response_metrics(
                    None,
                    started_at=started_at,
                    attempts=attempts,
                    status="failed",
                    error_type=type(error).__name__,
                )
                http_status = getattr(error, 'status_code', None)
                self._last_call_metrics['http_status'] = http_status if type(http_status) is int else None
                raise ModelInvocationError(
                    f"DeepSeek 请求失败：{type(error).__name__}: {error}"
                ) from error

        if not getattr(response, "choices", None):
            self._last_call_metrics = self._response_metrics(
                response,
                started_at=started_at,
                attempts=attempts,
                status="invalid_response",
                error_type="MissingChoices",
            )
            raise ModelInvocationError("DeepSeek 响应中没有 choices")
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            self._last_call_metrics = self._response_metrics(
                response,
                started_at=started_at,
                attempts=attempts,
                status="truncated",
                error_type="OutputTruncated",
            )
            raise ModelInvocationError(
                "DeepSeek 输出被截断，请提高 DEEPSEEK_MAX_TOKENS"
            )
        content = getattr(choice.message, "content", None)
        if not content or not str(content).strip():
            self._last_call_metrics = self._response_metrics(
                response,
                started_at=started_at,
                attempts=attempts,
                status="invalid_response",
                error_type="EmptyContent",
            )
            raise ModelInvocationError("DeepSeek 返回了空内容")
        self._last_call_metrics = self._response_metrics(
            response,
            started_at=started_at,
            attempts=attempts,
            status="completed",
        )
        return str(content)
