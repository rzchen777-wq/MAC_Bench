"""Daytona sandboxes with prepared images and private byte channels."""

from __future__ import annotations

import math
import os
from pathlib import Path, PurePosixPath
import queue
import shlex
import threading
import time
import uuid

from .base import (
    shell_from_file,
    CommandResult,
    SandboxBackend,
    SandboxHandle,
    PreparedImage,
)

DOCKER_IMAGE = "docker:28.3.3-dind"

_BOOT = r"""set -eu
nohup dockerd --storage-driver=overlay2 --host=unix:///var/run/docker.sock > /work/dockerd.log 2>&1 </dev/null &
for i in $(seq 1 90); do
    if timeout 3 docker info >/dev/null 2>&1; then
        test "$(docker info --format '{{.Driver}}')" = overlay2
        exit 0
    fi
    sleep 1
done
tail -c 12000 /work/dockerd.log
exit 1
"""
_STOP = r"""set -eu
test -z "$(docker ps -aq)"
test -z "$(docker image ls -q)"
kill -TERM "$(cat /var/run/docker.pid)"
for i in $(seq 1 90); do
    if ! pgrep -x dockerd >/dev/null; then break; fi
    sleep 1
done
! pgrep -x dockerd >/dev/null
sync
"""


class _DaytonaChannel:
    """A raw PTY with an explicit ready boundary; shell output never enters RPC."""

    def __init__(self, pty, command, marker):
        self.pty = pty
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.output = queue.Queue(maxsize=64)
        self.error = None
        self.marker = marker
        try:
            pty.wait_for_connection(timeout=20)
            self.reader = threading.Thread(target=self._receive, daemon=True)
            self.reader.start()
            pty.send_input(command.encode() + b"\n")
            if not self.ready.wait(30) or self.error:
                raise RuntimeError(
                    "Daytona channel did not become ready"
                ) from self.error
        except BaseException:
            self.close()
            raise

    def _put(self, item):
        while not self.closed.is_set():
            try:
                self.output.put(item, timeout=0.2)
                return
            except queue.Full:
                pass

    def _receive(self):
        startup = b""
        try:
            for chunk in self.pty:
                if not self.ready.is_set():
                    startup = (startup + chunk)[-65536:]
                    if self.marker not in startup:
                        continue
                    _, chunk = startup.split(self.marker, 1)
                    self.ready.set()
                if chunk:
                    self._put(chunk)
            if not self.ready.is_set():
                raise EOFError("Daytona channel closed during startup")
        except Exception as exc:
            self.error = exc
        finally:
            self.ready.set()
            self._put(None)

    def write(self, data):
        if self.closed.is_set():
            raise EOFError("Daytona channel closed")
        # Bounded websocket frames also avoid large terminal input writes.
        for offset in range(0, len(data), 16384):
            self.pty.send_input(data[offset : offset + 16384])

    def read(self):
        while not self.closed.is_set():
            try:
                chunk = self.output.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                if self.error:
                    raise self.error
                return
            yield chunk

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.pty.kill()
        except Exception as error:
            from daytona import DaytonaNotFoundError

            # The SDK wraps the typed API error when a process already exited.
            cause = error
            while cause is not None:
                if isinstance(cause, DaytonaNotFoundError):
                    break
                cause = cause.__cause__
            else:
                raise
        finally:
            self.pty.disconnect()


