"""Tests for persistent LLM response cache."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mac_bench.runtime.llm.cache import (
    CachingChatClient,
    DiskLLMCache,
    cache_enabled_from_env,
    cache_key,
    cached_acomplete,
)
from mac_bench.runtime.llm.client import ChatMessage, ChatResponse


class _CountingClient:
    def __init__(self) -> None:
        self.calls = 0
        self.config = SimpleNamespace(
            model="test-model",
            thinking_mode="disabled",
            image_input=True,
        )

    def complete(
        self,
        messages,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        self.calls += 1
        return ChatResponse(
            content=f"answer-{self.calls}-t{temperature}-m{max_tokens}",
            model="test-model",
            usage={"prompt_tokens": 3, "completion_tokens": 2},
            raw_id=f"id-{self.calls}",
            finish_reason="stop",
        )


def test_cache_key_stable_and_sensitive_to_prompt() -> None:
    msgs = [ChatMessage(role="user", content="hello")]
    a = cache_key(model="m1", messages=msgs, temperature=0.0)
    b = cache_key(model="m1", messages=msgs, temperature=0.0)
    c = cache_key(model="m1", messages=[ChatMessage(role="user", content="HELLO")], temperature=0.0)
    d = cache_key(model="m2", messages=msgs, temperature=0.0)
    e = cache_key(model="m1", messages=msgs, temperature=0.2)
    tools = [{"type": "function", "function": {"name": "exec"}}]
    f = cache_key(model="m1", messages=msgs, temperature=0.0, tools=tools)
    assert a == b
    assert a != c
    assert a != d
    assert a != e
    assert a != f


def test_disk_cache_roundtrip(tmp_path: Path) -> None:
    cache = DiskLLMCache(tmp_path)
    key = cache_key(
        model="m",
        messages=[ChatMessage(role="user", content="q")],
        temperature=0.0,
        max_tokens=100,
    )
    assert cache.get(key) is None
    response = ChatResponse(
        content="cached",
        model="m",
        usage={"prompt_tokens": 1},
        raw_id="r1",
        finish_reason="stop",
    )
    cache.put(key, response, meta={"model": "m"})
    hit = cache.get(key)
    assert hit is not None
    assert hit.content == "cached"
    assert hit.raw_id == "r1"
    assert cache.hits == 1
    assert cache.misses == 1
    assert cache.writes == 1


def test_disk_cache_preserves_reasoning_protocol_fields(tmp_path: Path) -> None:
    cache = DiskLLMCache(tmp_path)
    key = cache_key(model="m", messages=[ChatMessage("user", "q")], thinking_mode="enabled")
    details = [
        {
            "type": "reasoning.text",
            "text": "plan",
            "signature": "sig",
            "index": 0,
        }
    ]
    cache.put(
        key,
        ChatResponse(
            content="",
            model="m",
            usage={"completion_tokens": 4},
            finish_reason="tool_calls",
            assistant_message={
                "role": "assistant",
                "reasoning_content": "plan",
                "reasoning_details": details,
            },
        ),
    )

    hit = cache.get(key)

    assert hit is not None
    assert hit.assistant_message is not None
    assert hit.assistant_message["reasoning_content"] == "plan"
    assert hit.assistant_message["reasoning_details"] == details


def test_caching_client_avoids_repeat_calls(tmp_path: Path) -> None:
    inner = _CountingClient()
    client = CachingChatClient(
        inner,
        model="test-model",
        cache=DiskLLMCache(tmp_path),
        thinking_mode="disabled",
    )
    messages = [ChatMessage(role="user", content="ping")]
    first = client.complete(messages, temperature=0.0, max_tokens=50)
    second = client.complete(messages, temperature=0.0, max_tokens=50)
    assert first.content == second.content
    assert inner.calls == 1
    # Different max_tokens → miss
    third = client.complete(messages, temperature=0.0, max_tokens=51)
    assert third.content != first.content
    assert inner.calls == 2
    assert client.config is inner.config


def test_cache_enabled_from_env() -> None:
    assert cache_enabled_from_env({"MAC_BENCH_LLM_CACHE": "1"}) is True
    assert cache_enabled_from_env({"MAC_BENCH_LLM_CACHE": "0"}) is False
    assert cache_enabled_from_env({}) is True


def test_cached_acomplete_roundtrip_with_tools(tmp_path: Path) -> None:
    import asyncio

    cache = DiskLLMCache(tmp_path)
    message = SimpleNamespace(
        content=None,
        tool_calls=[{"id": "1", "function": {"name": "exec", "arguments": "{}"}}],
        model_dump=lambda exclude_none=True: {
            "role": "assistant",
            "tool_calls": [{"id": "1", "function": {"name": "exec", "arguments": "{}"}}],
        },
    )
    live = SimpleNamespace(
        id="litellm-1",
        model="openai/m",
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5),
    )

    async def _run() -> None:
        with patch(
            "mac_bench.runtime.llm.cache.gateway_acomplete",
            new=AsyncMock(return_value=live),
        ) as mocked:
            first = await cached_acomplete(
                model="m",
                messages=[{"role": "user", "content": "run"}],
                tools=[{"type": "function", "function": {"name": "exec"}}],
                temperature=0.0,
                max_tokens=32,
                cache=cache,
                cache_enabled=True,
            )
            second = await cached_acomplete(
                model="m",
                messages=[{"role": "user", "content": "run"}],
                tools=[{"type": "function", "function": {"name": "exec"}}],
                temperature=0.0,
                max_tokens=32,
                cache=cache,
                cache_enabled=True,
            )
        assert mocked.await_count == 1
        assert first.choices[0].message.tool_calls
        assert second.choices[0].message.tool_calls
        assert cache.writes == 1
        assert cache.hits >= 1

    asyncio.run(_run())
