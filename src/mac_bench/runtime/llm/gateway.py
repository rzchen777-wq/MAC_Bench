"""Shared LiteLLM gateway for model requests."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import litellm

from mac_bench.runtime.llm.config import LLMConfig

LOG = logging.getLogger("mac_bench.runtime.llm.gateway")


def embed(*, config, inputs):
    """One explicit embedding attempt; no environment mutation or hidden retry."""
    options = {'model': resolve_model_name(config.model_name, api_base=config.base_url),
               'input': inputs, 'api_base': config.base_url,
               'api_key': config.api_key or 'not-needed',
               'timeout': config.timeout_seconds, 'num_retries': 0}
    if config.dimensions is not None:
        options['dimensions'] = config.dimensions
    return litellm.embedding(**options)

litellm.set_verbose = False
litellm.drop_params = True

_RETRYABLE_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.Timeout,
    litellm.exceptions.InternalServerError,
)

MAX_RETRIES = 5
BASE_DELAY = 1.5
MAX_DELAY = 60.0
JITTER_RANGE = 1.0


def apply_gateway_env(config: LLMConfig, *, environ: dict[str, str] | None = None) -> None:
    """Publish the selected model connection for LiteLLM."""

    env = os.environ if environ is None else environ
    env["OPENAI_API_BASE"] = config.base_url
    env["OPENAI_API_KEY"] = config.api_key or "not-needed"
    env["MAC_BENCH_LLM_THINKING"] = config.thinking_mode
    if config.model:
        env.setdefault("MAC_BENCH_LLM_MODEL", config.model)


_PROVIDER_PREFIXES = (
    "openai/",
    "deepseek/",
    "anthropic/",
    "azure/",
    "hosted_vllm/",
)


def bare_model_name(model: str) -> str:
    """Strip litellm provider prefixes so the wire model id is provider-agnostic."""

    name = (model or "").strip()
    lowered = name.lower()
    for prefix in _PROVIDER_PREFIXES:
        if lowered.startswith(prefix):
            return name[len(prefix) :]
    return name


def resolve_model_name(model: str, *, api_base: str | None = None) -> str:
    """Select the gateway while preserving OpenRouter's model namespace.

    Bare ``deepseek/...`` would use litellm's native DeepSeek provider (``/beta``),
    which is not the shared gateway path and can auth-fail under load.
    """

    base = (
        api_base
        if api_base is not None
        else (
            os.environ.get("OPENAI_API_BASE")
            or os.environ.get("MAC_BENCH_LLM_BASE_URL")
            or ""
        )
    ).strip()
    hostname = (urlparse(base).hostname or "").lower()
    if hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai"):
        return "openrouter/" + model.removeprefix("openrouter/")
    bare = bare_model_name(model)
    if base:
        return f"openai/{bare}"
    return bare


def _request_provider(*, api_base: str | None, model: str) -> str | None:
    """Identify APIs whose reasoning controls differ from OpenAI's schema."""

    hostname = (urlparse(api_base or "").hostname or "").lower()
    if hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai"):
        return "openrouter"
    if hostname == "deepseek.com" or hostname.endswith(".deepseek.com"):
        return "deepseek"

    # The URL takes precedence: OpenRouter can itself route DeepSeek models.
    model_name = bare_model_name(model).lower()
    if model_name.startswith("deepseek"):
        return "deepseek"
    return None


