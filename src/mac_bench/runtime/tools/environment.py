from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from mac_bench.runtime.types import SolverWorkspace
from mac_bench.runtime.sandbox.base import CommandResult, SandboxBackend

from .runtime import ToolErrorCode, ToolExecutionError


@dataclass(frozen=True)
class SafePath:
    input_path: str
    relative_path: str
    resolved_path: str


class ExecutionEnvironment:
    """Workspace-scoped access to one sandbox backend."""

    def __init__(
        self,
        backend: SandboxBackend,
        workspace: SolverWorkspace,
        *,
        max_read_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self.backend = backend
        self.workspace = workspace
        self.max_read_bytes = max_read_bytes

    def resolve_path(self, path: str, *, kind: str = "file") -> SafePath:
        raw, candidate = self._validate_path(path, allow_root=True)
        try:
            root = self.backend.resolve_path(
                self.workspace.sandbox,
                path=".",
                cwd=self.workspace.cwd,
                kind="directory",
            )
            resolved = self.backend.resolve_path(
                self.workspace.sandbox,
                path=candidate,
                cwd=self.workspace.cwd,
                kind=kind,
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError(
                ToolErrorCode.FILE_NOT_FOUND,
                f"workspace path {raw!r} does not exist",
            ) from exc
        except (IsADirectoryError, NotADirectoryError) as exc:
            raise ToolExecutionError(
                ToolErrorCode.NOT_A_REGULAR_FILE,
                f"workspace path {raw!r} is not a {kind}",
            ) from exc
        except NotImplementedError as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                "sandbox backend lacks secure path resolution",
            ) from exc
        root_path = PurePosixPath(root)
        resolved_path = PurePosixPath(resolved)
        if resolved_path != root_path and root_path not in resolved_path.parents:
            raise ToolExecutionError(
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                f"path {raw!r} resolves outside the workspace",
            )
        relative = "." if resolved_path == root_path else str(resolved_path.relative_to(root_path))
        return SafePath(raw, relative, str(resolved_path))

    def resolve_write_path(self, path: str) -> SafePath:
        """Resolve a possibly-new path through existing workspace symlinks."""

        raw, candidate = self._validate_path(path)
        try:
            root = self.backend.resolve_path(
                self.workspace.sandbox,
                path=".",
                cwd=self.workspace.cwd,
                kind="directory",
            )
            resolved = self.backend.resolve_path(
                self.workspace.sandbox,
                path=candidate,
                cwd=self.workspace.cwd,
                kind="write",
            )
        except NotImplementedError as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                "sandbox backend lacks secure write-path resolution",
            ) from exc
        except Exception as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"failed to resolve writable path {raw!r}: {type(exc).__name__}: {exc}",
            ) from exc
        root_path = PurePosixPath(root)
        resolved_path = PurePosixPath(resolved)
        if resolved_path == root_path or root_path not in resolved_path.parents:
            raise ToolExecutionError(
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                f"path {raw!r} resolves outside the workspace",
            )
        relative = str(resolved_path.relative_to(root_path))
        return SafePath(raw, relative, str(resolved_path))

    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> tuple[SafePath, bytes]:
        safe = self.resolve_path(path, kind="file")
        try:
            data = self.backend.read_file(
                self.workspace.sandbox,
                path=safe.resolved_path,
            )
        except Exception as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"failed to read {path!r}: {type(exc).__name__}: {exc}",
            ) from exc
        limit = self.max_read_bytes if max_bytes is None else max_bytes
        if len(data) > limit:
            raise ToolExecutionError(
                ToolErrorCode.FILE_TOO_LARGE,
                f"file {path!r} exceeds the {limit}-byte read limit",
                details={"byte_count": len(data), "max_bytes": limit},
            )
        return safe, data

    @staticmethod
    def version(data: bytes) -> str:
        return f"sha256:{hashlib.sha256(data).hexdigest()}"

    def replace_bytes(self, safe: SafePath, data: bytes) -> None:
        try:
            self.backend.replace_file(
                self.workspace.sandbox,
                path=safe.resolved_path,
                content=data,
            )
        except Exception as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"failed to replace {safe.input_path!r}: {type(exc).__name__}: {exc}",
            ) from exc

    def delete_path(self, safe: SafePath) -> None:
        try:
            self.backend.delete_file(
                self.workspace.sandbox,
                path=safe.resolved_path,
            )
        except Exception as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"failed to delete {safe.input_path!r}: {type(exc).__name__}: {exc}",
            ) from exc

    @staticmethod
    def _validate_path(path: str, *, allow_root: bool = False) -> tuple[str, str]:
        raw = str(path).strip()
        if not raw or "\x00" in raw:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty workspace path",
            )
        candidate = PurePosixPath(raw)
        if (
            (not candidate.is_absolute() and ".." in candidate.parts)
            or (not allow_root and str(candidate) in {"", "."})
        ):
            raise ToolExecutionError(
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                f"path {raw!r} is outside the workspace",
            )
        return raw, str(candidate)

    def exec_controlled(self, command: str, *, timeout: int = 120) -> CommandResult:
        try:
            return self.backend.exec(
                self.workspace.sandbox,
                command,
                cwd=self.workspace.cwd,
                timeout=timeout,
            )
        except Exception as exc:
            raise ToolExecutionError(
                ToolErrorCode.EXECUTION_ERROR,
                f"sandbox command failed: {type(exc).__name__}: {exc}",
            ) from exc