class DaytonaSandboxBackend(SandboxBackend):
    provider = "daytona"

    def __init__(
        self,
        *,
        docker=False,
        cpu=4,
        memory_mib=8192,
        disk_gib=10,
        idle_timeout=7200,
        endpoint="https://app.daytona.io/api",
        target="us",
    ):
        from daytona import Daytona, DaytonaConfig

        if (
            cpu < 1
            or int(cpu) != cpu
            or memory_mib < 1024
            or memory_mib % 1024
            or disk_gib < 1
        ):
            raise ValueError(
                "Daytona requires whole CPU cores and GiB resource allocations"
            )
        self._docker = docker
        self._networks = {}
        self._cpu, self._memory, self._disk = int(cpu), memory_mib // 1024, disk_gib
        self._idle_timeout = idle_timeout
        self._region = target
        self._url = endpoint
        from .providers import BackendConfig

        self.config = BackendConfig(
            "daytona",
            "business" if docker else "software",
            cpu,
            memory_mib,
            disk_gib,
            7200,
            endpoint,
            target,
            idle_timeout_seconds=idle_timeout,
        )
        key = os.environ.get("DAYTONA_API_KEY")
        if not key:
            raise ValueError("DAYTONA_API_KEY is required")
        self._client = Daytona(
            DaytonaConfig(api_key=key, api_url=self._url, target=self._region)
        )

    def business_image(self, common, task):
        from daytona import Image

        image = Image.base(DOCKER_IMAGE).run_commands(
            "apk add --no-cache python3 py3-pip py3-psycopg2 bash curl tar gzip procps iproute2 skopeo",
            "python3 -m pip install --break-system-packages requests PyYAML jsonschema graphql-core openapi-schema-validator",
            "mkdir -p /work",
        )
        # Keep shared OCI blobs as separate cached build inputs instead of one
        # multi-gigabyte archive. All files retain their original OCI paths.
        common = Path(common)
        for source in sorted(common.rglob("*")):
            if source.is_file():
                destination = "/work/environment/images/" + source.relative_to(common).as_posix()
                image = image.add_local_file(source, destination)
        return image.add_local_dir(task, "/work/environment/images")

    @property
    def image_scope(self):
        return {
            "provider": self.provider,
            "endpoint": self._url,
            "target": self._region,
            "docker": self._docker,
            "cpu": self._cpu,
            "memory_mib": self._memory * 1024,
            "disk_gib": self._disk,
        }

    def image_exists(self, image):
        self.validate_image(image)
        from daytona import DaytonaNotFoundError

        try:
            snapshot = self._client.snapshot.get(image.reference)
        except DaytonaNotFoundError:
            return False
        if snapshot.state not in {"active", "inactive"}:
            raise RuntimeError("Daytona image is not ready: " + str(snapshot.state))
        return True

    def create(self, *, name, prepared=None, image=None, network_blocked=False):
        from daytona import (
            CreateSandboxFromImageParams,
            CreateSandboxFromSnapshotParams,
            Resources,
        )

        if prepared and image is not None:
            raise ValueError("Specify a prepared image or a base image, not both")
        settings = dict(
            name=name + "-" + uuid.uuid4().hex[:12],
            network_block_all=network_blocked,
            auto_stop_interval=math.ceil(self._idle_timeout / 60),
            auto_delete_interval=60,
            labels={"application": "mac-bench"},
        )
        if prepared:
            self.validate_image(prepared)
            params = CreateSandboxFromSnapshotParams(
                snapshot=prepared.reference, **settings
            )
        else:
            params = CreateSandboxFromImageParams(
                image=image or "python:3.12-slim",
                resources=Resources(
                    cpu=self._cpu, memory=self._memory, disk=self._disk
                ),
                **settings,
            )
        try:
            raw = self._client.create(params, timeout=1200)
        except BaseException:
            from daytona import DaytonaNotFoundError

            # A timeout can occur after allocation, before the SDK returns a handle.
            try:
                self._client.get(settings["name"]).delete(timeout=120)
            except DaytonaNotFoundError:
                pass
            raise
        handle = SandboxHandle(id=raw.id, provider="daytona", raw=raw)
        try:
            if self._docker:
                self._checked(handle, _BOOT, 300)
            elif network_blocked:
                self._isolate_network(handle)
            return handle
        except BaseException:
            self.delete(handle)
            raise

    def _isolate_network(self, sandbox):
        from .network import START_NETWORK

        if sandbox.id in self._networks:
            return
        pid = self._checked(
            sandbox, shlex.join(["python3", "-I", "-c", START_NETWORK]), 30
        )
        self._networks[sandbox.id] = int(pid.strip())

    def _network_entry(self, sandbox):
        from .network import enter_network

        pid = self._networks.get(sandbox.id)
        return enter_network(pid) if pid else ""

    def _checked(self, sandbox, command, timeout):
        result = self.exec(sandbox, command, timeout=timeout)
        if result.exit_code:
            raise RuntimeError("Daytona provisioning failed: " + result.stdout[-12000:])
        return result.stdout

    def capture_business_image(self, sandbox, *, recipe, timeout=600):
        # Capture compact OCI files while the Docker store is empty.
        return self.capture_image(sandbox, recipe=recipe, timeout=timeout)

    def capture_image(self, sandbox, *, recipe, timeout=600):
        if sandbox.provider != self.provider or timeout < 1:
            raise ValueError("Invalid Daytona image source or timeout")
        if sandbox.id in self._networks:
            raise ValueError(
                "Capture dependencies before opening an offline task workspace"
            )
        name = "mac-bench-" + recipe[:32] + "-" + uuid.uuid4().hex[:12]
        if self._docker:
            self._checked(sandbox, _STOP, timeout)
        else:
            self._checked(sandbox, "sync", 30)
        sandbox.raw.stop(timeout=120)
        sandbox.raw._experimental_create_snapshot(name, timeout=timeout)
        sandbox.raw.start(timeout=120)
        if self._docker:
            self._checked(sandbox, _BOOT, 300)
        return PreparedImage(self.provider, name, recipe, self.image_scope)

    def exec(self, sandbox, command, *, cwd=None, env=None, timeout=None):
        started = time.monotonic()
        # Upload large scripts to avoid API command-size and shell argument limits.
        if len(command.encode()) >= 32768:
            path = "/tmp/mac-bench-exec-" + uuid.uuid4().hex + ".sh"
            self.write_file(sandbox, path=path, content=command.encode())
            dispatch = shlex.join(["bash", "-lc", shell_from_file(path)])
        else:
            dispatch = shlex.join(["bash", "-lc", command])
        if entry := self._network_entry(sandbox):
            dispatch = shlex.join(
                [
                    "python3",
                    "-I",
                    "-c",
                    entry + "import sys; os.execvp('sh',['sh','-c',sys.argv[1]])",
                    dispatch,
                ]
            )
        result = sandbox.raw.process.exec(
            dispatch, cwd=cwd, env=dict(env or {}), timeout=timeout or 300
        )
        return CommandResult(
            command, result.exit_code, str(result.result), time.monotonic() - started
        )

    def write_file(self, sandbox, *, path, content):
        self._parent(sandbox, path)
        sandbox.raw.fs.upload_file(content, path)

    def _parent(self, sandbox, path):
        parsed = PurePosixPath(path)
        if not parsed.is_absolute() or ".." in parsed.parts:
            raise ValueError("Sandbox path must be absolute and contained")
        self._checked(sandbox, shlex.join(["mkdir", "-p", str(parsed.parent)]), 30)

    def upload_file(self, sandbox, *, source, path):
        self._parent(sandbox, path)
        sandbox.raw.fs.upload_file(str(source), path)

    def read_file(self, sandbox, *, path):
        return sandbox.raw.fs.download_file(path)

    def open_channel(self, sandbox, *, argv, cwd):
        identity = uuid.uuid4().hex
        marker = ("mac-bench-ready-" + identity + "\n").encode()
        path = "/tmp/mac-bench-channel-" + identity + ".py"
        program = (
            self._network_entry(sandbox) + "import os,tty\n"
            f"os.unlink({path!r})\n"
            f"os.dup2(os.open({('/tmp/mac-bench-channel-' + identity + '.log')!r},os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),2)\n"
            "tty.setraw(0)\n"
            f"os.write(1,{marker!r})\n"
            f"os.execvp({argv[0]!r},{list(argv)!r})\n"
        )
        self.write_file(sandbox, path=path, content=program.encode())
        pty = sandbox.raw.process.create_pty_session(id="mb-" + identity, cwd=cwd)
        return _DaytonaChannel(
            pty, shlex.join(["exec", "python3", "-I", "-u", path]), marker
        )

    def delete(self, sandbox):
        sandbox.raw.delete(timeout=120)
        self._networks.pop(sandbox.id, None)
