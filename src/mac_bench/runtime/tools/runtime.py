from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Sequence

from jsonschema import Draft202012Validator

from .protocol import function_tool_schema, parse_function_call

if TYPE_CHECKING:
    from .environment import ExecutionEnvironment


class ToolErrorCode(StrEnum):
    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    FILE_NOT_FOUND = "file_not_found"
    NOT_A_REGULAR_FILE = "not_a_regular_file"
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_FORMAT = "unsupported_format"
    DECODE_ERROR = "decode_error"
    OUTPUT_LIMIT = "output_limit"
    INVALID_PATCH = "invalid_patch"
    VERSION_CONFLICT = "version_conflict"
    SESSION_NOT_FOUND = "session_not_found"
    PROCESS_NOT_RUNNING = "process_not_running"
    EXECUTION_ERROR = "execution_error"


class ToolExecutionError(Exception):
    def __init__(
        self,
        code: ToolErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class TextContent:
    text: str

    def openai_block(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}

    def anthropic_block(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}

    def trace_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass(frozen=True)
class ImageContent:
    data: bytes = field(repr=False)
    media_type: str
    width: int
    height: int
    detail: str = "high"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"

    def openai_block(self) -> dict[str, Any]:
        return {
            "type": "image_url",
            "image_url": {"url": self.data_url(), "detail": self.detail},
        }

    def openai_responses_block(self) -> dict[str, Any]:
        return {
            "type": "input_image",
            "image_url": self.data_url(),
            "detail": self.detail,
        }

    def anthropic_block(self) -> dict[str, Any]:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": base64.b64encode(self.data).decode("ascii"),
            },
        }

    def trace_dict(self) -> dict[str, Any]:
        return {
            "type": "image",
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "detail": self.detail,
            "byte_count": len(self.data),
            "sha256": self.sha256,
        }


ContentBlock = TextContent | ImageContent


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]

    @classmethod
    def from_openai(cls, payload: Mapping[str, Any]) -> "ToolCall":
        parsed = parse_function_call(payload)
        return cls(
            call_id=str(payload.get("id") or ""),
            name=parsed.name,
            arguments=dict(parsed.arguments),
        )


@dataclass(frozen=True)
class ToolResult:
    status: Literal["completed", "failed"]
    content: tuple[ContentBlock, ...]
    data: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None

    @classmethod
    def completed(
        cls,
        data: Mapping[str, Any],
        *,
        images: Sequence[ImageContent] = (),
        model_text: str | None = None,
    ) -> "ToolResult":
        text = (
            model_text
            if model_text is not None
            else json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
        )
        return cls(
            status="completed",
            content=(TextContent(text), *tuple(images)),
            data=dict(data),
        )

    @classmethod
    def failed(cls, error: ToolExecutionError) -> "ToolResult":
        payload = {
            "status": "failed",
            "error": {
                "code": error.code.value,
                "message": error.message,
                "details": error.details,
            },
        }
        return cls(
            status="failed",
            content=(TextContent(json.dumps(payload, ensure_ascii=False)),),
            error=payload["error"],
        )

    def openai_content(self) -> str | list[dict[str, Any]]:
        if len(self.content) == 1 and isinstance(self.content[0], TextContent):
            return self.content[0].text
        return [block.openai_block() for block in self.content]

    def anthropic_content(self) -> list[dict[str, Any]]:
        return [block.anthropic_block() for block in self.content]

    def openai_responses_content(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for block in self.content:
            if isinstance(block, ImageContent):
                blocks.append(block.openai_responses_block())
            else:
                blocks.append(block.openai_block())
        return blocks

    def trace_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "data": dict(self.data),
            "content": [block.trace_dict() for block in self.content],
        }
        if self.error is not None:
            payload["error"] = dict(self.error)
        return payload


@dataclass(frozen=True)
class ToolContext:
    environment: ExecutionEnvironment | None
    agent_id: str
    sample_id: str
    permissions: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    observed_versions: dict[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @property
    def owner_scope(self) -> str:
        return f"{self.sample_id}:{self.agent_id}"

    def observe_version(self, path: str, version: str) -> None:
        self.observed_versions[path] = version


ToolHandler = Callable[[ToolContext, Mapping[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: Mapping[str, Any]
    required: tuple[str, ...]
    handler: ToolHandler = field(repr=False, compare=False)
    capability: str | None = None
    additional_properties: bool = False

    def schema(self) -> dict[str, Any]:
        return function_tool_schema(
            name=self.name,
            description=self.description,
            properties=self.properties,
            required=self.required,
            additional_properties=self.additional_properties,
        )

    def arguments_schema(self) -> dict[str, Any]:
        return dict(self.schema()["function"]["parameters"])


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool name {spec.name!r}")
        Draft202012Validator.check_schema(spec.arguments_schema())
        self._specs[spec.name] = spec

    def canonical_name(self, name: str) -> str:
        return name.strip().lower()

    def get(self, name: str) -> ToolSpec:
        canonical = self.canonical_name(name)
        try:
            return self._specs[canonical]
        except KeyError as exc:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"unknown tool {name!r}",
            ) from exc

    def schemas(self, context: ToolContext) -> list[dict[str, Any]]:
        return [
            spec.schema()
            for spec in self._specs.values()
            if self._available(spec, context)
        ]

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        try:
            spec = self.get(name)
            normalized_arguments = dict(arguments)
            if spec.name not in context.permissions:
                raise ToolExecutionError(
                    ToolErrorCode.PERMISSION_DENIED,
                    f"{context.agent_id} cannot use tool {spec.name!r}",
                )
            if spec.capability and spec.capability not in context.capabilities:
                raise ToolExecutionError(
                    ToolErrorCode.UNSUPPORTED_CAPABILITY,
                    f"tool {spec.name!r} requires capability {spec.capability!r}",
                )
            errors = sorted(
                Draft202012Validator(spec.arguments_schema()).iter_errors(normalized_arguments),
                key=lambda error: tuple(str(part) for part in error.absolute_path),
            )
            if errors:
                first = errors[0]
                location = ".".join(str(part) for part in first.absolute_path)
                prefix = f"arguments.{location}: " if location else "arguments: "
                raise ToolExecutionError(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    prefix + first.message,
                )
            return spec.handler(context, normalized_arguments)
        except ToolExecutionError as exc:
            return ToolResult.failed(exc)
        except Exception as exc:
            return ToolResult.failed(
                ToolExecutionError(
                    ToolErrorCode.EXECUTION_ERROR,
                    f"{type(exc).__name__}: {exc}",
                )
            )

    def execute_call(self, call: ToolCall, context: ToolContext) -> ToolResult:
        return self.execute(call.name, call.arguments, context)

    @staticmethod
    def _available(spec: ToolSpec, context: ToolContext) -> bool:
        if spec.name not in context.permissions:
            return False
        return spec.capability is None or spec.capability in context.capabilities
