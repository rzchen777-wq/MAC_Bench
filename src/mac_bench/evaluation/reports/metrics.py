"""Episode metrics and evaluation failure reporting."""

import math
from collections import Counter


def evaluation_fields(verdict):
    status = verdict["status"]
    valid = status in {"passed", "failed"}
    success = status == "passed" if valid else None
    gc = verdict.get("gc") if valid else None
    if gc is not None and (
        isinstance(gc, bool) or not isinstance(gc, (int, float))
        or not math.isfinite(gc) or not 0 <= gc <= 1
    ):
        raise ValueError("Invalid goal-condition score")
    if valid and verdict.get("task_success", success) is not success:
        raise ValueError("Inconsistent task success verdict")
    if valid and "complete" in verdict and verdict["complete"] is not success:
        raise ValueError("Inconsistent completion verdict")
    if success and gc is not None and gc != 1:
        raise ValueError("A successful task must satisfy all goals")
    return {
        "evaluation_status": status,
        "task_complete": success,
        "task_success": success,
        "score": float(success) if valid else None,
        "gc": gc,
        "ps": gc,
        "rr": float(success) if valid else None,
    }


def summarize_episodes(episodes):
    """Mean episode SR/GC with an explicit common denominator and exclusions.

    Repeated episodes of one world are repeats, not additional benchmark tasks.
    A main-experiment caller supplies its preselected rollout per task.
    """
    eligible, excluded = [], Counter()
    for row in episodes:
        if row.get("status") != "completed":
            excluded["episode_not_completed"] += 1
        elif row.get("evaluation_status") not in {"passed", "failed"}:
            excluded["evaluation_unavailable"] += 1
        elif row.get("gc") is None or row.get("task_success") is None:
            excluded["goal_metrics_unavailable"] += 1
        else:
            eligible.append(row)
    n = len(eligible)
    return {
        "episodes": len(episodes),
        "scored_episodes": n,
        "excluded": dict(excluded),
        "rr": sum(row["task_success"] for row in eligible) / n if n else None,
        "ps": sum(row["gc"] for row in eligible) / n if n else None,
        "sr": sum(row["task_success"] for row in eligible) / n if n else None,
        "gc": sum(row["gc"] for row in eligible) / n if n else None,
    }
