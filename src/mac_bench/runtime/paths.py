"""Repository, output and immutable-evidence locations for MAC-Bench."""
import json
import os
from functools import lru_cache
from pathlib import Path


def repository_root():
    configured = os.environ.get('MAC_BENCH_ROOT')
    if configured:
        root = Path(configured).expanduser().resolve()
        if (root/'src/mac_bench').is_dir() and (root/'pyproject.toml').is_file():
            return root
        raise ValueError('MAC_BENCH_ROOT must identify a MAC-Bench checkout')
    for root in Path(__file__).resolve().parents:
        if (root/'src/mac_bench').is_dir() and (root/'pyproject.toml').is_file():
            return root
    raise RuntimeError('Task assets require a MAC-Bench checkout or MAC_BENCH_ROOT')


def artifact_root():
    configured = os.environ.get('MAC_BENCH_OUTPUT_ROOT')
    return Path(configured).expanduser().resolve() if configured else repository_root()/'outputs'


def artifact_directory(kind, run_id):
    if kind not in {'evaluation','environment'}:
        raise ValueError('Unknown output kind')
    if not run_id or Path(run_id).name != run_id or run_id in {'.','..'}:
        raise ValueError('run_id must be one directory name')
    return artifact_root()/kind/run_id


def evidence_path(value):
    path = Path(value)
    return path if path.is_absolute() else repository_root()/path
