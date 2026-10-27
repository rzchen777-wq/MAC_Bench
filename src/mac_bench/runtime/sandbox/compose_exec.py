"""Route solver tools into a compose app container from a docker-host sandbox.

SaaSBench (and similar compose work orders) run the hidden verifier on the VM
and the agent inside the app container. The host bind-mounts
``/work/sandbox/docker/workspace`` onto container ``/app``.
"""

from __future__ import annotations

import os
import shlex
from pathlib import PurePosixPath
from typing import Mapping

from mac_bench.runtime.types import SolverWorkspace
from mac_bench.runtime.sandbox.base import (
    BackendProcessHandle,
    BackendProcessSnapshot,
    CommandResult,
    SandboxBackend,
    SandboxHandle,
)

DEFAULT_APP_ROOT = "/app"
DEFAULT_HOST_WORKSPACE = "/work/sandbox/docker/workspace"


def map_app_path_to_host(
    path: str,
    *,
    cwd: str = DEFAULT_APP_ROOT,
    app_root: str = DEFAULT_APP_ROOT,
    host_workspace: str = DEFAULT_HOST_WORKSPACE,
) -> str:
    """Map a container workspace path onto the VM bind-mount."""

    raw = PurePosixPath(path)
    if not raw.is_absolute():
        raw = PurePosixPath(cwd) / raw
    resolved = PurePosixPath(os.path.normpath(str(raw)))
    root = PurePosixPath(app_root)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path {path!r} is outside {app_root}") from exc
    if str(relative) == ".":
        return host_workspace
    return str(PurePosixPath(host_workspace) / relative)


def resolve_via_realpath(
    backend: SandboxBackend,
    sandbox: SandboxHandle,
    *,
    path: str,
    cwd: str | None,
    kind: str,
) -> str:
    """Resolve a path inside the sandbox using ``realpath`` (Daytona-compatible)."""

    if kind == "write":
        result = backend.exec(
            sandbox,
            f"realpath -m -- {shlex.quote(path)}",
            cwd=cwd,
            timeout=30,
        )
        if result.exit_code != 0 or not result.stdout.strip():
            raise RuntimeError(f"failed to resolve writable path {path!r}: {result.stdout}")
        return result.stdout.strip().splitlines()[-1].strip()
    checks = {
        "any": "test -e \"$target\"",
        "file": "test -f \"$target\"",
        "directory": "test -d \"$target\"",
    }
    if kind not in checks:
        raise ValueError(f"unsupported path kind {kind!r}")
    quoted = shlex.quote(path)
    command = (
        f"target=$(realpath -e -- {quoted}) || exit 2; "
        f"{checks[kind]} || exit 3; printf '%s' \"$target\""
    )
    result = backend.exec(sandbox, command, cwd=cwd, timeout=30)
    if result.exit_code == 2:
        raise FileNotFoundError(path)
    if result.exit_code == 3:
        raise IsADirectoryError(path) if kind == "file" else NotADirectoryError(path)
    if result.exit_code != 0 or not result.stdout.strip():
        raise RuntimeError(f"failed to resolve sandbox path {path!r}: {result.stdout}")
    return result.stdout.strip()


