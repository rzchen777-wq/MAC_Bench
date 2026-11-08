from __future__ import annotations

import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from mac_bench.runtime.sandbox.base import BackendProcessHandle, BackendProcessSnapshot

from .protocol import function_tool_schema
from .runtime import ToolContext, ToolErrorCode, ToolExecutionError, ToolResult, ToolSpec

DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_YIELD_MS = 10_000
MAX_TIMEOUT_MS = 3_600_000
MAX_YIELD_MS = 30_000
MAX_WAIT_MS = 300_000
MAX_COMMAND_CHARS = 100_000
MAX_CALL_OUTPUT_CHARS = 100_000
MAX_LOG_CHARS = 10_000_000
DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 2_000
DEFAULT_SESSION_RETENTION_SECONDS = 3_600
DEFAULT_MAX_SESSIONS_PER_SCOPE = 64

ProcessStatus = Literal["running", "completed", "failed", "killed"]


@dataclass
class ProcessSession:
    session_id: str
    owner_scope: str
    environment_key: tuple[int, str, str]
    context: ToolContext = field(repr=False)
    backend_handle: BackendProcessHandle = field(repr=False)
    command: str
    cwd: str
    tty: bool
    timeout_ms: int
    started_at: float
    log_path: Path
    status: ProcessStatus = "running"
    exit_code: int | None = None
    signal: str | None = None
    stdin_open: bool = True
    ended_at: float | None = None
    poll_cursor: int = 0
    captured_chars: int = 0
    backend_output_chars: int = 0
    log_truncated: bool = False
    timed_out: bool = False
    expires_at: float | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def runtime_ms(self) -> int:
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return max(0, int((end - self.started_at) * 1_000))


