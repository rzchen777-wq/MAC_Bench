"""Run a business task from OCI images and ordinary initial-state files."""

import io
import json
import os
import shlex
import tarfile
import tempfile
import shutil
from pathlib import Path

from mac_bench.runtime.paths import repository_root
from mac_bench.runtime.common.hashing import digest
from mac_bench.runtime.sandbox.modal import ModalSandboxBackend, native_host_image
from mac_bench.environments.packaging.lifecycle import (
    install_query_tools,
    install_private_verifier,
    start_service,
)
from mac_bench.environments.packaging.modal_session import ModalWorldSession


def selected_images(directory, tags):
    """Select a task's OCI descriptors and shared blobs."""
    directory = Path(directory)
    index = json.loads((directory / "index.json").read_text())
    entries = [
        x
        for x in index["manifests"]
        if x.get("annotations", {}).get("org.opencontainers.image.ref.name") in tags
    ]
    if len(entries) != len(set(tags)):
        raise ValueError(
            "Task application images are missing; install the environment data"
        )
    blobs = set()

    def include(descriptor):
        algorithm, value = descriptor["digest"].split(":", 1)
        if (
            algorithm != "sha256"
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("Invalid OCI digest")
        name = "blobs/sha256/" + value
        if name in blobs:
            return
        blobs.add(name)
        if "manifest" in descriptor["mediaType"] or "index" in descriptor["mediaType"]:
            manifest = json.loads((directory / name).read_text())
            for child in [
                *manifest.get("manifests", []),
                *manifest.get("layers", []),
                *([manifest["config"]] if "config" in manifest else []),
            ]:
                include(child)

    for entry in entries:
        include(entry)
    return {**index, "manifests": entries}, sorted(blobs)


def upload_file(sandbox, source, target):
    """Stream large environment files without holding an image in host RAM."""
    sandbox.raw.filesystem.copy_from_local(source, target)


def runtime_source():
    root = Path(__file__).parents[2]
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for file in sorted(root.rglob("*")):
            if (
                file.is_file()
                and "__pycache__" not in file.parts
                and file.suffix in {".py", ".json", ".txt"}
            ):
                archive.add(file, arcname="mac_bench/" + str(file.relative_to(root)))
    return stream.getvalue()


class PortableBusinessWorld:
    def __init__(
        self, bundle, *, backend=None, on_event=None, diagnostic_directory=None
    ):
        self.bundle = bundle
        self.on_event = on_event
        self.diagnostic_directory = diagnostic_directory
        config_path = bundle.root / "environment/environment.json"
        self.environment = json.loads(config_path.read_text())
        cache = repository_root() / ".cache/business" / bundle.manifest["world_id"]
        self.cache_directory = cache
        self.snapshot_name = "business-" + bundle.manifest["revision"]
        self.backend = backend or ModalSandboxBackend(
            vm_runtime=True,
            timeout=7200,
            snapshot_directory=cache,
            cpu=float(os.environ.get("MAC_BENCH_CPUS", "4")),
            memory_mib=int(os.environ.get("MAC_BENCH_MEMORY_MIB", "16384")),
        )
        self.sandbox = None
        self.closed = False

    def command(self, command, timeout=300):
        result = self.backend.exec(self.sandbox, command, timeout=timeout)
        if result.exit_code:
            raise RuntimeError(result.stdout[-1800:])
        return result.stdout

    def __enter__(self):
        if self.backend.snapshot_exists(name=self.snapshot_name):
            return self._open()
        import fcntl

        self.cache_directory.mkdir(parents=True, exist_ok=True)
        with (self.cache_directory / "prepare.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._open()

    def _open(self):
        try:
            if self.backend.snapshot_exists(name=self.snapshot_name):
                self.sandbox = self.backend.create(
                    name="mac-bench-business", snapshot=self.snapshot_name
                )
                self._event("allocated")
                self._restore()
                return self
            image_root = repository_root() / "data/environments/images"
            index, blobs = selected_images(
                image_root, {s["image"] for s in self.environment["services"]}
            )
            # Modal uploads build-context files through its blob store and reuses
            # unchanged layers. Large images never travel through exec logs.
            cache = (
                repository_root() / ".cache/business" / self.bundle.manifest["world_id"]
            )
            cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="images-", dir=cache) as temporary:
                directory = Path(temporary)
                for name in blobs:
                    target = directory / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(image_root / name, target)
                    except OSError:
                        shutil.copyfile(image_root / name, target)
                (directory / "index.json").write_text(json.dumps(index))
                (directory / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
                image = (
                    native_host_image()
                    .apt_install("skopeo")
                    .pip_install(
                        "PyYAML",
                        "jsonschema",
                        "graphql-core",
                        "openapi-schema-validator",
                    )
                    .add_local_dir(directory, "/work/environment/images", copy=True)
                )
                self.sandbox = self.backend.create(
                    name="mac-bench-business", image=image
                )
            self._event("allocated")
            self.command("mkdir -p /work/world/src /work/sessions")
            self.backend.write_file(
                self.sandbox, path="/work/runtime.tar.gz", content=runtime_source()
            )
            self.command(
                "tar xzf /work/runtime.tar.gz -C /work/world/src && rm /work/runtime.tar.gz"
            )
            root = self.bundle.root / "environment"
            small_files = {}
            for file in sorted(root.rglob("*")):
                if not file.is_file() or "images" in file.relative_to(root).parts:
                    continue
                rel = file.relative_to(root)
                if rel.parts[0] == "runtime":
                    target = Path("/work/world").joinpath(*rel.parts[1:])
                elif rel.parts[0] == "host":
                    target = Path("/work").joinpath(*rel.parts[1:])
                else:
                    target = Path("/work/environment") / rel
                if file.suffix == ".py":
                    content = (
                        file.read_bytes()
                        .replace(
                            b"evoteam.collaboration.realize", b"mac_bench.environments"
                        )
                        .replace(
                            b"mac_bench.construction.realize", b"mac_bench.environments"
                        )
                        .replace(
                            b"evoteam.collaboration.common", b"mac_bench.runtime.common"
                        )
                    )
                    small_files[str(target)] = content
                elif file.stat().st_size < 16 * 1024 * 1024:
                    small_files[str(target)] = file.read_bytes()
                else:
                    self.command(shlex.join(["mkdir", "-p", str(target.parent)]), 30)
                    upload_file(self.sandbox, file, str(target))
            self.backend.write_files(self.sandbox, files=small_files)
            self.command(
                "PYTHONPATH=/work/world/src python3 -m mac_bench.environments.business.worker /work/environment --prepare-only",
                1200,
            )
            self.command("rm -rf /work/environment/images/blobs", 60)
            self.backend.create_snapshot(
                self.sandbox, name=self.snapshot_name, timeout=600
            )
            self._restore()
            return self
        except BaseException:
            self.diagnose_failure()
            self.close()
            raise

    def _restore(self):
        # Application snapshots cache data and dependencies. Runtime code always
        # comes from the installed package, including after local edits.
        self.backend.write_file(
            self.sandbox, path="/work/runtime.tar.gz", content=runtime_source()
        )
        self.command(
            "tar xzf /work/runtime.tar.gz -C /work/world/src && rm /work/runtime.tar.gz"
        )
        self.command(
            "PYTHONPATH=/work/world/src python3 -m mac_bench.environments.business.worker /work/environment",
            1200,
        )
        profile = (
            (self.bundle.root / "private/profile.py")
            .read_bytes()
            .replace(b"evoteam.collaboration.realize", b"mac_bench.environments")
            .replace(b"mac_bench.construction.realize", b"mac_bench.environments")
        )
        self.backend.write_file(
            self.sandbox, path="/work/world/profile.py", content=profile
        )
        install_query_tools(
            self.backend,
            self.sandbox,
            namespace="mac_bench.environments",
            bundle=self.bundle,
        )
        install_private_verifier(self.backend, self.sandbox, self.bundle)
        start_service(self.backend, self.sandbox)
        self.session = ModalWorldSession(self.backend, self.sandbox)
        self.assert_initial()
        self._event("ready")

    def reset(self):
        self.command(
            "pkill -f '^python3 -m mac_bench.environments.packaging.server' || true", 30
        )
        self._restore()

    def diagnose_failure(self):
        if self.sandbox is None or self.closed or self.diagnostic_directory is None:
            return
        directory = Path(self.diagnostic_directory)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            result = self.backend.exec(
                self.sandbox,
                'tail -c 20000 /work/world-server.log 2>/dev/null; docker ps --format "{{.Names}} {{.Status}}"',
                timeout=30,
            )
            (directory / "environment.log").write_text(result.stdout)
        except Exception:
            pass

    def close(self):
        if self.sandbox is not None and not self.closed:
            self.backend.delete(self.sandbox)
            self.closed = True
            self._event("closed")

    def _event(self, phase, **details):
        if self.on_event:
            self.on_event(
                {"phase": phase, "sandbox_id": self.sandbox.id, "closed": self.closed, **details}
            )


    def assert_initial(self):
        expected = json.loads((self.bundle.root / "private/initial.json").read_text())
        observed = self.session.observe()
        if digest(expected) != digest(observed):
            raise RuntimeError(
                "Restored native business state differs from packaged initial state"
            )
        self.initial_observation = observed
        return {"matches": True, "digest": digest(observed)}


    def __exit__(self, *exc):
        if exc[0] is not None:
            self.diagnose_failure()
        self.close()
