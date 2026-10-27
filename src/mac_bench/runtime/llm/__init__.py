"""Provider-neutral interfaces for OpenAI-compatible language models."""

from __future__ import annotations

from typing import Any

from .client import ChatMessage, ChatResponse, LiteLLMChatClient
from .config import LLMConfig

__all__ = [
    "CachingChatClient",
    "ChatMessage",
    "ChatResponse",
    "DiskLLMCache",
    "LLMConfig",
    "LiteLLMChatClient",
    "build_live_client",
    "cache_dir_from_env",
    "cache_enabled_from_env",
    "cached_acomplete",
    "shared_disk_cache",
]

_CACHE_EXPORTS = frozenset(
    {
        "CachingChatClient",
        "DiskLLMCache",
        "cache_dir_from_env",
        "cache_enabled_from_env",
        "cached_acomplete",
        "shared_disk_cache",
    }
)


def __getattr__(name: str) -> Any:
    if name in _CACHE_EXPORTS:
        from . import cache

        return getattr(cache, name)
    raise AttributeError(name)


def build_live_client(
    config: LLMConfig | None = None,
    *,
    cache: bool | None = None,
    cache_dir: str | None = None,
    publish_env: bool = True,
) -> Any:
    """Create a live litellm-backed client, optionally disk-cached.

    Cache defaults on (``MAC_BENCH_LLM_CACHE=1``). Disable with ``MAC_BENCH_LLM_CACHE=0``.
    Directory defaults to ``runs/llm_cache`` (``MAC_BENCH_LLM_CACHE_DIR`` overrides).
    """

    cfg = config or LLMConfig.from_env()
    inner = LiteLLMChatClient(cfg, publish_env=publish_env)
    from .cache import (
        CachingChatClient,
        DiskLLMCache,
        cache_dir_from_env,
        cache_enabled_from_env,
        shared_disk_cache,
    )

    use_cache = cache_enabled_from_env() if cache is None else bool(cache)
    if not use_cache:
        return inner
    from pathlib import Path

    root = Path(cache_dir) if cache_dir is not None else cache_dir_from_env()
    # Prefer the process-wide cache so Meta-Team monkeypatch shares entries.
    disk = shared_disk_cache() if cache_dir is None else DiskLLMCache(root)
    return CachingChatClient(
        inner,
        model=cfg.model,
        cache=disk,
        thinking_mode=cfg.thinking_mode,
    )