class ProcessManager:
    """Owns scoped process sessions while backends own actual processes."""

    def __init__(
        self,
        *,
        retention_seconds: float = DEFAULT_SESSION_RETENTION_SECONDS,
        max_sessions_per_scope: int = DEFAULT_MAX_SESSIONS_PER_SCOPE,
    ) -> None:
        if retention_seconds < 0 or max_sessions_per_scope < 1:
            raise ValueError("process retention must be non-negative and capacity positive")
        self._sessions: dict[str, ProcessSession] = {}
        self._lock = threading.RLock()
        self._logs = tempfile.TemporaryDirectory(prefix="mac-bench-process-logs-")
        self.retention_seconds = float(retention_seconds)
        self.max_sessions_per_scope = int(max_sessions_per_scope)

    def start(
        self,
        context: ToolContext,
        *,
        command: str,
        cwd: str,
        timeout_ms: int,
        tty: bool,
    ) -> ProcessSession:
        self._reap_expired()
        key = _environment_key(context)
        with self._lock:
            scope_count = sum(
                item.owner_scope == context.owner_scope and item.environment_key == key
                for item in self._sessions.values()
            )
        if scope_count >= self.max_sessions_per_scope:
            raise ToolExecutionError(
                ToolErrorCode.OUTPUT_LIMIT,
                f"process session capacity {self.max_sessions_per_scope} "
                "was reached for this scope",
            )
        session_id = uuid.uuid4().hex
        try:
            handle = context.environment.backend.start_process(
                context.environment.workspace.sandbox,
                session_id=session_id,
                command=command,
                cwd=cwd,
                tty=tty,
            )
        except Exception as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"failed to start command: {type(exc).__name__}: {exc}",
            ) from exc
        log_path = Path(self._logs.name) / f"{session_id}.log"
        log_path.touch()
        session = ProcessSession(
            session_id=session_id,
            owner_scope=context.owner_scope,
            environment_key=key,
            context=context,
            backend_handle=handle,
            command=command,
            cwd=cwd,
            tty=tty,
            timeout_ms=timeout_ms,
            started_at=time.monotonic(),
            log_path=log_path,
        )
        with self._lock:
            self._sessions[session_id] = session
        threading.Thread(
            target=self._watch_timeout,
            args=(session,),
            name=f"process-timeout-{session_id}",
            daemon=True,
        ).start()
        return session

    def refresh(self, session: ProcessSession, *, wait_ms: int = 0) -> ProcessSession:
        with session.lock:
            if session.status != "running":
                return session
        try:
            snapshot = session.context.environment.backend.poll_process(
                session.context.environment.workspace.sandbox,
                session.backend_handle,
                wait_ms=wait_ms,
            )
        except Exception as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"failed to poll session {session.session_id}: {type(exc).__name__}: {exc}",
            ) from exc
        self._merge_snapshot(session, snapshot)
        return session

    def get(self, context: ToolContext, session_id: str) -> ProcessSession:
        self._reap_expired()
        with self._lock:
            session = self._sessions.get(session_id)
        if (
            session is None
            or session.owner_scope != context.owner_scope
            or session.environment_key != _environment_key(context)
        ):
            raise ToolExecutionError(
                ToolErrorCode.SESSION_NOT_FOUND,
                f"process session {session_id!r} was not found in this scope",
            )
        return session

    def list(self, context: ToolContext) -> list[ProcessSession]:
        self._reap_expired()
        key = _environment_key(context)
        with self._lock:
            sessions = [
                item
                for item in self._sessions.values()
                if item.owner_scope == context.owner_scope and item.environment_key == key
            ]
        for session in sessions:
            self.refresh(session)
        return sorted(sessions, key=lambda item: item.started_at)

    def write(self, session: ProcessSession, *, data: str, eof: bool) -> None:
        self.refresh(session)
        with session.lock:
            if session.status != "running" or not session.stdin_open:
                raise ToolExecutionError(
                    ToolErrorCode.PROCESS_NOT_RUNNING,
                    f"process session {session.session_id!r} is not accepting input",
                )
        try:
            session.context.environment.backend.write_process(
                session.context.environment.workspace.sandbox,
                session.backend_handle,
                data=data,
                eof=eof,
            )
        except Exception as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"failed to write process input: {type(exc).__name__}: {exc}",
            ) from exc
        if eof:
            with session.lock:
                session.stdin_open = False

    def kill(self, session: ProcessSession) -> None:
        self.refresh(session)
        with session.lock:
            if session.status != "running":
                return
        try:
            session.context.environment.backend.kill_process(
                session.context.environment.workspace.sandbox,
                session.backend_handle,
            )
        except Exception as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"failed to kill process tree: {type(exc).__name__}: {exc}",
            ) from exc
        try:
            snapshot = session.context.environment.backend.poll_process(
                session.context.environment.workspace.sandbox,
                session.backend_handle,
            )
            self._merge_snapshot(session, snapshot)
        except Exception:
            with session.lock:
                session.status = "killed"
                session.signal = "SIGTERM"
                session.stdin_open = False
                session.ended_at = time.monotonic()
                session.expires_at = session.ended_at + self.retention_seconds

    def poll_output(self, session: ProcessSession) -> tuple[str, bool]:
        with session.lock:
            text = session.log_path.read_text(encoding="utf-8")
            output = text[session.poll_cursor :]
            session.poll_cursor = len(text)
            truncated = len(output) > MAX_CALL_OUTPUT_CHARS or session.log_truncated
            return output[:MAX_CALL_OUTPUT_CHARS], truncated

    @staticmethod
    def log_window(session: ProcessSession, *, offset: int, limit: int) -> dict[str, Any]:
        with session.lock:
            lines = session.log_path.read_text(encoding="utf-8").splitlines(keepends=True)
            selected = lines[offset : offset + limit]
            next_offset = offset + len(selected)
            has_more = next_offset < len(lines)
            output = "".join(selected)
            char_truncated = len(output) > MAX_CALL_OUTPUT_CHARS
            return {
                "output": output[:MAX_CALL_OUTPUT_CHARS],
                "offset": offset,
                "next_offset": next_offset if has_more else None,
                "total_lines": len(lines),
                "truncated": has_more or char_truncated or session.log_truncated,
            }

    def _merge_snapshot(self, session: ProcessSession, snapshot: BackendProcessSnapshot) -> None:
        with session.lock:
            if len(snapshot.output) >= session.backend_output_chars:
                delta = snapshot.output[session.backend_output_chars :]
            else:
                delta = snapshot.output
            session.backend_output_chars = len(snapshot.output)
            remaining = MAX_LOG_CHARS - session.captured_chars
            if delta and remaining > 0:
                accepted = delta[:remaining]
                with session.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(accepted)
                session.captured_chars += len(accepted)
                if len(accepted) < len(delta):
                    session.log_truncated = True
            elif delta:
                session.log_truncated = True
            session.stdin_open = snapshot.stdin_open
            session.log_truncated = session.log_truncated or snapshot.output_truncated
            if not session.timed_out:
                session.status = snapshot.status
                session.exit_code = snapshot.exit_code
                session.signal = snapshot.signal
            if session.status != "running" and session.ended_at is None:
                session.ended_at = time.monotonic()
                session.expires_at = session.ended_at + self.retention_seconds

    def _watch_timeout(self, session: ProcessSession) -> None:
        remaining = session.timeout_ms / 1_000
        while remaining > 0:
            interval = min(remaining, 0.25)
            time.sleep(interval)
            remaining -= interval
            with session.lock:
                if session.status != "running":
                    return
        try:
            self.refresh(session)
        except ToolExecutionError:
            pass
        with session.lock:
            if session.status != "running":
                return
            session.timed_out = True
            session.status = "failed"
            session.signal = "TIMEOUT"
            session.stdin_open = False
            session.ended_at = time.monotonic()
            session.expires_at = session.ended_at + self.retention_seconds
        try:
            session.context.environment.backend.kill_process(
                session.context.environment.workspace.sandbox,
                session.backend_handle,
            )
        except Exception:
            pass

    def _reap_expired(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if session.expires_at is not None and session.expires_at <= now
            ]
            sessions = [self._sessions.pop(session_id) for session_id in expired]
        for session in sessions:
            session.log_path.unlink(missing_ok=True)


