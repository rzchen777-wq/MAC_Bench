"""Task, reset and grader retain the caller's selected backend and image."""

import io, json, tarfile
from types import SimpleNamespace
import pytest
from mac_bench.runtime.sandbox.base import PreparedImage
from mac_bench.environments.engineering.product import runtime
from mac_bench.environments.packaging.bundle import WorldBundle


@pytest.mark.parametrize("provider", ["modal", "daytona"])
def test_injected_backend_and_prepared_image_are_fixed_across_reset_and_grade(
    tmp_path, monkeypatch, provider
):
    root = tmp_path / "bundle"
    (root / "private").mkdir(parents=True)
    (root / "public").mkdir()
    (root / "public/task.txt").write_text("Implement the task")
    (root / "private/environment.json").write_text(
        json.dumps({"image": "python:3.12", "setup": "true", "build": "true"})
    )
    (root / "private/contract.json").write_text("{}")
    bundle = SimpleNamespace(
        root=root,
        manifest={
            "metadata": {
                "world_family": "product_application",
                "workspace_archive": "private/source.tar",
            },
            "public_files": ["public/task.txt"],
        },
    )
    monkeypatch.setattr(WorldBundle, "load", lambda path: bundle)
    backend = SimpleNamespace(write_files=lambda *a, **kw: None)
    prepared = PreparedImage(provider, "image", "recipe", {"provider": provider})
    instances = []
    preparations = []

    class Runtime:
        def __init__(self, env, directory, *, backend):
            self.backend = backend
            self.sandbox = None
            self.opens = []
            instances.append(self)

        def open(self, **kw):
            self.opens.append(kw)
            self.sandbox = object()

        def close(self):
            self.sandbox = None

        def install_source(self, path):
            pass

        def command(self, *a, **kw):
            return SimpleNamespace(exit_code=0)

        def build(self):
            return SimpleNamespace(exit_code=0)

        def verify(self, *a):
            return {"task_success": True}

    monkeypatch.setattr(runtime.LoadedProductWorld, "runtime_type", Runtime)

    def prepare(env, directory, **kw):
        preparations.append(kw)
        assert kw["backend"] is backend
        return prepared

    monkeypatch.setattr(runtime, "prepare_environment", prepare)

    def forbidden(*a):
        raise AssertionError("Backend must not be reselected")

    monkeypatch.setattr(runtime, "create_backend", forbidden)
    world = runtime.LoadedProductWorld(root, tmp_path / "run", backend=backend)
    world.open()
    monkeypatch.setenv("MAC_BENCH_SOFTWARE_PROVIDER", "invalid-after-open")
    world.reset()
    submission = tmp_path / "submission.tar"
    with tarfile.open(submission, "w") as archive:
        member = tarfile.TarInfo("main.py")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"pass"))
    assert world.grade_submission(submission)["task_success"]
    assert len(preparations) == 1
    assert all(item.backend is backend for item in instances)
    assert all(
        kwargs == {"prepared": prepared, "network_blocked": True}
        for item in instances
        for kwargs in item.opens
    )
    world.close()
    assert all(item.sandbox is None for item in instances)
