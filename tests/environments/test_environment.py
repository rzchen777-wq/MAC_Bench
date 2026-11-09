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
    from mac_bench.environments.engineering.preparation import (
        dependency_recipe,
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
    first = dependency_recipe(read_environment(tmp_path))
    (tmp_path / "private/dependencies/package-lock.json").write_text('{"version":2}')
    assert first != dependency_recipe(read_environment(tmp_path))


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
    assert not (root / 'assets/business_tools').exists()
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


def test_business_cache_changes_when_task_image_changes(tmp_path, monkeypatch):
    from mac_bench.environments.business import runtime

    monkeypatch.setattr(runtime, 'repository_root', lambda: tmp_path)
    root=tmp_path/'task';(root/'environment').mkdir(parents=True)
    (root/'environment/environment.json').write_text(json.dumps({'services':[{'image':'app'}]}))
    images=tmp_path/'data/environments/images';images.mkdir(parents=True)
    bundle=SimpleNamespace(root=root,manifest={'world_id':'sample','revision':'unchanged-task'})
    def select(value):
        (images/'blobs/sha256').mkdir(parents=True, exist_ok=True)
        (images/'blobs/sha256'/(value*64)).write_bytes(b'fixture image')
        (images/'index.json').write_text(json.dumps({'schemaVersion':2,'manifests':[{
            'digest':'sha256:'+value*64,'mediaType':'application/vnd.oci.image.layer.v1.tar',
            'annotations':{'org.opencontainers.image.ref.name':'app'}}]}))
        return runtime.PortableBusinessWorld(bundle,backend=object()).image_recipe
    assert select('a')==select('a')
    assert select('a')!=select('b')










def test_software_public_materials_use_one_batch_on_open_and_reset(tmp_path, monkeypatch):
    from mac_bench.environments.engineering.product import runtime
    root = tmp_path / 'world'
    (root / 'public').mkdir(parents=True)
    (root / 'private').mkdir()
    for name, data in {'task.txt': b'Task', 'develop.sh': b'echo build', 'fixture.tar': b'fixture',
                       'source.tar': b'source'}.items():
        (root / 'public' / name).write_bytes(data)
    (root / 'private/environment.json').write_text(json.dumps({'image':'python', 'build':'echo ready'}))
    (root / 'private/contract.json').write_text('{}')
    seal_bundle(root, world_id='sample', public_files=['public/task.txt','public/develop.sh'],
        metadata={'world_family':'product_application','workspace_archive':'public/source.tar',
                  'fixture_archive':'public/fixture.tar'})
    batches = []
    backend = SimpleNamespace(write_files=lambda sandbox, files: batches.append(files))
    class Runtime:
        def __init__(self, *args, **kwargs): self.sandbox = None; self.backend = kwargs["backend"]
        def open(self, **kwargs): self.sandbox = object()
        def install_source(self, path): pass
        def command(self, *args, **kwargs): return SimpleNamespace(exit_code=0)
        def close(self): self.sandbox = None
    monkeypatch.setattr(runtime.LoadedProductWorld, 'runtime_type', Runtime)
    monkeypatch.setattr(runtime, 'create_backend', lambda *args: backend)
    monkeypatch.setattr(runtime, 'prepare_environment', lambda *args, **kwargs: 'image')
    world = runtime.LoadedProductWorld(root, tmp_path / 'run')
    world.open(); world.reset(); world.close()
    assert len(batches) == 2 and batches[0] == batches[1]
    assert batches[0] == {'/work/public/task.txt': b'Task', '/work/public/develop.sh': b'echo build',
                          '/work/TASK.md': b'Task', '/work/fixtures.tar': b'fixture'}
