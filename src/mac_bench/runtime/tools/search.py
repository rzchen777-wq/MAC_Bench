from __future__ import annotations

import json
import shlex
from collections import defaultdict, deque
from typing import Any, Mapping

from .runtime import ToolContext, ToolErrorCode, ToolExecutionError, ToolResult, ToolSpec

DEFAULT_MATCH_LIMIT = 100
MAX_MATCH_LIMIT = 500
MAX_OFFSET = 10_000
MAX_CONTEXT_LINES = 10
MAX_OUTPUT_CHARS = 100_000


def search_workspace(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    query = str(arguments.get("query") or "")
    if not query:
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, "query must not be empty")
    path = str(arguments.get("path") or ".").strip()
    target = str(arguments.get("target") or "content").strip().lower()
    if target not in {"content", "files"}:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            "target must be 'content' or 'files'",
        )
    limit = _bounded_int(arguments.get("limit", DEFAULT_MATCH_LIMIT), "limit", 1, MAX_MATCH_LIMIT)
    offset = _bounded_int(arguments.get("offset", 0), "offset", 0, MAX_OFFSET)
    context_lines = _bounded_int(
        arguments.get("context_lines", 0),
        "context_lines",
        0,
        MAX_CONTEXT_LINES,
    )
    safe = context.environment.resolve_path(path, kind="any")
    search_path = shlex.quote(safe.relative_path)
    warnings: list[str] = []
    if target == "files":
        command = f"rg --files -- {search_path}"
        result = context.environment.exec_controlled(command, timeout=120)
        if _rg_unavailable(result.exit_code, result.stdout):
            result = context.environment.exec_controlled(
                f"find -- {search_path} -type f -print",
                timeout=120,
            )
            warnings.append("ripgrep is unavailable; used find fallback")
        if result.exit_code not in {0, 1}:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"file search failed: {result.stdout[-2000:]}",
            )
        import fnmatch

        paths = [line for line in result.stdout.splitlines() if fnmatch.fnmatch(line, query)]
        paths.sort()
        matches = [{"path": item} for item in paths]
    else:
        command_parts = ["rg", "--json", "--line-number", "--column"]
        if not bool(arguments.get("regex", False)):
            command_parts.append("--fixed-strings")
        if not bool(arguments.get("case_sensitive", False)):
            command_parts.append("--ignore-case")
        glob = arguments.get("glob")
        if glob:
            command_parts.extend(("--glob", str(glob)))
        if context_lines:
            command_parts.extend(("--context", str(context_lines)))
        command_parts.extend(("--", query, safe.relative_path))
        command = " ".join(shlex.quote(part) for part in command_parts)
        result = context.environment.exec_controlled(command, timeout=120)
        if _rg_unavailable(result.exit_code, result.stdout):
            result = context.environment.exec_controlled(
                _grep_fallback_command(
                    query=query,
                    path=safe.relative_path,
                    regex=bool(arguments.get("regex", False)),
                    case_sensitive=bool(arguments.get("case_sensitive", False)),
                    glob=str(glob) if glob else None,
                    max_lines=offset + limit + 1,
                ),
                timeout=120,
            )
            warnings.append(
                "ripgrep is unavailable; used grep fallback without context lines"
            )
        if result.exit_code not in {0, 1}:
            code = (
                ToolErrorCode.INVALID_ARGUMENTS
                if result.exit_code == 2
                else ToolErrorCode.EXECUTION_ERROR
            )
            raise ToolExecutionError(
                code,
                f"content search failed: {result.stdout[-2000:]}",
            )
        matches = (
            _parse_grep_lines(result.stdout)
            if warnings
            else _parse_rg_json(result.stdout, context_lines=context_lines)
        )
        matches.sort(key=lambda item: (str(item["path"]), int(item.get("line") or 0)))
    total = len(matches)
    selected = matches[offset : offset + limit]
    selected, output_truncated = _truncate_matches(selected)
    next_offset = offset + len(selected)
    has_more = next_offset < total
    data = {
            "query": query,
            "path": safe.input_path,
            "target": target,
            "matches": selected,
            "total_matches": total,
            "truncated": has_more or output_truncated,
            "next_offset": next_offset if has_more else None,
            "error": None,
            "warnings": warnings,
        }
    lines = [
        f"[search query={query!r} path={safe.input_path!r}: "
        f"{len(selected)} of {total} match(es)]"
    ]
    for item in selected:
        lines.extend(_render_match(item))
    if has_more:
        lines.append(f"[more matches available; continue with offset={next_offset}]")
    lines.extend(f"[warning: {warning}]" for warning in warnings)
    return ToolResult.completed(
        data,
        model_text="\n".join(lines),
    )


