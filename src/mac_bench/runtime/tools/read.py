from __future__ import annotations

from typing import Any, Mapping

from .runtime import ToolContext, ToolErrorCode, ToolExecutionError, ToolResult, ToolSpec

DEFAULT_LINE_LIMIT = 500
MAX_LINE_LIMIT = 2_000
MAX_TEXT_CHARS = 100_000
MAX_TEXT_BYTES = 10 * 1024 * 1024


def read_text(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    path = str(arguments.get("path") or "").strip()
    offset = _positive_int(arguments.get("offset", 1), name="offset")
    limit = _positive_int(arguments.get("limit", DEFAULT_LINE_LIMIT), name="limit")
    if limit > MAX_LINE_LIMIT:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"limit cannot exceed {MAX_LINE_LIMIT}",
        )
    safe, raw = context.environment.read_bytes(path, max_bytes=MAX_TEXT_BYTES)
    if b"\x00" in raw[:8192]:
        raise ToolExecutionError(
            ToolErrorCode.UNSUPPORTED_FORMAT,
            f"file {path!r} appears to be binary",
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(
            ToolErrorCode.DECODE_ERROR,
            f"file {path!r} is not valid UTF-8 text",
            details={"byte_offset": exc.start},
        ) from exc
    lines = text.splitlines()
    start_index = min(offset - 1, len(lines))
    selected = lines[start_index : start_index + limit]
    rendered: list[str] = []
    used = 0
    char_truncated = False
    warnings: list[str] = []
    for line_number, line in enumerate(selected, start=start_index + 1):
        prefix = f"{line_number}| "
        rendered_line = prefix + line
        addition = len(rendered_line) + (1 if rendered else 0)
        if used + addition > MAX_TEXT_CHARS:
            if not rendered:
                available = max(0, MAX_TEXT_CHARS - len(prefix))
                rendered.append(prefix + line[:available])
                used = len(rendered[0])
                warnings.append(
                    f"Line {line_number} was truncated from {len(line)} to {available} characters."
                )
            char_truncated = True
            break
        rendered.append(rendered_line)
        used += addition
    end_line = start_index + len(rendered)
    has_more = end_line < len(lines)
    lowered = path.lower()
    if lowered.endswith((".csv", ".tsv")):
        warnings.append("This appears to be tabular data; use read_csv for structured access.")
    version = context.environment.version(raw)
    context.observe_version(safe.relative_path, version)
    data = {
            "path": safe.input_path,
            "resolved_path": safe.resolved_path,
            "content": "\n".join(rendered),
            "content_format": "line_numbered",
            "start_line": start_index + 1 if rendered else offset,
            "end_line": end_line,
            "total_lines": len(lines),
            "truncated": has_more or char_truncated,
            "next_offset": end_line + 1 if has_more else None,
            "warnings": warnings,
            "version": version,
        }
    footer = (
        f"\n\n[read {safe.input_path}: lines {data['start_line']}-{end_line} "
        f"of {len(lines)}"
    )
    if data["next_offset"] is not None:
        footer += f"; continue with offset={data['next_offset']}"
    footer += "]"
    return ToolResult.completed(
        data,
        model_text=(data["content"] or "[empty file]") + footer,
    )


def read_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="read",
        description="Read a UTF-8 text file from the workspace with line-numbered pagination.",
        properties={
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LINE_LIMIT},
        },
        required=("path",),
        handler=read_text,
    )


def _positive_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool):
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"{name} must be an integer",
        ) from exc
    if parsed < 1:
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, f"{name} must be positive")
    return parsed
