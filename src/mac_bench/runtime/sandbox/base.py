from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class SandboxHandle:
    """Provider-neutral reference to a live sandbox."""

    id: str
    provider: str
    raw: Any = field(compare=False, repr=False)


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    duration_seconds: float


@dataclass(frozen=True)
class BackendProcessHandle:
    id: str
    raw: Any = field(compare=False, repr=False)


@dataclass(frozen=True)
class BackendProcessSnapshot:
    status: Literal["running", "completed", "failed", "killed"]
    output: str
    exit_code: int | None = None
    signal: str | None = None
    stdin_open: bool = True
    output_truncated: bool = False


class SandboxChannel(Protocol):
    """A persistent stdin/stdout channel to a trusted program in the sandbox."""
    def write(self, data: bytes) -> None: ...
    def read(self) -> Iterator[bytes]: ...
    def close(self) -> None: ...


class SandboxBackend(ABC):
    """Provider-neutral contract used by feasibility and task runners."""

    @abstractmethod
    def create(
        self,
        *,
        name: str,
        snapshot: str | None = None,
        image: str | None = None,
        network_blocked: bool = False,
    ) -> SandboxHandle:
        """Create a sandbox.

        Prefer ``image`` (registry/OCI tag) as the environment identity. Optional
        ``snapshot`` is a provider-cached template derived from an image. Do not
        bake task-specific patches/prompts into the environment; upload those
        after the sandbox is live.
        """
        raise NotImplementedError

    @abstractmethod
    def snapshot_exists(self, *, name: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def create_snapshot(
        self, sandbox: SandboxHandle, *, name: str, timeout: int = 600
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def clone(
        self,
        sandbox: SandboxHandle,
        *,
        url: str,
        path: str,
        revision: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def exec(
        self,
        sandbox: SandboxHandle,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandResult:
        raise NotImplementedError

    @abstractmethod
    def write_file(
        self, sandbox: SandboxHandle, *, path: str, content: bytes
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def read_file(self, sandbox: SandboxHandle, *, path: str) -> bytes:
        raise NotImplementedError

    def resolve_path(
        self,
        sandbox: SandboxHandle,
        *,
        path: str,
        cwd: str | None = None,
        kind: str = "any",
    ) -> str:
        """Resolve an existing sandbox path without following it outside its sandbox.

        Backends used by model-facing file tools implement this capability;
        evaluator-only backends may leave it unsupported.
        """

        del sandbox, path, cwd, kind
        raise NotImplementedError("sandbox backend does not support secure path resolution")

    def replace_file(
        self, sandbox: SandboxHandle, *, path: str, content: bytes
    ) -> None:
        """Atomically replace one regular file when supported by the backend."""

        self.write_file(sandbox, path=path, content=content)

    def delete_file(self, sandbox: SandboxHandle, *, path: str) -> None:
        del sandbox, path
        raise NotImplementedError("sandbox backend does not support file deletion")

    def start_process(
        self,
        sandbox: SandboxHandle,
        *,
        session_id: str,
        command: str,
        cwd: str,
        tty: bool,
    ) -> BackendProcessHandle:
        del sandbox, session_id, command, cwd, tty
        raise NotImplementedError("sandbox backend does not support managed processes")

    def open_channel(self, sandbox: SandboxHandle, *, argv: Sequence[str], cwd: str) -> SandboxChannel:
        """Start one persistent capability worker, separate from agent commands."""
        raise NotImplementedError('sandbox backend does not support persistent channels')

    def poll_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        wait_ms: int = 0,
    ) -> BackendProcessSnapshot:
        del sandbox, handle, wait_ms
        raise NotImplementedError("sandbox backend does not support managed processes")

    def write_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        data: str,
        eof: bool,
    ) -> None:
        del sandbox, handle, data, eof
        raise NotImplementedError("sandbox backend does not support managed processes")

    def kill_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        grace_ms: int = 1_000,
    ) -> None:
        del sandbox, handle, grace_ms
        raise NotImplementedError("sandbox backend does not support managed processes")

    @abstractmethod
    def set_network_blocked(
        self, sandbox: SandboxHandle, *, blocked: bool
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, sandbox: SandboxHandle) -> None:
        raise NotImplementedError
