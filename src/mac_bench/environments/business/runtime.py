"""Run a business task from OCI images and ordinary initial-state files."""

import io
from concurrent.futures import ThreadPoolExecutor

import json
import os
import shlex
import tarfile
import tempfile
import shutil
from hashlib import sha256
from collections import Counter
from pathlib import Path

from mac_bench.runtime.paths import repository_root
from mac_bench.runtime.common.hashing import digest
from mac_bench.runtime.sandbox.modal import ModalSandboxBackend, native_host_image
from mac_bench.environments.packaging.lifecycle import (
    install_public_resources,
    install_private_verifier,
    start_service,
)
from mac_bench.environments.packaging.modal_session import ModalWorldSession
from mac_bench.environments.packaging.background import configure_hrms_shifts


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


def shared_image_blobs(directory):
    """Blobs reused by multiple application images form one cached build layer."""
    directory = Path(directory)
    index = json.loads((directory / "index.json").read_text())
    counts = Counter()
    for entry in index["manifests"]:
        tag = entry.get("annotations", {}).get("org.opencontainers.image.ref.name")
        if tag:
            _, blobs = selected_images(directory, {tag})
            counts.update(blobs)
    return sorted(name for name, count in counts.items() if count > 1)


def image_context(source, destination, blobs):
    """Use hardlinks for build contexts so local preparation does not duplicate OCI data."""
    for name in blobs:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source / name, target)
        except OSError:
            shutil.copyfile(source / name, target)


def upload_file(sandbox, source, target):
    """Stream large environment files without holding an image in host RAM."""
    sandbox.raw.filesystem.copy_from_local(source, target)


RUNTIME_DIRECTORIES = {"apps", "catalog", "tools", "verification", "runtime", "packaging", "business"}
PACKAGE_FILES = {"__init__.py", "runtime/__init__.py", 'environments/__init__.py', 'environments/errors.py'}


def runtime_source():
    root = Path(__file__).parents[2]
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        candidates = {root / name for name in PACKAGE_FILES}
        for directory in [root / 'runtime/common',
                          *(root / 'environments' / name for name in RUNTIME_DIRECTORIES)]:
            candidates.update(directory.rglob('*'))
        for file in sorted(candidates):
            if (file.is_file() and '__pycache__' not in file.parts
                    and file.suffix in {'.py', '.json', '.txt'}):
                archive.add(file, arcname='mac_bench/' + file.relative_to(root).as_posix())
    return stream.getvalue()


