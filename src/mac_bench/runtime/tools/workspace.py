from __future__ import annotations

import base64
import shlex
import threading
from dataclasses import dataclass, field
from typing import Protocol

from mac_bench.runtime.types import SolverWorkspace
from mac_bench.runtime.sandbox.base import (
    BackendProcessHandle,
    BackendProcessSnapshot,
    CommandResult,
    SandboxBackend,
    SandboxHandle,
)


class WorkspaceTools(Protocol):
    def exec_solver(
        self, workspace: SolverWorkspace, command: str, *, timeout: int = 120
    ) -> CommandResult: ...

    def collect_patch(self, workspace: SolverWorkspace) -> str: ...


def backend_for_workspace(tools: WorkspaceTools, workspace: SolverWorkspace) -> SandboxBackend:
    resolver = getattr(tools, "backend_for_workspace", None)
    if callable(resolver):
        return resolver(workspace)
    backend = getattr(tools, "backend", None)
    if isinstance(backend, SandboxBackend):
        return backend
    return SynchronousWorkspaceBackend(tools)


@dataclass
class _SynchronousProcess:
    done: threading.Event = field(default_factory=threading.Event)
    result: CommandResult | None = None
    killed: bool = False


class SynchronousWorkspaceBackend(SandboxBackend):
    """Compatibility adapter for legacy test runtimes without a backend handle."""

    def __init__(self, tools: WorkspaceTools) -> None:
        self.tools = tools

    def create(self, *, name, snapshot=None, image=None, network_blocked=False):
        del name, snapshot, image, network_blocked
        raise NotImplementedError

    def snapshot_exists(self, *, name: str) -> bool:
        del name
        return False

    def create_snapshot(self, sandbox, *, name: str, timeout: int = 600) -> None:
        del sandbox, name, timeout
        raise NotImplementedError

    def clone(self, sandbox, *, url: str, path: str, revision=None) -> None:
        del sandbox, url, path, revision
        raise NotImplementedError

    def exec(self, sandbox, command, *, cwd=None, env=None, timeout=None):
        del env
        workspace = SolverWorkspace(sandbox=sandbox, cwd=cwd or ".")
        return self.tools.exec_solver(workspace, command, timeout=timeout or 120)

    def write_file(self, sandbox, *, path: str, content: bytes) -> None:
        encoded = base64.b64encode(content).decode("ascii")
        parent = shlex.quote(str(path).rsplit("/", 1)[0] if "/" in str(path) else ".")
        command = (
            f"mkdir -p -- {parent}; "
            f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
        )
        result = self.exec(sandbox, command, timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(result.stdout)

    def read_file(self, sandbox, *, path: str) -> bytes:
        result = self.exec(sandbox, f"base64 -w0 -- {shlex.quote(path)}", timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(result.stdout)
        return base64.b64decode(result.stdout.strip())

    def resolve_path(self, sandbox, *, path: str, cwd=None, kind="any") -> str:
        if path == ".":
            return str(cwd or ".")
        option = "-m" if kind == "write" else "-e"
        result = self.exec(
            sandbox,
            f"realpath {option} -- {shlex.quote(path)}",
            cwd=cwd,
            timeout=30,
        )
        if result.exit_code != 0:
            raise FileNotFoundError(path)
        return result.stdout.strip()

    def delete_file(self, sandbox, *, path: str) -> None:
        result = self.exec(sandbox, f"rm -f -- {shlex.quote(path)}", timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(result.stdout)

    def set_network_blocked(self, sandbox, *, blocked: bool) -> None:
        del sandbox, blocked

    def delete(self, sandbox) -> None:
        del sandbox

    def start_process(self, sandbox, *, session_id, command, cwd, tty):
        del tty
        state = _SynchronousProcess()

        def run() -> None:
            try:
                state.result = self.exec(sandbox, command, cwd=cwd, timeout=None)
            finally:
                state.done.set()

        threading.Thread(target=run, name=f"sync-process-{session_id}", daemon=True).start()
        return BackendProcessHandle(session_id, state)

    def poll_process(self, sandbox, handle, *, wait_ms=0):
        del sandbox
        state = handle.raw
        assert isinstance(state, _SynchronousProcess)
        state.done.wait(wait_ms / 1_000)
        if not state.done.is_set():
            return BackendProcessSnapshot("running", "")
        if state.killed:
            return BackendProcessSnapshot("killed", "", signal="SIGTERM", stdin_open=False)
        assert state.result is not None
        status = "completed" if state.result.exit_code == 0 else "failed"
        return BackendProcessSnapshot(
            status,
            state.result.stdout,
            state.result.exit_code,
            stdin_open=False,
        )

    def write_process(self, sandbox, handle, *, data, eof):
        del sandbox, handle, data, eof
        raise BrokenPipeError("legacy synchronous runtime does not expose stdin")

    def kill_process(self, sandbox, handle, *, grace_ms=1_000):
        del sandbox, grace_ms
        state = handle.raw
        assert isinstance(state, _SynchronousProcess)
        state.killed = True
        state.done.set()
