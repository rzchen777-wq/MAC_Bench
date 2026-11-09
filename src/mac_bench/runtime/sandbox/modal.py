"""Modal implementation of the provider-neutral sandbox contract.

Default sandboxes are gVisor. Pass ``vm_runtime=True`` to get a real Linux
kernel with dockerd for business application containers.
"""

from __future__ import annotations

import json
from hashlib import sha256
import shlex
import threading
import time
import uuid
from pathlib import PurePosixPath
from typing import Mapping

from mac_bench.runtime.sandbox.base import (
    CommandResult,
    SandboxBackend,
    SandboxHandle,
    PreparedImage,
    shell_from_file,
)


APP_NAME = "mac-bench"


class _ModalChannel:
    def __init__(self, process):
        self.process = process

        def drain_errors():
            for _ in process.stderr:
                pass

        threading.Thread(target=drain_errors, daemon=True).start()

    def write(self, data):
        self.process.stdin.write(data)
        self.process.stdin.drain()

    def read(self):
        for chunk in self.process.stdout:
            yield chunk.encode() if isinstance(chunk, str) else bytes(chunk)

    def close(self):
        self.process.stdin.write_eof()
        self.process.stdin.drain()
        code = self.process.wait()
        if code:
            raise RuntimeError(f"Sandbox capability worker exited with status {code}")


def native_host_image():
    import modal

    return (
        modal.Image.from_registry("ubuntu:24.04", add_python="3.12")
        .env({"DEBIAN_FRONTEND": "noninteractive"})
        .apt_install(
            [
                "docker.io",
                "docker-compose-v2",
                "python3",
                "python3-pip",
                "python3-venv",
                "curl",
                "ca-certificates",
                "tar",
                "gzip",
                "iproute2",
                "procps",
                "iputils-ping",
            ]
        )
        .pip_install("requests>=2.31.0", "psycopg2-binary>=2.9.9")
        .run_commands("mkdir -p /work")
    )


