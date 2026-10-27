from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .config import LLMConfig


def apply_gateway_env(config: LLMConfig) -> None:
    from .gateway import apply_gateway_env as apply

    apply(config)


def gateway_complete(**kwargs: Any) -> Any:
    from .gateway import complete

    return complete(**kwargs)


def response_usage(response: Any) -> dict[str, int]:
    from .gateway import response_usage as read_usage

    return read_usage(response)


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str | Sequence[Mapping[str, Any]] | None = None
    tool_call_id: str | None = None
    tool_calls: Sequence[Mapping[str, Any]] = ()
    reasoning: str | None = None
    reasoning_content: str | None = None
    reasoning_details: Sequence[Mapping[str, Any]] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            payload["content"] = _content_copy(self.content)
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [dict(item) for item in self.tool_calls]
        if self.reasoning is not None:
            payload["reasoning"] = self.reasoning
        if self.reasoning_content is not None:
            payload["reasoning_content"] = self.reasoning_content
        if self.reasoning_details:
            payload["reasoning_details"] = _mapping_sequence_copy(self.reasoning_details)
        return payload

    @property
    def has_reasoning(self) -> bool:
        return bool(self.reasoning or self.reasoning_content or self.reasoning_details)

    def trace_dict(self) -> dict[str, Any]:
        payload = self.as_dict()
        if "content" in payload:
            payload["content"] = trace_safe_content(payload["content"])
        return payload


@dataclass(frozen=True)
class ChatResponse:
    content: str
    model: str
    usage: Mapping[str, int]
    raw_id: str | None = None
    finish_reason: str | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    assistant_message: Mapping[str, Any] | None = None


class ChatClient(Protocol):
    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatResponse: ...


class ModelOutputTruncatedError(RuntimeError):
    """The provider exhausted its output allowance before completing a response."""
    def __init__(self, message, *, response=None):
        super().__init__(message)
        self.response = response


def _assistant_content(message: Any, *, finish_reason: str | None) -> str:
    """Return the user-visible assistant text from a litellm/OpenAI message."""

    finish = (finish_reason or "unknown").lower()
    if finish in {"length", "max_tokens"}:
        raise ModelOutputTruncatedError(
            "LLM output was truncated before the assistant response completed "
            f"(finish_reason={finish!r}). Increase the per-call model max_tokens setting."
        )

    content = getattr(message, "content", None)
    if content is None and isinstance(message, Mapping):
        content = message.get("content")
    if content is not None and str(content).strip():
        return str(content)
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls is None and isinstance(message, Mapping):
        tool_calls = message.get("tool_calls")
    if tool_calls:
        return ""
    reasoning = _message_field(message, "reasoning_content")
    if reasoning is None:
        reasoning = _message_field(message, "reasoning")
    if reasoning is None:
        reasoning = _message_field(message, "reasoning_details")
    if reasoning:
        raise RuntimeError(
            "LLM returned reasoning but no assistant content or tool calls "
            f"(finish_reason={finish!r})."
        )
    raise RuntimeError(f"LLM returned empty assistant content (finish_reason={finish!r})")


