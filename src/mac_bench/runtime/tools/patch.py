from __future__ import annotations

import difflib
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .environment import SafePath
from .runtime import ToolContext, ToolErrorCode, ToolExecutionError, ToolResult, ToolSpec

MAX_PATCH_CHARS = 1_000_000
MAX_PATCH_FILES = 100
MAX_DIFF_CHARS = 200_000


@dataclass(frozen=True)
class PatchHunk:
    anchor: str | None
    lines: tuple[str, ...]
    end_of_file: bool = False


@dataclass(frozen=True)
class PatchOperation:
    kind: Literal["add", "update", "delete"]
    path: str
    content: str | None = None
    move_to: str | None = None
    hunks: tuple[PatchHunk, ...] = ()


@dataclass(frozen=True)
class _Change:
    safe: SafePath
    before: bytes | None
    after: bytes | None
    operation: Literal["added", "updated", "deleted"]


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[tuple[int, str, str], threading.RLock] = {}


def parse_patch(patch: str) -> tuple[PatchOperation, ...]:
    if not patch or len(patch) > MAX_PATCH_CHARS:
        _invalid("patch must be non-empty and at most 1,000,000 characters")
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        _invalid("patch must start with '*** Begin Patch' and end with '*** End Patch'")
    operations: list[PatchOperation] = []
    index = 1
    while index < len(lines) - 1:
        marker = lines[index]
        if marker.startswith("*** Add File: "):
            path = marker.removeprefix("*** Add File: ").strip()
            index += 1
            content: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                line = lines[index]
                if not line.startswith("+"):
                    _invalid("every Add File content line must start with '+'", path=path)
                content.append(line[1:])
                index += 1
            operations.append(
                PatchOperation("add", path, content="\n".join(content) + ("\n" if content else ""))
            )
        elif marker.startswith("*** Delete File: "):
            path = marker.removeprefix("*** Delete File: ").strip()
            operations.append(PatchOperation("delete", path))
            index += 1
        elif marker.startswith("*** Update File: "):
            path = marker.removeprefix("*** Update File: ").strip()
            index += 1
            move_to: str | None = None
            if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
                move_to = lines[index].removeprefix("*** Move to: ").strip()
                index += 1
            hunks: list[PatchHunk] = []
            while index < len(lines) - 1 and not _is_file_marker(lines[index]):
                header = lines[index]
                if not header.startswith("@@"):
                    _invalid("Update File content must be grouped under '@@' hunks", path=path)
                anchor = header[2:].strip() or None
                index += 1
                body: list[str] = []
                at_eof = False
                while index < len(lines) - 1:
                    line = lines[index]
                    if line == "*** End of File":
                        at_eof = True
                        index += 1
                        break
                    if line.startswith("@@") or _is_file_marker(line):
                        break
                    if not line or line[0] not in {" ", "+", "-"}:
                        _invalid("hunk lines must start with space, '+', or '-'", path=path)
                    body.append(line)
                    index += 1
                if not body:
                    _invalid("update hunk must not be empty", path=path)
                hunks.append(PatchHunk(anchor, tuple(body), at_eof))
            if not hunks and move_to is None:
                _invalid("Update File requires at least one hunk or a move target", path=path)
            operations.append(PatchOperation("update", path, move_to=move_to, hunks=tuple(hunks)))
        else:
            _invalid(f"unknown patch marker {marker!r}")
        if len(operations) > MAX_PATCH_FILES:
            _invalid(f"patch cannot modify more than {MAX_PATCH_FILES} files")
    if not operations:
        _invalid("patch contains no file operations")
    return tuple(operations)