def _reasoning_extra_body(
    *, api_base: str | None, model: str, thinking_mode: str
) -> dict[str, Any] | None:
    provider = _request_provider(api_base=api_base, model=model)
    enabled = thinking_mode in {"enabled", "on", "true", "1"}
    disabled = thinking_mode in {"", "disabled", "off", "false", "0", "none"}

    if provider == "deepseek":
        if enabled:
            return {"thinking": {"type": "enabled"}}
        if disabled:
            return {"thinking": {"type": "disabled"}}
    elif provider == "openrouter":
        if enabled:
            return {"reasoning": {"enabled": True}}
        if disabled:
            return {"reasoning": {"effort": "none"}}
    return None


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, asyncio.CancelledError):
        return False
    # Transient DeepSeek "governor" / concurrent auth flaps.
    name = type(exc).__name__
    msg = str(exc).lower()
    if "governor" in msg or ("authentication fails" in msg and "governor" in msg):
        return True
    if name in {"AuthenticationError", "APIError"} and "governor" in msg:
        return True
    if hasattr(exc, "status_code"):
        code = exc.status_code
        if code == 429 or (isinstance(code, int) and code >= 500):
            return True
    transient_keywords = (
        "no available passageway",
        "no available channel",
        "upstream timeout",
        "upstream connection",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "model service error",
        "model service error (cn)",
    )
    return any(kw in msg for kw in transient_keywords)


def _build_kwargs(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict] | None,
    temperature: float,
    max_tokens: int | None,
    stop: list[str] | None,
    api_base: str | None,
    api_key: str | None = None,
    thinking_mode: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    base = (
        api_base
        if api_base is not None
        else (
            os.environ.get("OPENAI_API_BASE")
            or os.environ.get("MAC_BENCH_LLM_BASE_URL")
            or None
        )
    )
    if base:
        base = str(base).rstrip("/")
    resolved = resolve_model_name(model, api_base=base)
    resolved_api_key = api_key or (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("MAC_BENCH_LLM_API_KEY")
    )
    kwargs: dict[str, Any] = {
        "model": resolved,
        "messages": [dict(m) for m in messages],
        "temperature": temperature,
    }
    if base:
        kwargs["api_base"] = base
    if resolved_api_key:
        kwargs["api_key"] = resolved_api_key
    if timeout_seconds is not None:
        kwargs["timeout"] = float(timeout_seconds)
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if tools:
        kwargs["tools"] = tools
    if stop:
        kwargs["stop"] = stop
    # Reasoning controls are gateway-specific even when both expose OpenAI chat.
    thinking_raw = (
        thinking_mode
        if thinking_mode is not None
        else os.environ.get("MAC_BENCH_LLM_THINKING") or "disabled"
    ).strip().lower()
    reasoning_body = _reasoning_extra_body(
        api_base=base,
        model=model,
        thinking_mode=thinking_raw,
    )
    if reasoning_body is not None:
        kwargs["extra_body"] = reasoning_body
    return kwargs


