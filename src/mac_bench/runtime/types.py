"""Public task contract used by MAC-Bench solvers."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class PublicTask:
    public_id: str
    repo: str
    problem_statement: str
