"""Persistent LLM response cache keyed by model + prompt (+ sampling knobs)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from mac_bench.runtime.llm.client import ChatClient, ChatMessage, ChatResponse, trace_safe_content
from mac_bench.runtime.llm.gateway import (
    CachedLiteLLMResponse,
    MAX_RETRIES,
    acomplete as gateway_acomplete,
    serialize_litellm_response,
)

LOG = logging.getLogger("mac_bench.runtime.llm.cache")

_DEFAULT_CACHE_DIR = Path("runs/llm_cache")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cache_key(
    *,
    model: str,
    messages: Sequence[ChatMessage] | Sequence[Mapping[str, Any]],
    temperature: float = 0.0,
    max_tokens: int | None = None,
    thinking_mode: str | None = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
    stop: Sequence[str] | None = None,
) -> str:
    """Stable SHA-256 over model name and the full chat prompt payload."""

    normalized_messages: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ChatMessage):
            payload = message.as_dict()
            if "content" in payload:
                payload["content"] = trace_safe_content(payload["content"])
            normalized_messages.append(payload)
        else:
            normalized_messages.append(dict(message))
    payload = {
        "model": model,
        "messages": normalized_messages,
        "temperature": float(temperature),
        "max_tokens": max_tokens,
        "thinking_mode": thinking_mode,
        "tools": list(tools) if tools else None,
        "stop": list(stop) if stop else None,
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return digest


def _response_to_dict(response: ChatResponse) -> dict[str, Any]:
    return {
        "content": response.content,
        "model": response.model,
        "usage": dict(response.usage),
        "raw_id": response.raw_id,
        "finish_reason": response.finish_reason,
        "tool_calls": [dict(item) for item in response.tool_calls],
        "assistant_message": dict(response.assistant_message or {}),
    }


def _response_from_dict(payload: Mapping[str, Any]) -> ChatResponse:
    usage_raw = payload.get("usage") or {}
    usage = {
        str(key): int(value)
        for key, value in usage_raw.items()
        if isinstance(value, (int, float))
    }
    return ChatResponse(
        content=str(payload.get("content") or ""),
        model=str(payload.get("model") or ""),
        usage=usage,
        raw_id=str(payload["raw_id"]) if payload.get("raw_id") is not None else None,
        finish_reason=(
            str(payload["finish_reason"])
            if payload.get("finish_reason") is not None
            else None
        ),
        tool_calls=tuple(
            dict(item)
            for item in payload.get("tool_calls") or ()
            if isinstance(item, Mapping)
        ),
        assistant_message=(
            dict(payload.get("assistant_message") or {})
            if isinstance(payload.get("assistant_message"), Mapping)
            else None
        ),
    )


def _normalize_entry(key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if "kind" in payload:
        return dict(payload)
    if "response" in payload:
        return {
            "schema_version": 2,
            "kind": "chat",
            "key": key,
            "meta": dict(payload.get("meta") or {}),
            "response": payload.get("response") or {},
        }
    return {
        "schema_version": 2,
        "kind": "chat",
        "key": key,
        "meta": {},
        "response": dict(payload),
    }


class DiskLLMCache:
    """One JSON file per cache key under ``root/<aa>/<fullhash>.json``."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _DEFAULT_CACHE_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def _path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def _read_entry(self, key: str) -> dict[str, Any] | None:
        path = self._path_for(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return _normalize_entry(key, payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            LOG.warning("llm cache entry unreadable, treating as miss: %s", path)
            return None

    def get(self, key: str) -> ChatResponse | None:
        entry = self._read_entry(key)
        if entry is None or (entry.get("kind") or "chat") != "chat":
            self.misses += 1
            return None
        try:
            response = _response_from_dict(entry.get("response") or {})
        except (TypeError, ValueError, KeyError):
            self.misses += 1
            return None
        self.hits += 1
        return response

    def get_litellm(self, key: str) -> Any | None:
        entry = self._read_entry(key)
        if entry is None or entry.get("kind") != "litellm":
            self.misses += 1
            return None
        self.hits += 1
        return CachedLiteLLMResponse(entry.get("response") or {})

    def put(
        self,
        key: str,
        response: ChatResponse,
        *,
        meta: Mapping[str, Any] | None = None,
    ) -> Path:
        return self.put_entry(
            key,
            kind="chat",
            response=_response_to_dict(response),
            meta=meta,
        )

    def put_entry(
        self,
        key: str,
        *,
        kind: str,
        response: Mapping[str, Any],
        meta: Mapping[str, Any] | None = None,
    ) -> Path:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "schema_version": 2,
            "kind": kind,
            "key": key,
            "meta": dict(meta or {}),
            "response": dict(response),
        }
        tmp = path.with_suffix(".json.tmp")
        with self._lock:
            tmp.write_text(
                json.dumps(body, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
            self.writes += 1
        return path

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}


def cache_enabled_from_env(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    raw = (env.get("MAC_BENCH_LLM_CACHE") or "1").strip().lower()
    return raw not in {"0", "false", "off", "no", "disabled"}


def cache_dir_from_env(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = (env.get("MAC_BENCH_LLM_CACHE_DIR") or "").strip()
    return Path(raw) if raw else _DEFAULT_CACHE_DIR


class CachingChatClient:
    """Wrap a live ``ChatClient`` with a persistent disk cache."""

    def __init__(
        self,
        inner: ChatClient,
        *,
        model: str,
        cache: DiskLLMCache | None = None,
        thinking_mode: str | None = None,
    ) -> None:
        self.inner = inner
        self.model = model
        self.thinking_mode = thinking_mode
        self.cache = cache or DiskLLMCache()

    @property
    def capabilities(self) -> frozenset[str]:
        value = getattr(self.inner, "capabilities", frozenset())
        return frozenset(value)

    @property
    def config(self) -> Any:
        """Expose the wrapped client config for run metadata and diagnostics."""

        return getattr(self.inner, "config", None)

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatResponse:
        key = cache_key(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking_mode=self.thinking_mode,
            tools=tools,
        )
        hit = self.cache.get(key)
        if hit is not None:
            LOG.debug("llm cache hit key=%s…", key[:12])
            return hit
        if tools:
            response = self.inner.complete(
                messages, temperature=temperature, max_tokens=max_tokens, tools=tools
            )
        else:
            response = self.inner.complete(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        self.cache.put(
            key,
            response,
            meta={
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking_mode": self.thinking_mode,
                "n_messages": len(messages),
                "has_tools": bool(tools),
            },
        )
        LOG.debug("llm cache miss→store key=%s…", key[:12])
        return response


_SHARED_CACHE: DiskLLMCache | None = None
_SHARED_CACHE_LOCK = threading.Lock()


def shared_disk_cache(*, cache_dir: str | Path | None = None) -> DiskLLMCache:
    """Process-wide DiskLLMCache used by ChatClient and Meta-Team patch."""

    global _SHARED_CACHE
    with _SHARED_CACHE_LOCK:
        if cache_dir is not None:
            return DiskLLMCache(cache_dir)
        if _SHARED_CACHE is None:
            _SHARED_CACHE = DiskLLMCache(cache_dir_from_env())
        return _SHARED_CACHE


async def cached_acomplete(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict] | None = None,
    temperature: float = 1.0,
    max_tokens: int | None = 16384,
    stop: list[str] | None = None,
    api_base: str | None = None,
    max_retries: int | None = None,
    cache: DiskLLMCache | None = None,
    cache_enabled: bool | None = None,
) -> Any:
    """Meta-Team-compatible async complete with shared disk cache."""

    use_cache = cache_enabled_from_env() if cache_enabled is None else bool(cache_enabled)
    disk = cache or shared_disk_cache()
    key = cache_key(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        stop=stop,
    )
    if use_cache:
        hit = disk.get_litellm(key)
        if hit is not None:
            LOG.debug("llm litellm cache hit key=%s…", key[:12])
            return hit
    response = await gateway_acomplete(
        model=model,
        messages=messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        stop=stop,
        api_base=api_base,
        max_retries=MAX_RETRIES if max_retries is None else max_retries,
    )
    if use_cache:
        disk.put_entry(
            key,
            kind="litellm",
            response=serialize_litellm_response(response),
            meta={
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "n_messages": len(messages),
                "has_tools": bool(tools),
            },
        )
        LOG.debug("llm litellm cache miss→store key=%s…", key[:12])
    return response