def exec_process(
    manager: ProcessManager,
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> ToolResult:
    command = str(arguments.get("command") or "")
    if not command.strip():
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, "command must not be empty")
    if len(command) > MAX_COMMAND_CHARS:
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, "command is too long")
    timeout_ms = _bounded_int(
        arguments.get("timeout_ms", DEFAULT_TIMEOUT_MS),
        "timeout_ms",
        1,
        MAX_TIMEOUT_MS,
    )
    yield_ms = _bounded_int(
        arguments.get("yield_time_ms", DEFAULT_YIELD_MS),
        "yield_time_ms",
        0,
        MAX_YIELD_MS,
    )
    tty = bool(arguments.get("tty", False))
    workdir = str(arguments.get("workdir") or ".")
    safe_cwd = context.environment.resolve_path(workdir, kind="directory")
    session = manager.start(
        context,
        command=command,
        cwd=safe_cwd.resolved_path,
        timeout_ms=timeout_ms,
        tty=tty,
    )
    manager.refresh(session, wait_ms=yield_ms)
    output, truncated = manager.poll_output(session)
    data = _session_data(session)
    data.update({"output": output, "truncated": truncated})
    if session.status != "running" and not truncated:
        data["session_id"] = None
    return ToolResult.completed(data, model_text=_process_model_text(data))


def process_action(
    manager: ProcessManager,
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> ToolResult:
    action = str(arguments.get("action") or "")
    if action == "list":
        _reject_irrelevant(arguments, {"action"}, action)
        data = {"sessions": [_session_summary(item) for item in manager.list(context)]}
        lines = ["[process sessions]"]
        lines.extend(
            f"{item['session_id']} {item['status']} {item['command']}"
            for item in data["sessions"]
        )
        return ToolResult.completed(data, model_text="\n".join(lines))
    session_id = str(arguments.get("session_id") or "")
    if not session_id:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"process action {action!r} requires session_id",
        )
    session = manager.get(context, session_id)
    if action == "poll":
        _reject_irrelevant(arguments, {"action", "session_id", "wait_ms"}, action)
        wait_ms = _bounded_int(arguments.get("wait_ms", 0), "wait_ms", 0, MAX_WAIT_MS)
        manager.refresh(session, wait_ms=wait_ms)
        output, truncated = manager.poll_output(session)
        data = _session_data(session)
        data.update({"output": output, "truncated": truncated})
        return ToolResult.completed(data, model_text=_process_model_text(data))
    if action == "log":
        _reject_irrelevant(arguments, {"action", "session_id", "offset", "limit"}, action)
        offset = _bounded_int(arguments.get("offset", 0), "offset", 0, 10_000_000)
        limit = _bounded_int(arguments.get("limit", DEFAULT_LOG_LINES), "limit", 1, MAX_LOG_LINES)
        manager.refresh(session)
        data = _session_data(session)
        data.update(manager.log_window(session, offset=offset, limit=limit))
        return ToolResult.completed(data, model_text=_process_model_text(data))
    if action == "write":
        _reject_irrelevant(arguments, {"action", "session_id", "data", "eof"}, action)
        data_arg = str(arguments.get("data") or "")
        eof = bool(arguments.get("eof", False))
        if not data_arg and not eof:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_ARGUMENTS,
                "write requires non-empty data or eof=true",
            )
        manager.write(session, data=data_arg, eof=eof)
        manager.refresh(session, wait_ms=250)
        data = _session_data(session)
        return ToolResult.completed(data, model_text=_process_model_text(data))
    if action == "kill":
        _reject_irrelevant(arguments, {"action", "session_id"}, action)
        manager.kill(session)
        data = _session_data(session)
        return ToolResult.completed(data, model_text=_process_model_text(data))
    raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, f"unknown process action {action!r}")