class ModalSandboxBackend(SandboxBackend):
    """Create/exec/file/delete against Modal Sandboxes.

    ``vm_runtime=True`` starts ``dockerd`` as the sandbox entrypoint.
    """

    provider = "modal"

    def __init__(
        self,
        *,
        app_name: str = APP_NAME,
        vm_runtime: bool = False,
        timeout: int = 3600,
        cpu: float = 2,
        memory_mib: int = 8192,
        gpu: str | None = None,
        idle_timeout: int | None = None,
    ) -> None:
        import modal

        self._modal = modal
        self._app_name = app_name
        self._app = modal.App.lookup(app_name, create_if_missing=True)
        self._vm_runtime = vm_runtime
        self._timeout = timeout
        self._cpu = cpu
        self._memory_mib = memory_mib
        self._gpu = gpu
        self._idle_timeout = idle_timeout
        from .providers import BackendConfig

        self.config = BackendConfig(
            "modal",
            "business" if vm_runtime else "software",
            cpu,
            memory_mib,
            0,
            timeout,
            app_name=app_name,
        )

    def business_image(self, common, task):
        return (
            native_host_image()
            .apt_install("skopeo")
            .pip_install(
                "PyYAML", "jsonschema", "graphql-core", "openapi-schema-validator"
            )
            .add_local_dir(common, "/work/environment/images", copy=True)
            .add_local_dir(task, "/work/environment/images", copy=True)
        )

    def capture_business_image(self, sandbox, *, recipe, timeout=600):
        command = (
            "PYTHONPATH=/work/world/src python3 -m mac_bench.environments.business.worker "
            "/work/environment --prepare-only"
        )
        result = self.exec(sandbox, command, timeout=1200)
        if result.exit_code:
            raise RuntimeError(
                "Business image preparation failed: " + result.stdout[-12000:]
            )
        return self.capture_image(sandbox, recipe=recipe, timeout=timeout)

    def upload_file(self, sandbox, *, source, path):
        sandbox.raw.filesystem.copy_from_local(source, path)

    def create(
        self,
        *,
        name: str,
        prepared: PreparedImage | None = None,
        image: str | None = None,
        network_blocked: bool = False,
    ) -> SandboxHandle:
        modal = self._modal
        if prepared:
            if image is not None:
                raise ValueError("Specify a prepared image or a base image, not both")
            self.validate_image(prepared)
            modal_image = modal.Image.from_id(prepared.reference)
        elif image is None:
            modal_image = (
                native_host_image() if self._vm_runtime else modal.Image.debian_slim()
            )
        elif not isinstance(image, str):
            modal_image = image
        else:
            modal_image = modal.Image.from_registry(image)

        kwargs: dict = {
            "app": self._app,
            "image": modal_image,
            "timeout": self._timeout,
            "cpu": self._cpu,
            "memory": self._memory_mib,
            "block_network": network_blocked,
            "workdir": "/work",
        }
        if self._idle_timeout is not None:
            kwargs["idle_timeout"] = self._idle_timeout
        if self._gpu:
            kwargs["gpu"] = self._gpu
        if self._vm_runtime:
            kwargs["experimental_options"] = {"vm_runtime": True}
            raw = modal.Sandbox.create(
                "/usr/bin/dockerd", "--storage-driver=overlay2",
                "--feature=containerd-snapshotter=false", **kwargs
            )
            handle = SandboxHandle(id=str(raw.object_id), provider="modal", raw=raw)
            try:
                self._wait_dockerd(handle)
            except BaseException:
                # Creation has already allocated a sandbox. Do not lose its
                # handle when Docker startup fails before returning to caller.
                self.delete(handle)
                raise
            return handle
        raw = modal.Sandbox.create("sleep", "infinity", **kwargs)
        return SandboxHandle(id=str(raw.object_id), provider="modal", raw=raw)

    def _wait_dockerd(self, sandbox: SandboxHandle, *, seconds: int = 120) -> None:
        result = self.exec(
            sandbox,
            "for i in $(seq 1 {n}); do "
            "if [ -S /var/run/docker.sock ] && docker info >/dev/null 2>&1; then "
            "test \"$(docker info --format '{{{{.Driver}}}}')\" = overlay2 || exit 1; "
            "echo ready; exit 0; fi; sleep 1; done; "
            "echo 'dockerd not ready' >&2; exit 1".format(n=seconds),
            timeout=seconds + 15,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"dockerd not ready: {result.stdout}")

    @property
    def image_scope(self):
        return {
            "provider": self.provider,
            "app_name": self._app_name,
            "docker": self._vm_runtime,
            **({"storage_driver": "overlay2"} if self._vm_runtime else {}),
        }

    def image_exists(self, image):
        self.validate_image(image)
        from modal.exception import NotFoundError

        try:
            # Resolving an existing ID queries metadata; it does not rebuild layers.
            self._modal.Image.from_id(image.reference).build(self._app)
            return True
        except NotFoundError:
            return False

    def capture_image(self, sandbox, *, recipe, timeout=600):
        if sandbox.provider != self.provider or timeout < 1:
            raise ValueError("Invalid Modal image source or timeout")
        image = sandbox.raw.snapshot_filesystem(timeout=timeout, ttl=None)
        if not isinstance(image.object_id, str) or not image.object_id.startswith(
            "im-"
        ):
            raise RuntimeError("Modal snapshot returned no image identity")
        return PreparedImage(self.provider, image.object_id, recipe, self.image_scope)

    def exec(
        self,
        sandbox: SandboxHandle,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        # Shell output can contain arbitrary bytes (for example head/cat on a
        # PDF). Decode previews ourselves; a bad byte must not abort an episode.
        exec_kwargs: dict = {"text": False}
        if cwd:
            exec_kwargs["workdir"] = cwd
        if env:
            exec_kwargs["env"] = dict(env)
        if timeout is not None:
            exec_kwargs["timeout"] = timeout
        command_file = None
        dispatch = command
        # Modal caps aggregate argv at 64 KiB, independently of the shell's
        # limit. Transport large programs as files, then evaluate the exact
        # text in the same login shell with stdin left available to the command.
        if len(command.encode("utf-8")) + 16 >= 65536:
            command_file = "/tmp/mac-bench-exec-" + uuid.uuid4().hex + ".sh"
            sandbox.raw.filesystem.write_bytes(command.encode("utf-8"), command_file)
            dispatch = shell_from_file(command_file)
        try:
            process = sandbox.raw.exec("bash", "-lc", dispatch, **exec_kwargs)
            process.wait()
        finally:
            if command_file is not None:
                # Usually already removed before eval. Also clean up if exec
                # failed before the remote shell could start.
                try:
                    sandbox.raw.exec(
                        "rm", "-f", "--", command_file, text=False, timeout=30
                    ).wait()
                except Exception:
                    pass

        def decoded(stream):
            value = stream.read() or b""
            return (
                value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else value
            )

        stdout = decoded(process.stdout) + decoded(process.stderr)
        code = 0 if process.returncode is None else int(process.returncode)
        return CommandResult(
            command=command,
            exit_code=code,
            stdout=stdout,
            duration_seconds=time.monotonic() - started,
        )

    def write_file(self, sandbox: SandboxHandle, *, path: str, content: bytes) -> None:
        if not path.startswith("/"):
            raise ValueError(f"sandbox path must be absolute: {path}")
        parent = str(PurePosixPath(path).parent)
        if parent not in {"", "/"}:
            mkdir = self.exec(sandbox, f"mkdir -p -- {shlex.quote(parent)}", timeout=30)
            if mkdir.exit_code != 0:
                raise RuntimeError(f"mkdir failed for {path}: {mkdir.stdout}")
        if len(content) > 4 * 1024 * 1024:
            self._write_large_file(sandbox, path=path, content=content)
            return
        sandbox.raw.filesystem.write_bytes(content, path)

    def _write_large_file(
        self, sandbox: SandboxHandle, *, path: str, content: bytes
    ) -> None:
        """Stage bounded uploads and verify exact bytes before replacing a file.

        A provider stream can complete while duplicating a chunk. A tar parser
        is too late to catch this reliably: extraction may already be partial.
        Native app operations are never retried here; this only transfers bytes.
        """
        staging = path + ".upload-" + uuid.uuid4().hex
        chunk_size = 4 * 1024 * 1024
        manifest = []
        try:
            for offset in range(0, len(content), chunk_size):
                chunk = content[offset : offset + chunk_size]
                name = str(offset // chunk_size)
                sandbox.raw.filesystem.write_bytes(chunk, staging + "/" + name)
                manifest.append(
                    {
                        "name": name,
                        "size": len(chunk),
                        "sha256": sha256(chunk).hexdigest(),
                    }
                )
            script = """import hashlib,json,os,pathlib,sys
directory=pathlib.Path(sys.argv[1]); destination=pathlib.Path(sys.argv[2])
manifest=json.loads(sys.argv[3]); temporary=directory/'assembled'
invalid=[]
for row in manifest:
    part=directory/row['name']
    data=part.read_bytes() if part.is_file() else b''
    if len(data)!=row['size'] or hashlib.sha256(data).hexdigest()!=row['sha256']:
        invalid.append(row['name'])
if invalid:
    print(json.dumps({'invalid_chunks':invalid}));sys.exit(3)
with temporary.open('wb') as stream:
    for row in manifest:
        data=(directory/row['name']).read_bytes()
        if len(data)!=row['size'] or hashlib.sha256(data).hexdigest()!=row['sha256']:
            raise RuntimeError('Uploaded chunk content verification failed: '+row['name'])
        stream.write(data)
    stream.flush();os.fsync(stream.fileno())
if hashlib.sha256(temporary.read_bytes()).hexdigest()!=sys.argv[4]:
    raise RuntimeError('Assembled upload content verification failed')
os.replace(temporary,destination)
"""
            command = shlex.join(
                [
                    "python3",
                    "-c",
                    script,
                    staging,
                    path,
                    json.dumps(manifest, separators=(",", ":")),
                    sha256(content).hexdigest(),
                ]
            )
            for attempt in range(3):
                result = self.exec(sandbox, command, timeout=180)
                if not result.exit_code:
                    break
                if result.exit_code != 3 or attempt == 2:
                    raise RuntimeError(
                        f"Large file upload verification failed: {result.stdout}"
                    )
                invalid = json.loads(result.stdout)["invalid_chunks"]
                known = {row["name"] for row in manifest}
                if not invalid or not set(invalid) <= known:
                    raise RuntimeError("Invalid upload verification response")
                # Byte replacement is idempotent. Retry only corrupt staging
                # parts, never an application request or a whole native run.
                for name in invalid:
                    offset = int(name) * chunk_size
                    sandbox.raw.filesystem.write_bytes(
                        content[offset : offset + chunk_size], staging + "/" + name
                    )
        finally:
            self.exec(sandbox, shlex.join(["rm", "-rf", "--", staging]), timeout=30)

    def read_file(self, sandbox: SandboxHandle, *, path: str) -> bytes:
        if not path.startswith("/"):
            raise ValueError(f"sandbox path must be absolute: {path}")
        return sandbox.raw.filesystem.read_bytes(path)

    def open_channel(self, sandbox, *, argv, cwd):
        return _ModalChannel(sandbox.raw.exec(*argv, workdir=cwd, text=False))

    def delete(self, sandbox: SandboxHandle) -> None:
        sandbox.raw.terminate(wait=True)