def _render_match(item: Mapping[str, Any]) -> list[str]:
    path = str(item.get("path") or "")
    line_number = int(item.get("line") or 0)
    before = [str(line) for line in item.get("before") or ()]
    after = [str(line) for line in item.get("after") or ()]
    rendered = [
        f"{path}-{line_number - len(before) + index}- {text}"
        for index, text in enumerate(before)
        if line_number
    ]
    location = path
    if line_number:
        location += f":{line_number}"
        if item.get("column"):
            location += f":{item['column']}"
    text = str(item.get("text") or "")
    rendered.append(f"{location}: {text}" if text else location)
    if line_number:
        rendered.extend(
            f"{path}-{line_number + index}- {text}"
            for index, text in enumerate(after, start=1)
        )
    return rendered


def _rg_unavailable(exit_code: int, output: str) -> bool:
    lowered = output.lower()
    return exit_code == 127 or "rg: command not found" in lowered or "rg: not found" in lowered


def _grep_fallback_command(
    *,
    query: str,
    path: str,
    regex: bool,
    case_sensitive: bool,
    glob: str | None,
    max_lines: int,
) -> str:
    parts = ["grep", "-R", "-n", "-H"]
    if not regex:
        parts.append("-F")
    if not case_sensitive:
        parts.append("-i")
    if glob:
        parts.append(f"--include={glob}")
    parts.extend(("--", query, path))
    command = " ".join(shlex.quote(part) for part in parts)
    return f"{command} | head -n {max(1, int(max_lines))}"


def _parse_grep_lines(output: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        path, separator, rest = raw_line.partition(":")
        line_text, separator2, text = rest.partition(":")
        if not separator or not separator2 or not line_text.isdigit():
            continue
        matches.append(
            {
                "path": path,
                "line": int(line_text),
                "column": 1,
                "text": text,
            }
        )
    return matches


def _parse_rg_json(output: str, *, context_lines: int) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    prior: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=context_lines))
    pending: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw_line in output.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        path = str((data.get("path") or {}).get("text") or "")
        line_text = str((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
        if kind == "context":
            for item in list(pending[path]):
                after = item.setdefault("after", [])
                if len(after) < context_lines:
                    after.append(line_text)
                if len(after) >= context_lines:
                    pending[path].remove(item)
            prior[path].append(line_text)
            continue
        if kind != "match":
            continue
        submatches = data.get("submatches") or []
        first = submatches[0] if submatches else {}
        item: dict[str, Any] = {
            "path": path,
            "line": int(data.get("line_number") or 0),
            "column": int(first.get("start") or 0) + 1,
            "text": line_text,
        }
        if context_lines:
            item["before"] = list(prior[path])
            item["after"] = []
            pending[path].append(item)
        matches.append(item)
        prior[path].clear()
    return matches


def _truncate_matches(matches: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    kept: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for item in matches:
        size = len(json.dumps(item, ensure_ascii=False))
        candidate = item
        if not kept and size > MAX_OUTPUT_CHARS:
            candidate = dict(item)
            candidate["text"] = str(candidate.get("text") or "")[:50_000]
            candidate["before"] = []
            candidate["after"] = []
            candidate["match_truncated"] = True
            size = len(json.dumps(candidate, ensure_ascii=False))
            truncated = True
        if kept and used + size > MAX_OUTPUT_CHARS:
            return kept, True
        kept.append(candidate)
        used += size
    return kept, truncated


def search_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="search",
        description="Search workspace file names or UTF-8 file contents with bounded results.",
        properties={
            "query": {"type": "string"},
            "path": {"type": "string"},
            "target": {"type": "string", "enum": ["content", "files"]},
            "regex": {"type": "boolean"},
            "case_sensitive": {"type": "boolean"},
            "glob": {"type": "string"},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": MAX_CONTEXT_LINES},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_MATCH_LIMIT},
            "offset": {"type": "integer", "minimum": 0, "maximum": MAX_OFFSET},
        },
        required=("query",),
        handler=search_workspace,
    )


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"{name} must be between {minimum} and {maximum}",
        )
    return parsed
