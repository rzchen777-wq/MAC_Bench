"""Minimal public-task and workspace contracts used by MAC-Bench solvers."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
from .sandbox.base import SandboxHandle

@dataclass(frozen=True)
class PublicTask:
    public_id: str
    repo: str
    problem_statement: str

@dataclass(frozen=True)
class SolverWorkspace:
    sandbox: SandboxHandle | None
    cwd: str = "workspace/repo"
    kind: Literal["software", "artifact"] = "software"