# Bump for changes to the prepared image layout or base dependencies.
# Runtime code, startup policy and task wording do not invalidate app images.
PREPARED_FORMAT = 1


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
        image_root = repository_root() / "data/environments/images"
        self.image_index, self.image_blobs = selected_images(
            image_root, {service["image"] for service in self.environment["services"]}
        )
        missing = [name for name in self.image_blobs if not (image_root / name).is_file()]
        if missing:
            raise FileNotFoundError('Missing task environment files; run mac-bench data download --task-id '
                                    + bundle.manifest['world_id'])
        images = sorted(entry["digest"] for entry in self.image_index["manifests"])
        # Cache the application state, not the task wording or outcome checks.
        environment_files = {
            str(path.relative_to(bundle.root / "environment")): sha256(path.read_bytes()).hexdigest()
            for path in sorted((bundle.root / "environment").rglob("*"))
            if path.is_file() and path.relative_to(bundle.root / "environment").parts[0] not in {"runtime", "images"}
        }
        self.snapshot_name = "business-" + digest({
            "environment": environment_files, "images": images,
            "prepared_format": PREPARED_FORMAT,
        })
        self.backend = backend or ModalSandboxBackend(
            vm_runtime=True,
            timeout=7200,
            snapshot_directory=cache,
            cpu=float(os.environ.get("MAC_BENCH_CPUS", "4")),
            memory_mib=int(os.environ.get("MAC_BENCH_MEMORY_MIB", "8192")),
        )
        self.sandbox = None
        self.closed = False
        self.runtime_installed = False
        self.runtime_code_installed = False

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
            index, blobs = self.image_index, self.image_blobs
            # Modal uploads build-context files through its blob store and reuses
            # unchanged layers. Large images never travel through exec logs.
            cache = (
                repository_root() / ".cache/business" / self.bundle.manifest["world_id"]
            )
            cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="images-", dir=cache) as temporary:
                common = Path(temporary) / "common"
                task = Path(temporary) / "task"
                common.mkdir()
                task.mkdir()
                shared = sorted(set(shared_image_blobs(image_root)) & set(blobs))
                image_context(image_root, common, shared)
                image_context(image_root, task, sorted(set(blobs) - set(shared)))
                (common / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
                (task / "index.json").write_text(json.dumps(index))
                # All tasks reuse the same application-image layer. Only the
                # selected manifest and task-specific deltas vary per build.
                image = (
                    native_host_image()
                    .apt_install("skopeo")
                    .pip_install(
                        "PyYAML", "jsonschema", "graphql-core", "openapi-schema-validator",
                    )
                    .add_local_dir(common, "/work/environment/images", copy=True)
                    .add_local_dir(task, "/work/environment/images", copy=True)
                )
                self.sandbox = self.backend.create(
                    name="mac-bench-business", image=image
                )
            self._event("allocated")
            self.command("mkdir -p /work/world/src /work/sessions")
            self._install_runtime()
            root = self.bundle.root / "environment"
            small_files = {}
            for file in sorted(root.rglob("*")):
                if not file.is_file() or file.relative_to(root).parts[0] in {"images", "runtime"}:
                    continue
                rel = file.relative_to(root)
                if rel.parts[0] == "host":
                    target = Path("/work").joinpath(*rel.parts[1:])
                else:
                    target = Path("/work/environment") / rel
                if file.stat().st_size < 16 * 1024 * 1024:
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

    def _install_runtime_code(self):
        if self.runtime_code_installed:
            return
        self.backend.write_file(
            self.sandbox, path="/work/runtime.tar.gz", content=runtime_source()
        )
        self.command(
            "rm -rf /work/world && mkdir -p /work/world/src && tar xzf /work/runtime.tar.gz -C /work/world/src && rm /work/runtime.tar.gz"
        )
        self.runtime_code_installed = True

    def _install_runtime(self):
        if self.runtime_installed:
            return
        self._install_runtime_code()
        root = self.bundle.root / "environment/runtime"
        files = {
            "/work/world/" + str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()
        }
        if not self.bundle.manifest["metadata"].get("private_verifier_observer_profile"):
            files["/work/world/profile.py"] = (self.bundle.root / "private/profile.py").read_bytes()
        if files:
            self.backend.write_files(self.sandbox, files=files)
        install_public_resources(self.backend, self.sandbox, bundle=self.bundle)
        install_private_verifier(self.backend, self.sandbox, self.bundle)
        self.runtime_installed = True

    def _restore(self):
        # Each new sandbox receives current code/config. Resets reuse that code
        # and restore the complete application data before checking the initial state.
        self._install_runtime_code()
        with ThreadPoolExecutor(max_workers=1) as pool:
            restored = pool.submit(self.command,
                "PYTHONPATH=/work/world/src python3 -m mac_bench.environments.business.worker /work/environment", 1200)
            self._install_runtime()
            restored.result()
        scheduling = configure_hrms_shifts(self.backend, self.sandbox,
            {'containers': [service['name'] for service in self.environment['services']]})
        if scheduling:
            self._event('scheduling_policy_applied', policy=scheduling)
        start_service(self.backend, self.sandbox)
        self.session = ModalWorldSession(self.backend, self.sandbox)
        self.assert_initial()
        self._event("ready")

    def reset(self):
        self.session.stop_episode()
        self.session.close()
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
        observed_digest = self.session.observe_digest()
        if digest(expected) != observed_digest:
            raise RuntimeError(
                "Restored native business state differs from packaged initial state"
            )
        self.initial_observation = expected
        return {"matches": True, "digest": observed_digest}


    def __exit__(self, *exc):
        if exc[0] is not None:
            self.diagnose_failure()
        self.close()