class DockerComposeAppBackend(SandboxBackend):
    """Solver-facing backend: exec/process inside the app container."""

    def __init__(
        self,
        host: SandboxBackend,
        *,
        container: str,
        app_root: str = DEFAULT_APP_ROOT,
        host_workspace: str = DEFAULT_HOST_WORKSPACE,
    ) -> None:
        self.host = host
        self.container = container
        self.app_root = app_root
        self.host_workspace = host_workspace

    def create(self, *, name, snapshot=None, image=None, network_blocked=False):
        del name, snapshot, image, network_blocked
        raise RuntimeError("compose app backend wraps a live VM; create the host sandbox instead")

    def snapshot_exists(self, *, name: str) -> bool:
        del name
        return False

    def create_snapshot(self, sandbox, *, name: str, timeout: int = 600) -> None:
        del sandbox, name, timeout
        raise NotImplementedError

    def clone(self, sandbox, *, url: str, path: str, revision=None) -> None:
        del sandbox, url, path, revision
        raise NotImplementedError

    def _docker_exec_command(
        self,
        command: str,
        *,
        cwd: str | None,
        env: Mapping[str, str] | None = None,
        interactive: bool = False,
    ) -> str:
        inner_cwd = cwd or self.app_root
        env_args = "".join(
            f" -e {shlex.quote(f'{key}={value}')}"
            for key, value in dict(env or {}).items()
        )
        flags = "-i " if interactive else ""
        return (
            f"docker exec {flags}-w {shlex.quote(inner_cwd)}{env_args} "
            f"{shlex.quote(self.container)} bash -lc {shlex.quote(command)}"
        )

    def exec(
        self,
        sandbox: SandboxHandle,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandResult:
        wrapped = self._docker_exec_command(command, cwd=cwd, env=env)
        return self.host.exec(sandbox, wrapped, timeout=timeout)

    def write_file(self, sandbox: SandboxHandle, *, path: str, content: bytes) -> None:
        host_path = map_app_path_to_host(
            path,
            app_root=self.app_root,
            host_workspace=self.host_workspace,
        )
        parent = str(PurePosixPath(host_path).parent)
        if parent not in {"", "/"}:
            mkdir = self.host.exec(
                sandbox, f"mkdir -p -- {shlex.quote(parent)}", timeout=30
            )
            if mkdir.exit_code != 0:
                raise RuntimeError(f"mkdir failed for {host_path}: {mkdir.stdout}")
        self.host.write_file(sandbox, path=host_path, content=content)

    def read_file(self, sandbox: SandboxHandle, *, path: str) -> bytes:
        host_path = map_app_path_to_host(
            path,
            app_root=self.app_root,
            host_workspace=self.host_workspace,
        )
        return self.host.read_file(sandbox, path=host_path)

    def replace_file(self, sandbox: SandboxHandle, *, path: str, content: bytes) -> None:
        self.write_file(sandbox, path=path, content=content)

    def delete_file(self, sandbox: SandboxHandle, *, path: str) -> None:
        host_path = map_app_path_to_host(
            path,
            app_root=self.app_root,
            host_workspace=self.host_workspace,
        )
        result = self.host.exec(sandbox, f"rm -f -- {shlex.quote(host_path)}", timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(result.stdout)

    def resolve_path(
        self,
        sandbox: SandboxHandle,
        *,
        path: str,
        cwd: str | None = None,
        kind: str = "any",
    ) -> str:
        return resolve_via_realpath(self, sandbox, path=path, cwd=cwd, kind=kind)

    def start_process(
        self,
        sandbox: SandboxHandle,
        *,
        session_id: str,
        command: str,
        cwd: str,
        tty: bool,
    ) -> BackendProcessHandle:
        pidfile = f"/tmp/evoteam-process-{session_id}.pid"
        inner = (
            f"printf '%s\\n' $$ > {shlex.quote(pidfile)}; "
            f"exec bash -lc {shlex.quote(command)}"
        )
        wrapped = self._docker_exec_command(inner, cwd=cwd, interactive=True)
        handle = self.host.start_process(
            sandbox,
            session_id=session_id,
            command=wrapped,
            cwd="/work",
            tty=tty,
        )
        return BackendProcessHandle(session_id, _ComposeProcess(handle, pidfile))

    def poll_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        wait_ms: int = 0,
    ) -> BackendProcessSnapshot:
        state = handle.raw
        assert isinstance(state, _ComposeProcess)
        return self.host.poll_process(sandbox, state.host_handle, wait_ms=wait_ms)

    def write_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        data: str,
        eof: bool,
    ) -> None:
        state = handle.raw
        assert isinstance(state, _ComposeProcess)
        self.host.write_process(sandbox, state.host_handle, data=data, eof=eof)

    def kill_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        grace_ms: int = 1_000,
    ) -> None:
        state = handle.raw
        assert isinstance(state, _ComposeProcess)
        inner_kill = (
            f"pid=$(cat {shlex.quote(state.pidfile)} 2>/dev/null || true); "
            "if [ -n \"$pid\" ]; then kill \"$pid\" 2>/dev/null || true; "
            "sleep 0.2; kill -9 \"$pid\" 2>/dev/null || true; fi"
        )
        self.host.exec(
            sandbox,
            self._docker_exec_command(inner_kill, cwd=self.app_root),
            timeout=max(5, int(grace_ms / 1000) + 2),
        )
        self.host.kill_process(sandbox, state.host_handle, grace_ms=grace_ms)

    def set_network_blocked(self, sandbox: SandboxHandle, *, blocked: bool) -> None:
        del sandbox, blocked

    def delete(self, sandbox: SandboxHandle) -> None:
        del sandbox


class BackendWorkspaceTools:
    """WorkspaceTools adapter over a SandboxBackend (no git-patch requirement)."""

    def __init__(self, backend: SandboxBackend) -> None:
        self.backend = backend

    def exec_solver(
        self, workspace: SolverWorkspace, command: str, *, timeout: int = 120
    ) -> CommandResult:
        return self.backend.exec(
            workspace.sandbox, command, cwd=workspace.cwd, timeout=timeout
        )

    def collect_patch(self, workspace: SolverWorkspace) -> str:
        result = self.exec_solver(
            workspace,
            "git diff --binary HEAD 2>/dev/null || true",
            timeout=30,
        )
        return result.stdout or ""

    def backend_for_workspace(self, workspace: SolverWorkspace) -> SandboxBackend:
        del workspace
        return self.backend


class _ComposeProcess:
    def __init__(self, host_handle: BackendProcessHandle, pidfile: str) -> None:
        self.host_handle = host_handle
        self.pidfile = pidfile