def apply_patch(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    patch = str(arguments.get("patch") or "")
    operations = parse_patch(patch)
    environment = context.environment
    safe_paths: dict[str, SafePath] = {}
    for operation in operations:
        safe_paths[operation.path] = environment.resolve_write_path(operation.path)
        if operation.move_to:
            safe_paths[operation.move_to] = environment.resolve_write_path(operation.move_to)
    if len(safe_paths) != sum(1 + bool(item.move_to) for item in operations):
        _invalid("a patch cannot operate on the same path more than once")

    lock_keys = sorted(
        (
            id(environment.backend),
            environment.workspace.sandbox.id,
            safe.resolved_path,
        )
        for safe in safe_paths.values()
    )
    with ExitStack() as stack:
        for key in lock_keys:
            stack.enter_context(_path_lock(key))
        changes = _preflight(context, operations, safe_paths)
        _commit_with_rollback(context, changes)

    files: list[dict[str, Any]] = []
    diff_parts: list[str] = []
    for change in changes:
        before_version = environment.version(change.before) if change.before is not None else None
        after_version = environment.version(change.after) if change.after is not None else None
        if after_version is not None:
            context.observe_version(change.safe.relative_path, after_version)
        else:
            context.observed_versions.pop(change.safe.relative_path, None)
        files.append(
            {
                "path": change.safe.relative_path,
                "operation": change.operation,
                "before_version": before_version,
                "after_version": after_version,
            }
        )
        diff_parts.extend(_render_diff(change))
    rendered_diff = "".join(diff_parts)
    truncated = len(rendered_diff) > MAX_DIFF_CHARS
    data = {
            "files": files,
            "diff": rendered_diff[:MAX_DIFF_CHARS],
            "truncated": truncated,
            "warnings": ["diff output was truncated"] if truncated else [],
        }
    summary = ", ".join(
        f"{item['operation']} {item['path']}" for item in files
    ) or "no files changed"
    return ToolResult.completed(
        data,
        model_text=f"Applied patch: {summary}.\n{data['diff']}",
    )


def apply_patch_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="apply_patch",
        description=(
            "Apply a Codex/V4A patch. The patch must begin with '*** Begin Patch', "
            "end with '*** End Patch', and contain '*** Add File:', "
            "'*** Update File:', or '*** Delete File:' sections. Do not send a bare "
            "unified diff. Paths are workspace-relative."
        ),
        properties={
            "patch": {
                "type": "string",
                "maxLength": MAX_PATCH_CHARS,
                "description": (
                    "Complete patch including *** Begin Patch and *** End Patch markers."
                ),
            }
        },
        required=("patch",),
        handler=apply_patch,
    )


def _preflight(
    context: ToolContext,
    operations: tuple[PatchOperation, ...],
    safe_paths: Mapping[str, SafePath],
) -> list[_Change]:
    changes: list[_Change] = []
    for operation in operations:
        source = safe_paths[operation.path]
        before = _read_optional(context, source)
        if operation.kind == "add":
            if before is not None:
                _invalid("Add File target already exists", path=operation.path)
            assert operation.content is not None
            changes.append(_Change(source, None, operation.content.encode("utf-8"), "added"))
            continue
        if before is None:
            raise ToolExecutionError(
                ToolErrorCode.FILE_NOT_FOUND,
                f"patch source {operation.path!r} does not exist",
                details={"path": operation.path},
            )
        observed = context.observed_versions.get(source.relative_path)
        current = context.environment.version(before)
        if observed is not None and observed != current:
            raise ToolExecutionError(
                ToolErrorCode.VERSION_CONFLICT,
                f"{operation.path!r} changed since it was read",
                details={"path": operation.path, "observed": observed, "current": current},
            )
        if operation.kind == "delete":
            changes.append(_Change(source, before, None, "deleted"))
            continue
        updated = _apply_hunks(operation.path, before, operation.hunks)
        if operation.move_to:
            destination = safe_paths[operation.move_to]
            if _read_optional(context, destination) is not None:
                _invalid("Move target already exists", path=operation.move_to)
            changes.append(_Change(source, before, None, "deleted"))
            changes.append(_Change(destination, None, updated, "added"))
        else:
            changes.append(_Change(source, before, updated, "updated"))
    return sorted(changes, key=lambda item: item.safe.resolved_path)


