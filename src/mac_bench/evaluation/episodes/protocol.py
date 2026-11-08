"""Method input is public task access, a recorded model gateway and shared limits."""
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from pathlib import Path
from ..tasks import TaskView, AgentEnvironment
from .budget import EpisodeBudget


@dataclass(frozen=True)
class DevFeedback:
    """Aggregate dev feedback; private rules, tests and answers stay owner-side."""
    evaluation_status: str
    task_success: bool | None
    ps: float | None
    rr: float | None


@dataclass(frozen=True)
class EpisodeContext:
    task: TaskView
    environment: AgentEnvironment
    client: Any
    budget: EpisodeBudget
    event: Callable
    artifacts: Path | None = None
    phase: str = 'test'
    # One-shot handover: revoke/drain tools, seal candidate, score, then reflect.
    # Never supplied for test episodes.
    dev_feedback: Callable[[], DevFeedback] | None = None
    # Open lazily; ordinary tool-only methods do not create an extra workspace.
    open_execution: Callable | None = None


class Baseline(Protocol):
    """Emit agent_start/agent_end with stable agent_id for concurrency reporting.

    All workers must use context.client/environment and join before returning.
    """
    name: str

    def run(self, context: EpisodeContext) -> Any:
        """Return a dataclass/dict outcome. Final-state evaluation is harness-owned."""
        ...
