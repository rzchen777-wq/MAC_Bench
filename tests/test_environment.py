import json
from pathlib import Path
from types import SimpleNamespace
from hashlib import sha256
import pytest
from mac_bench.environment_cli import check_task, export_docker_context
from mac_bench.environments.packaging.bundle import seal_bundle
from mac_bench.evaluation.tasks.catalog import task_record


def test_close_after_initial_check_failure(tmp_path):
    calls = []

    class Task:
        def open(self):
            calls.append("open")

        def assert_initial(self):
            raise RuntimeError("bad initial state")

        def close(self):
            calls.append("close")

    with pytest.raises(RuntimeError, match="initial"):
        check_task(
            SimpleNamespace(task_id="sample"),
            tmp_path,
            task_factory=lambda *args: Task(),
        )
    assert calls == ["open", "close"]


def test_check_resets_and_closes_without_solver(tmp_path):
    calls = []

    class Task:
        def open(self):
            calls.append("open")

        def assert_initial(self):
            calls.append("check")
            return {"matches": True}

        def reset(self):
            calls.append("reset")

        def close(self):
            calls.append("close")

    result = check_task(
        SimpleNamespace(task_id="sample"), tmp_path, task_factory=lambda *args: Task()
    )
    assert calls == ["open", "check", "reset", "check", "close"]
    assert result["reset"]["matches"]


def test_dependency_cache_reuses_setup_across_task_builds(tmp_path, monkeypatch):
    from mac_bench.environments.engineering.product import runtime

    monkeypatch.setenv("MAC_BENCH_ENVIRONMENT_CACHE", str(tmp_path / "cache"))
    calls = []

    class FakeRuntime:
        def __init__(self, env, directory):
            self.directory = Path(directory)
            self.directory.mkdir(parents=True, exist_ok=True)
            self.provider = self.directory / "provider"
            self.provider.mkdir(exist_ok=True)
            self.backend = self
            self.sandbox = "sandbox"

        def snapshot_exists(self, *, name):
            return (
                self.provider / (sha256(name.encode()).hexdigest() + ".json")
            ).is_file()

        def open(self):
            calls.append("open")

        def command(self, name, command, **kwargs):
            calls.append(command)
            return SimpleNamespace(exit_code=0)

        def create_snapshot(self, sandbox, *, name, timeout):
            (self.provider / (sha256(name.encode()).hexdigest() + ".json")).write_text(
                "{}"
            )

        def close(self):
            calls.append("close")

    monkeypatch.setattr(runtime, "ProductRuntime", FakeRuntime)
    first = {
        "image": "python:3.12-slim",
        "setup": "install dependencies",
        "build": "task A",
    }
    second = {**first, "build": "task B"}
    assert runtime.prepare_environment(
        first, tmp_path / "run1"
    ) == runtime.prepare_environment(second, tmp_path / "run2")
    assert calls == ["open", "install dependencies", "close"]
    assert len(list((tmp_path / "run2/environment/provider").glob("*.json"))) == 1


def test_docker_context_has_only_public_workspace(tmp_path):
    world = tmp_path / "world"
    (world / "public").mkdir(parents=True)
    (world / "private").mkdir()
    (world / "public/task.txt").write_text("Modify the project")
    (world / "public/source.tar").write_bytes(b"example archive")
    (world / "private/reference.tar").write_bytes(b"private solution")
    env = {"image": "python:3.12-slim", "setup": "echo setup", "build": "echo task"}
    (world / "private/environment.json").write_text(json.dumps(env))
    seal_bundle(
        world,
        world_id="sample",
        public_files=["public/task.txt"],
        metadata={
            "world_family": "developer_release",
            "workspace_archive": "public/source.tar",
        },
    )
    record = task_record(world, category="developer_release")
    out = export_docker_context(record, tmp_path / "context")
    assert "echo setup" in (out / "base/setup.sh").read_text()
    assert "echo task" in (out / "task/initialize.sh").read_text()
    assert not any(
        "private" in p.parts or p.name == "reference.tar" for p in out.rglob("*")
    )


def test_dependency_cache_includes_lockfile_content(tmp_path, monkeypatch):
    from mac_bench.environments.engineering.product.runtime import (
        dependency_cache,
        read_environment,
    )

    monkeypatch.setenv("MAC_BENCH_ENVIRONMENT_CACHE", str(tmp_path / "cache"))
    (tmp_path / "private/dependencies").mkdir(parents=True)
    (tmp_path / "private/dependencies/package-lock.json").write_text('{"version":1}')
    environment = {
        "image": "node:20",
        "setup": "echo base",
        "build": "npm ci --offline",
        "dependency_files": {
            "package-lock.json": "private/dependencies/package-lock.json"
        },
        "dependency_setup": "npm ci --ignore-scripts",
    }
    (tmp_path / "private/environment.json").write_text(json.dumps(environment))
    first = dependency_cache(read_environment(tmp_path))
    (tmp_path / "private/dependencies/package-lock.json").write_text('{"version":2}')
    assert first != dependency_cache(read_environment(tmp_path))


def test_business_runtime_and_tool_assets_import_together(tmp_path):
    """The VM must not depend on Python modules left in an author's snapshot."""
    import io
    import os
    import subprocess
    import sys
    import tarfile
    from mac_bench.environments.business.runtime import runtime_source

    with tarfile.open(fileobj=io.BytesIO(runtime_source()), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    root = tmp_path / "mac_bench/environments"
    assets = root / "assets/business_tools"
    manifest = json.loads((assets / "manifest.json").read_text())
    for name in manifest["remote_files"] + manifest["runtime_files"]:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((assets / name).read_bytes().replace(
            b"mac_bench.construction.realize", b"mac_bench.environments"))
    program = """
from mac_bench.environments.catalog.factories import EntityFactory, AppConnection, attach_connections
from mac_bench.environments.tools import CallJournal, Role, WorldTools, build_registry
from mac_bench.environments.packaging import server, rpc_client
from mac_bench.environments.business import worker
from mac_bench.environments.verification.observation import NativeObserver, ReadSource, frappe_source, pretix_source
from mac_bench.environments.verification.snapshot import source_cards
from mac_bench.environments.verification.evaluation import evaluate
from mac_bench.environments.catalog.factories.apps import bigcapital, frappe, pretix, twenty
"""
    subprocess.run([sys.executable, "-c", program], cwd=tmp_path,
                   env={**os.environ, "PYTHONPATH": str(tmp_path)}, check=True,
                   capture_output=True, text=True)