def _apply_hunks(path: str, before: bytes, hunks: tuple[PatchHunk, ...]) -> bytes:
    try:
        text = before.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(
            ToolErrorCode.UNSUPPORTED_FORMAT,
            f"cannot update non-UTF-8 file {path!r}",
            details={"byte_offset": exc.start},
        ) from exc
    had_final_newline = text.endswith("\n")
    lines = text.splitlines()
    cursor = 0
    for number, hunk in enumerate(hunks, start=1):
        if hunk.anchor is not None:
            anchors = [index for index in range(cursor, len(lines)) if lines[index] == hunk.anchor]
            if not anchors:
                _invalid("hunk anchor was not found", path=path, hunk=number)
            cursor = anchors[0] + 1
        old = [line[1:] for line in hunk.lines if line[0] in {" ", "-"}]
        new = [line[1:] for line in hunk.lines if line[0] in {" ", "+"}]
        if hunk.end_of_file:
            candidates = [len(lines) - len(old)] if lines[len(lines) - len(old) :] == old else []
        elif old:
            candidates = [
                index
                for index in range(cursor, len(lines) - len(old) + 1)
                if lines[index : index + len(old)] == old
            ]
        else:
            candidates = [cursor] if hunk.anchor is not None else []
        if not candidates:
            _invalid("expected hunk context was not found", path=path, hunk=number)
        position = candidates[0]
        lines[position : position + len(old)] = new
        cursor = position + len(new)
    rendered = "\n".join(lines)
    if had_final_newline and (lines or text):
        rendered += "\n"
    return rendered.encode("utf-8")


def _commit_with_rollback(context: ToolContext, changes: list[_Change]) -> None:
    committed: list[_Change] = []
    try:
        for change in changes:
            current = _read_optional(context, change.safe)
            if current != change.before:
                raise ToolExecutionError(
                    ToolErrorCode.VERSION_CONFLICT,
                    f"{change.safe.relative_path!r} changed after patch preflight",
                    details={"path": change.safe.relative_path},
                )
            if change.after is None:
                context.environment.delete_path(change.safe)
            else:
                context.environment.replace_bytes(change.safe, change.after)
            committed.append(change)
    except ToolExecutionError as exc:
        rollback_errors: list[str] = []
        for change in reversed(committed):
            try:
                if change.before is None:
                    context.environment.delete_path(change.safe)
                else:
                    context.environment.replace_bytes(change.safe, change.before)
            except ToolExecutionError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                "patch commit failed and rollback was incomplete",
                details={"cause": exc.message, "rollback_errors": rollback_errors},
            ) from exc
        raise


def _read_optional(context: ToolContext, safe: SafePath) -> bytes | None:
    try:
        resolved = context.environment.backend.resolve_path(
            context.environment.workspace.sandbox,
            path=safe.relative_path,
            cwd=context.environment.workspace.cwd,
            kind="file",
        )
    except FileNotFoundError:
        return None
    except (IsADirectoryError, NotADirectoryError) as exc:
        raise ToolExecutionError(
            ToolErrorCode.NOT_A_REGULAR_FILE,
            f"patch path {safe.relative_path!r} is a directory",
        ) from exc
    try:
        return context.environment.backend.read_file(
            context.environment.workspace.sandbox,
            path=resolved,
        )
    except Exception as exc:
        raise ToolExecutionError(
            ToolErrorCode.EXECUTION_ERROR,
            f"failed to read patch path {safe.relative_path!r}: {exc}",
        ) from exc


def _render_diff(change: _Change) -> list[str]:
    before = (change.before or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
    after = (change.after or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
    return list(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{change.safe.relative_path}",
            tofile=f"b/{change.safe.relative_path}",
        )
    )


def _path_lock(key: tuple[int, str, str]) -> threading.RLock:
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _is_file_marker(line: str) -> bool:
    return line.startswith(
        ("*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** End Patch")
    )


def _invalid(message: str, *, path: str | None = None, hunk: int | None = None) -> None:
    details: dict[str, Any] = {}
    if path is not None:
        details["path"] = path
    if hunk is not None:
        details["hunk"] = hunk
    raise ToolExecutionError(ToolErrorCode.INVALID_PATCH, message, details=details)
