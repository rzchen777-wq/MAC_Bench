"""Thin wrappers around official Meta-Team RunManager for EvoTeam freeze dirs."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_run_at(
    run_dir: Path,
    *,
    source_team_dir: Path,
    team_name: str,
    config: dict[str, Any] | None = None,
    resume: bool = False,
) -> Any:
    """Create or resume a RunManager rooted at ``run_dir`` (not Meta-Team/runs/)."""

    from core.run_manager import RunManager  # type: ignore

    run_dir = Path(run_dir)
    source_team_dir = Path(source_team_dir)
    if run_dir.exists():
        if resume:
            return RunManager(run_dir)
        raise FileExistsError(f"Meta-Team run directory already exists: {run_dir}")
    if not source_team_dir.exists():
        raise FileNotFoundError(f"Source team directory not found: {source_team_dir}")

    run_dir.mkdir(parents=True)
    (run_dir / "cases").mkdir()
    (run_dir / "team").mkdir()
    v000 = run_dir / "team" / "v000"
    shutil.copytree(
        str(source_team_dir),
        str(v000),
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    config_data = {
        "run_id": run_dir.name,
        "team_name": team_name,
        "source_team_dir": str(source_team_dir),
        "created_at": _now_iso(),
        "evoteam_bridge": "mac_bench.baselines.meta_team",
        **(config or {}),
    }
    (run_dir / "config.json").write_text(
        json.dumps(config_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return RunManager(run_dir)


def load_run_at(run_dir: Path) -> Any:
    from core.run_manager import RunManager  # type: ignore

    run_dir = Path(run_dir)
    if not run_dir.exists():
        raise FileNotFoundError(f"Meta-Team run directory not found: {run_dir}")
    return RunManager(run_dir)


def copy_team_tree(source: Path, destination: Path) -> Path:
    """Relocatable freeze/restore copy of an official team version directory."""

    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        str(source),
        str(destination),
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    provenance = destination / "FROZEN_FROM.md"
    provenance.write_text(
        f"# Frozen Meta-Team pool\n\n"
        f"- source: `{source}`\n"
        f"- frozen_at: {_now_iso()}\n"
        f"- bridge: mac_bench.baselines.meta_team\n",
        encoding="utf-8",
    )
    return destination