async def acomplete(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict] | None = None,
    temperature: float = 1.0,
    max_tokens: int | None = 16384,
    stop: list[str] | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    thinking_mode: str | None = None,
    timeout_seconds: float | None = None,
    max_retries: int = MAX_RETRIES,
) -> Any:
    """Call litellm.acompletion with Meta-Team-style transient retries."""

    kwargs = _build_kwargs(
        model=model,
        messages=messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        stop=stop,
        api_base=api_base,
        api_key=api_key,
        thinking_mode=thinking_mode,
        timeout_seconds=timeout_seconds,
    )
    last_exc: Exception | None = None
    total_retry_delay = 0.0
    retries = max(0, int(max_retries))
    for attempt in range(retries + 1):
        try:
            response = await litellm.acompletion(**kwargs)
            if attempt > 0:
                LOG.info(
                    "LLM call succeeded after %d retries (total retry delay: %.1fs)",
                    attempt,
                    total_retry_delay,
                )
            return response
        except Exception as exc:
            last_exc = exc
            if attempt >= retries or not is_retryable(exc):
                if attempt > 0:
                    LOG.error(
                        "LLM call failed permanently after %d retries "
                        "(total retry delay: %.1fs): [%s] %s",
                        attempt,
                        total_retry_delay,
                        type(exc).__name__,
                        exc,
                    )
                raise
            delay = min(BASE_DELAY * (2**attempt) + random.uniform(0, JITTER_RANGE), MAX_DELAY)
            total_retry_delay += delay
            status_code = getattr(exc, "status_code", None)
            LOG.warning(
                "LLM call failed (attempt %d/%d), retrying in %.1fs: [%s] %s%s",
                attempt + 1,
                retries + 1,
                delay,
                type(exc).__name__,
                exc,
                f" (status={status_code})" if status_code else "",
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def complete(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict] | None = None,
    temperature: float = 1.0,
    max_tokens: int | None = 16384,
    stop: list[str] | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    thinking_mode: str | None = None,
    timeout_seconds: float | None = None,
    max_retries: int = MAX_RETRIES,
    client: Any = None,
) -> Any:
    """Sync litellm.completion with the same retry policy as ``acomplete``."""

    kwargs = _build_kwargs(
        model=model,
        messages=messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        stop=stop,
        api_base=api_base,
        api_key=api_key,
        thinking_mode=thinking_mode,
        timeout_seconds=timeout_seconds,
    )
    if client is not None:
        kwargs['client'] = client
    last_exc: Exception | None = None
    total_retry_delay = 0.0
    retries = max(0, int(max_retries))
    for attempt in range(retries + 1):
        try:
            response = litellm.completion(**kwargs)
            if attempt > 0:
                LOG.info(
                    "LLM call succeeded after %d retries (total retry delay: %.1fs)",
                    attempt,
                    total_retry_delay,
                )
            return response
        except Exception as exc:
            last_exc = exc
            if attempt >= retries or not is_retryable(exc):
                if attempt > 0:
                    LOG.error(
                        "LLM call failed permanently after %d retries "
                        "(total retry delay: %.1fs): [%s] %s",
                        attempt,
                        total_retry_delay,
                        type(exc).__name__,
                        exc,
                    )
                raise
            delay = min(BASE_DELAY * (2**attempt) + random.uniform(0, JITTER_RANGE), MAX_DELAY)
            total_retry_delay += delay
            LOG.warning(
                "LLM call failed (attempt %d/%d), retrying in %.1fs: [%s] %s",
                attempt + 1,
                retries + 1,
                delay,
                type(exc).__name__,
                exc,
            )
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def response_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, Mapping):
        raw = usage
    else:
        raw = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }
    return {
        str(key): int(value)
        for key, value in raw.items()
        if isinstance(value, (int, float))
    }


def serialize_litellm_response(response: Any) -> dict[str, Any]:
    """JSON-safe snapshot of a litellm response for disk cache."""

    choices_out: list[dict[str, Any]] = []
    for choice in getattr(response, "choices", None) or []:
        message = getattr(choice, "message", None)
        if message is None:
            continue
        if hasattr(message, "model_dump"):
            msg_dict = message.model_dump(exclude_none=True)
        elif isinstance(message, Mapping):
            msg_dict = dict(message)
        else:
            msg_dict = {
                "role": getattr(message, "role", "assistant"),
                "content": getattr(message, "content", None),
            }
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls is not None:
                msg_dict["tool_calls"] = tool_calls
        choices_out.append(
            {
                "message": msg_dict,
                "finish_reason": getattr(choice, "finish_reason", None),
            }
        )
    return {
        "id": getattr(response, "id", None),
        "model": getattr(response, "model", None),
        "choices": choices_out,
        "usage": response_usage(response),
    }


class _CachedLiteLLMResponse:
    """Minimal object shaped like a litellm ModelResponse for cache hits."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.id = payload.get("id")
        self.model = payload.get("model")
        self.usage = payload.get("usage") or {}
        self.choices = []
        for choice in payload.get("choices") or []:
            msg = dict(choice.get("message") or {})

            class _Message:
                def __init__(self, data: dict[str, Any]) -> None:
                    self._data = data
                    for key, value in data.items():
                        setattr(self, key, value)

                def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
                    if not exclude_none:
                        return dict(self._data)
                    return {k: v for k, v in self._data.items() if v is not None}

            self.choices.append(
                type(
                    "Choice",
                    (),
                    {
                        "message": _Message(msg),
                        "finish_reason": choice.get("finish_reason"),
                    },
                )()
            )


CachedLiteLLMResponse = _CachedLiteLLMResponse