class LiteLLMChatClient:
    """ChatClient backed by the shared Meta-Team-style litellm gateway."""

    def __init__(self, config: LLMConfig, *, publish_env: bool = True) -> None:
        if not config.api_key:
            raise RuntimeError(
                "An LLM API key is required to create a live client; "
                "set MAC_BENCH_LLM_API_KEY or OPENAI_API_KEY (or DEV_API_KEY)."
            )
        self._config = config
        if publish_env:
            apply_gateway_env(config)

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"image_input"}) if self._config.image_input else frozenset()

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatResponse:
        payload = [message.as_dict() for message in messages]
        response = gateway_complete(
            model=self._config.model,
            messages=payload,
            tools=[dict(item) for item in tools] if tools else None,
            temperature=temperature,
            max_tokens=max_tokens,
            api_base=self._config.base_url,
            api_key=self._config.api_key,
            thinking_mode=self._config.thinking_mode,
            timeout_seconds=self._config.timeout_seconds,
            max_retries=self._config.max_retries,
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("LLM response did not contain a chat message")
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise RuntimeError("LLM response did not contain a chat message")
        finish_reason = getattr(choice, "finish_reason", None)
        truncated = str(finish_reason).lower() in {'length', 'max_tokens'}
        if truncated:
            raw_content = message.get('content') if isinstance(message, Mapping) else getattr(message, 'content', None)
            content = str(raw_content) if raw_content is not None else ''
        else:
            content = _assistant_content(message, finish_reason=finish_reason)
        if hasattr(message, "model_dump"):
            assistant_message = message.model_dump(exclude_none=True)
        elif isinstance(message, Mapping):
            assistant_message = dict(message)
        else:
            assistant_message = {"role": "assistant", "content": content}
            raw_tool_calls = getattr(message, "tool_calls", None)
            if raw_tool_calls:
                assistant_message["tool_calls"] = _tool_calls_to_dicts(raw_tool_calls)
        tool_calls = tuple(_tool_calls_to_dicts(assistant_message.get("tool_calls") or ()))
        result = ChatResponse(
            content=content,
            model=str(getattr(response, "model", None) or self._config.model),
            usage=response_usage(response),
            raw_id=str(response.id) if getattr(response, "id", None) is not None else None,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            tool_calls=tool_calls,
            assistant_message=assistant_message,
        )
        if truncated:
            raise ModelOutputTruncatedError(
                'LLM output was truncated before the assistant response completed '
                f'(finish_reason={finish_reason!r}). Increase the per-call model max_tokens setting.',
                response=result)
        return result


def assistant_chat_message(
    response: ChatResponse,
    *,
    tool_calls: Sequence[Mapping[str, Any]] | None = None,
) -> ChatMessage:
    """Build a history message while preserving provider reasoning protocol fields."""

    raw = dict(response.assistant_message or {})
    provider_fields = raw.get("provider_specific_fields")
    if not isinstance(provider_fields, Mapping):
        provider_fields = {}

    def field(name: str) -> Any:
        value = raw.get(name)
        return provider_fields.get(name) if value is None else value

    raw_details = field("reasoning_details")
    details = (
        tuple(_mapping_sequence_copy(raw_details))
        if isinstance(raw_details, Sequence) and not isinstance(raw_details, (str, bytes))
        else ()
    )
    return ChatMessage(
        role="assistant",
        content=response.content or None,
        tool_calls=tuple(dict(item) for item in (response.tool_calls if tool_calls is None else tool_calls)),
        reasoning=_optional_text(field("reasoning")),
        reasoning_content=_optional_text(field("reasoning_content")),
        reasoning_details=details,
    )


def _tool_calls_to_dicts(tool_calls: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for call in tool_calls or ():
        if hasattr(call, "model_dump"):
            result.append(call.model_dump(exclude_none=True))
        elif isinstance(call, Mapping):
            result.append(dict(call))
        else:
            function = getattr(call, "function", None)
            function_payload: dict[str, Any] = {}
            if function is not None:
                function_payload = {
                    "name": getattr(function, "name", None),
                    "arguments": getattr(function, "arguments", None),
                }
            result.append(
                {
                    "id": getattr(call, "id", None),
                    "type": getattr(call, "type", "function"),
                    "function": function_payload,
                }
            )
    return result


def _content_copy(content: Any) -> Any:
    if isinstance(content, str) or content is None:
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        return [dict(item) if isinstance(item, Mapping) else item for item in content]
    raise TypeError("chat message content must be text or a sequence of content blocks")


def _message_field(message: Any, name: str) -> Any:
    value = getattr(message, name, None)
    if value is None and isinstance(message, Mapping):
        value = message.get(name)
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _mapping_sequence_copy(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy structured reasoning blocks without changing their order or values."""

    import copy

    return [copy.deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]


def trace_safe_content(content: Any) -> Any:
    """Return JSON-safe multimodal content without embedded image bytes."""

    if isinstance(content, str) or content is None:
        return content
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return str(content)
    safe: list[Any] = []
    for raw in content:
        if not isinstance(raw, Mapping):
            safe.append(str(raw))
            continue
        block = dict(raw)
        image_url = block.get("image_url")
        if isinstance(image_url, Mapping):
            image_payload = dict(image_url)
            url = str(image_payload.get("url") or "")
            if url.startswith("data:"):
                import hashlib

                media_type = url[5:].split(";", 1)[0]
                image_payload["url"] = None
                image_payload["media_type"] = media_type
                image_payload["data_url_sha256"] = hashlib.sha256(url.encode()).hexdigest()
                block["image_url"] = image_payload
        source = block.get("source")
        if isinstance(source, Mapping) and source.get("type") == "base64":
            import hashlib

            source_payload = dict(source)
            data = str(source_payload.pop("data", ""))
            source_payload["base64_sha256"] = hashlib.sha256(data.encode()).hexdigest()
            block["source"] = source_payload
        safe.append(block)
    return safe
