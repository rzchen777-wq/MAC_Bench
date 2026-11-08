"""Workspace capabilities required by the shared agent loop."""
from typing import Protocol
from mac_bench.runtime.types import SolverWorkspace
from mac_bench.runtime.sandbox.base import CommandResult, SandboxBackend

class WorkspaceTools(Protocol):
    def backend_for_workspace(self, workspace: SolverWorkspace) -> SandboxBackend: ...
    def exec_solver(self, workspace: SolverWorkspace, command: str, *, timeout: int = 120) -> CommandResult: ...
    def collect_patch(self, workspace: SolverWorkspace) -> str: ...


def backend_for_workspace(tools: WorkspaceTools, workspace: SolverWorkspace) -> SandboxBackend:
    return tools.backend_for_workspace(workspace)