def exec_tool_spec(manager: ProcessManager) -> ToolSpec:
    return ToolSpec(
        name="exec",
        description="Start a command in the workspace and return or yield a managed process.",
        properties=_exec_properties(),
        required=("command",),
        handler=lambda context, arguments: exec_process(manager, context, arguments),
    )


def process_tool_spec(manager: ProcessManager) -> ToolSpec:
    return ToolSpec(
        name="process",
        description="List, poll, inspect, write to, or kill a managed process session.",
        properties=_process_properties(),
        required=("action",),
        handler=lambda context, arguments: process_action(manager, context, arguments),
    )


def exec_tool_schema() -> dict[str, Any]:
    return function_tool_schema(
        name="exec",
        description="Start a command in the workspace and return or yield a managed process.",
        properties=_exec_properties(),
        required=("command",),
    )


def process_tool_schema() -> dict[str, Any]:
    return function_tool_schema(
        name="process",
        description="List, poll, inspect, write to, or kill a managed process session.",
        properties=_process_properties(),
        required=("action",),
    )


def _exec_properties() -> dict[str, Any]:
    return {
        "command": {"type": "string", "maxLength": MAX_COMMAND_CHARS},
        "workdir": {"type": "string"},
        "timeout_ms": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_MS},
        "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": MAX_YIELD_MS},
        "tty": {"type": "boolean"},
    }


def _process_properties() -> dict[str, Any]:
    return {
        "action": {"type": "string", "enum": ["list", "poll", "log", "write", "kill"]},
        "session_id": {"type": "string"},
        "wait_ms": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_MS},
        "data": {"type": "string"},
        "eof": {"type": "boolean"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LOG_LINES},
    }


def _session_data(session: ProcessSession) -> dict[str, Any]:
    return {
        "status": session.status,
        "session_id": session.session_id,
        "command": session.command,
        "exit_code": session.exit_code,
        "signal": session.signal,
        "wall_time_ms": session.runtime_ms,
        "stdin_open": session.stdin_open,
    }


def _session_summary(session: ProcessSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "status": session.status,
        "command": session.command,
        "cwd": session.cwd,
        "runtime_ms": session.runtime_ms,
        "tty": session.tty,
    }


def _process_model_text(data: Mapping[str, Any]) -> str:
    status = str(data.get("status") or "unknown")
    parts = [f"status={status}"]
    if data.get("exit_code") is not None:
        parts.append(f"exit_code={data['exit_code']}")
    if data.get("signal"):
        parts.append(f"signal={data['signal']}")
    if data.get("wall_time_ms") is not None:
        parts.append(f"wall_time_ms={data['wall_time_ms']}")
    session_id = data.get("session_id")
    if status == "running" and session_id:
        parts.append(f"session_id={session_id}")
    output = str(data.get("output") or "")
    text = "[process " + " ".join(parts) + "]"
    if output:
        text += "\n" + output
    if data.get("truncated"):
        text += "\n[output truncated; use process(action='log', ...) for a bounded window]"
    elif status == "running" and session_id:
        text += (
            "\n[process still running; continue with "
            f"process(action='poll', session_id='{session_id}', wait_ms=...)]"
        )
    return text


def _environment_key(context: ToolContext) -> tuple[int, str, str]:
    workspace = context.environment.workspace
    return id(context.environment.backend), workspace.sandbox.id, workspace.cwd


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ToolExecutionError(ToolErrorCode.INVALID_ARGUMENTS, f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"{name} must be an integer",
        ) from exc
    if parsed < minimum or parsed > maximum:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"{name} must be between {minimum} and {maximum}",
        )
    return parsed


def _reject_irrelevant(
    arguments: Mapping[str, Any], allowed: set[str], action: str
) -> None:
    unexpected = sorted(key for key in arguments if key not in allowed)
    if unexpected:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"process action {action!r} does not accept {unexpected!r}",
        )
