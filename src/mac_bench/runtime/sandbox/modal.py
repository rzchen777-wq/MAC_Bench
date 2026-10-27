"""Modal implementation of the provider-neutral sandbox contract.

Default sandboxes are gVisor. Pass ``vm_runtime=True`` to get a real Linux
kernel with dockerd for business application containers.
"""

from __future__ import annotations
from mac_bench.runtime.paths import artifact_root

import io
import json
import os
from hashlib import sha256
import shlex
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping

from mac_bench.runtime.sandbox.base import (
    BackendProcessHandle,
    BackendProcessSnapshot,
    CommandResult,
    SandboxBackend,
    SandboxHandle,
)
from mac_bench.runtime.sandbox.compose_exec import resolve_via_realpath

_MAX_PROCESS_OUTPUT_BYTES = 20 * 1024 * 1024


@dataclass
class _ModalProcess:
    proc: object
    pidfile: str
    output: bytearray = field(default_factory=bytearray)
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)
    stdin_open: bool = True
    killed: bool = False
    truncated: bool = False
    exit_code: int | None = None
    kill_signal: str | None = None

APP_NAME = "mac-bench"





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
        snapshot_directory: str | Path | None = None,
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
        self._snapshot_directory = Path(snapshot_directory) if snapshot_directory is not None else (
            artifact_root() / 'provider/snapshots')

    def create(
        self,
        *,
        name: str,
        snapshot: str | None = None,
        image: str | None = None,
        network_blocked: bool = False,
    ) -> SandboxHandle:
        modal = self._modal
        if snapshot:
            if image is not None:
                raise ValueError('specify a Modal snapshot or an image, not both')
            receipt = self._snapshot_receipt(snapshot)
            if receipt is None:
                raise FileNotFoundError('Modal snapshot receipt not found')
            # from_id is lazy; a missing provider image must fail creation or
            # first interaction, never fall back to an empty base image.
            modal_image = modal.Image.from_id(receipt['image_id'])
        elif image is None:
            modal_image = (
                native_host_image()
                if self._vm_runtime
                else modal.Image.debian_slim()
            )
        elif not isinstance(image, str):
            modal_image = image
        elif image == "mac-bench-native-vm":
            modal_image = native_host_image()
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
            raw = modal.Sandbox.create("/usr/bin/dockerd", "-D", **kwargs)
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
            "echo ready; exit 0; fi; sleep 1; done; "
            "echo 'dockerd not ready' >&2; exit 1".format(n=seconds),
            timeout=seconds + 15,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"dockerd not ready: {result.stdout}")

    def attach(self, sandbox_id: str) -> SandboxHandle:
        """Bind an existing sandbox; never create a replacement on lookup failure."""
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ValueError('Modal sandbox ID required')
        raw = self._modal.Sandbox.from_id(sandbox_id)
        if str(raw.object_id) != sandbox_id:
            raise RuntimeError('Modal sandbox identity mismatch')
        return SandboxHandle(id=sandbox_id, provider='modal', raw=raw)

    def snapshot_exists(self, *, name: str) -> bool:
        # A durable receipt names a real completed capture. The provider may
        # subsequently delete it; restoration still checks through Modal.
        return self._snapshot_receipt(name) is not None

    def _snapshot_path(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError('Modal snapshot name must be nonempty')
        return self._snapshot_directory / (sha256(name.encode()).hexdigest() + '.json')

    def _snapshot_receipt(self, name):
        path = self._snapshot_path(name)
        if not path.exists():
            return None
        receipt = json.loads(path.read_text())
        if (not isinstance(receipt, dict) or set(receipt) != {
                'version', 'name', 'app_name', 'vm_runtime', 'sandbox_id', 'image_id', 'scope'}
                or receipt['version'] != 1 or receipt['name'] != name
                or receipt['app_name'] != self._app_name
                or receipt['vm_runtime'] is not self._vm_runtime
                or not isinstance(receipt['image_id'], str) or not receipt['image_id'].startswith('im-')
                or receipt['scope'] != 'sandbox_root_filesystem'):
            raise ValueError('Modal snapshot receipt or runtime mismatch')
        return receipt

    def create_snapshot(
        self, sandbox: SandboxHandle, *, name: str, timeout: int = 600
    ) -> None:
        """Capture the actual root filesystem, not external mounted volumes.

        Application quiescence and storage coverage are the caller's obligation.
        This is a filesystem template, not a memory/process or DB transaction
        checkpoint. No provider call is made until this method is invoked.
        """
        path = self._snapshot_path(name)
        if path.exists():
            raise FileExistsError('Modal snapshot name already recorded')
        if sandbox.provider != 'modal' or type(timeout) is not int or timeout < 1:
            raise ValueError('invalid Modal snapshot source or timeout')
        image = sandbox.raw.snapshot_filesystem(timeout=timeout, ttl=None)
        image_id = image.object_id
        if not isinstance(image_id, str) or not image_id.startswith('im-'):
            raise RuntimeError('Modal snapshot returned no image identity')
        receipt = {'version': 1, 'name': name, 'app_name': self._app_name,
                   'vm_runtime': self._vm_runtime, 'sandbox_id': sandbox.id,
                   'image_id': image_id, 'scope': 'sandbox_root_filesystem'}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as output:
                json.dump(receipt, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            # Publish without replacing another process's snapshot of this name.
            os.link(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def clone(
        self,
        sandbox: SandboxHandle,
        *,
        url: str,
        path: str,
        revision: str | None = None,
    ) -> None:
        cmd = f"git clone -- {url} {path}"
        if revision:
            cmd = (
                f"git clone {url} {path} && git -C {path} checkout {revision}"
            )
        result = self.exec(sandbox, cmd, timeout=600)
        if result.exit_code != 0:
            raise RuntimeError(result.stdout)

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
            command_file = "/tmp/evoteam-exec-" + uuid.uuid4().hex + ".sh"
            sandbox.raw.filesystem.write_bytes(command.encode("utf-8"), command_file)
            quoted = shlex.quote(command_file)
            dispatch = (f"IFS= read -r -d '' evoteam_shell_program < {quoted} || true; "
                        f"rm -f -- {quoted}; eval \"$evoteam_shell_program\"")
        try:
            process = sandbox.raw.exec("bash", "-lc", dispatch, **exec_kwargs)
            process.wait()
        finally:
            if command_file is not None:
                # Usually already removed before eval. Also clean up if exec
                # failed before the remote shell could start.
                try:
                    sandbox.raw.exec("rm", "-f", "--", command_file,
                                     text=False, timeout=30).wait()
                except Exception:
                    pass
        def decoded(stream):
            value = stream.read() or b""
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        stdout = decoded(process.stdout) + decoded(process.stderr)
        code = 0 if process.returncode is None else int(process.returncode)
        return CommandResult(
            command=command,
            exit_code=code,
            stdout=stdout,
            duration_seconds=time.monotonic() - started,
        )

    def write_file(
        self, sandbox: SandboxHandle, *, path: str, content: bytes
    ) -> None:
        if not path.startswith("/"):
            raise ValueError(f"sandbox path must be absolute: {path}")
        parent = str(PurePosixPath(path).parent)
        if parent not in {"", "/"}:
            mkdir = self.exec(
                sandbox, f"mkdir -p -- {shlex.quote(parent)}", timeout=30
            )
            if mkdir.exit_code != 0:
                raise RuntimeError(f"mkdir failed for {path}: {mkdir.stdout}")
        if len(content) > 4 * 1024 * 1024:
            self._write_large_file(sandbox, path=path, content=content)
            return
        sandbox.raw.filesystem.write_bytes(content, path)

    def write_files(self, sandbox: SandboxHandle, *, files: Mapping[str, bytes]) -> None:
        """Install a verified set of owner files with one provider upload.

        Validate the entire transfer before replacing any destination. This is
        for provisioning a stopped service, not a transaction or a tool write.
        """
        if not files:
            return
        for path in files:
            if (not path.startswith('/') or path=='/' or '..' in PurePosixPath(path).parts
                    or str(PurePosixPath(path))!=path):
                raise ValueError('Batch sandbox paths must be absolute and contained')
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for path, content in sorted(files.items()):
                entry = tarfile.TarInfo(path.lstrip('/'))
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        content = stream.getvalue()
        expected = {path:sha256(value).hexdigest() for path, value in files.items()}
        transfer = '/tmp/mac-bench-files-'+uuid.uuid4().hex+'.tar.gz'
        self.write_file(sandbox, path=transfer, content=content)
        program = '''import hashlib,io,json,os,pathlib,sys,tarfile
source,checksum,expected=sys.argv[1:]
expected=json.loads(expected)
data=pathlib.Path(source).read_bytes()
assert hashlib.sha256(data).hexdigest()==checksum, 'Provisioning transfer checksum differs'
contents={}
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    for member in archive:
        path='/'+member.name
        assert member.isfile() and path in expected and path not in contents, 'Unexpected provisioning member'
        body=archive.extractfile(member).read()
        assert hashlib.sha256(body).hexdigest()==expected[path], 'Provisioning member checksum differs'
        contents[path]=body
assert set(contents)==set(expected), 'Provisioning member missing'
for name,body in contents.items():
    path=pathlib.Path(name)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.mac-bench-install')
    temporary.write_bytes(body)
    os.replace(temporary,path)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==expected[name]
pathlib.Path(source).unlink()
'''
        result = self.exec(sandbox, shlex.join(['python3','-I','-c',program, transfer,
            sha256(content).hexdigest(), json.dumps(expected, separators=(',',':'))]), timeout=150)
        if result.exit_code:
            raise RuntimeError('Batch sandbox file installation failed')

    def _write_large_file(self, sandbox: SandboxHandle, *, path: str, content: bytes) -> None:
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
                chunk = content[offset:offset + chunk_size]
                name = str(offset // chunk_size)
                sandbox.raw.filesystem.write_bytes(chunk, staging + "/" + name)
                manifest.append({"name": name, "size": len(chunk), "sha256": sha256(chunk).hexdigest()})
            script = '''import hashlib,json,os,pathlib,sys
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
'''
            command = shlex.join([
                'python3', '-c', script, staging, path,
                json.dumps(manifest, separators=(',', ':')), sha256(content).hexdigest(),
            ])
            for attempt in range(3):
                result = self.exec(sandbox, command, timeout=180)
                if not result.exit_code:
                    break
                if result.exit_code != 3 or attempt == 2:
                    raise RuntimeError(f"Large file upload verification failed: {result.stdout}")
                invalid = json.loads(result.stdout)['invalid_chunks']
                known = {row['name'] for row in manifest}
                if not invalid or not set(invalid) <= known:
                    raise RuntimeError('Invalid upload verification response')
                # Byte replacement is idempotent. Retry only corrupt staging
                # parts, never an application request or a whole native run.
                for name in invalid:
                    offset = int(name) * chunk_size
                    sandbox.raw.filesystem.write_bytes(content[offset:offset + chunk_size], staging + '/' + name)
        finally:
            self.exec(sandbox, shlex.join(['rm', '-rf', '--', staging]), timeout=30)

    def read_file(self, sandbox: SandboxHandle, *, path: str) -> bytes:
        if not path.startswith("/"):
            raise ValueError(f"sandbox path must be absolute: {path}")
        return sandbox.raw.filesystem.read_bytes(path)

    def delete_file(self, sandbox: SandboxHandle, *, path: str) -> None:
        result = self.exec(sandbox, f"rm -f -- {shlex.quote(path)}", timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(f"file deletion failed: {result.stdout}")

    def resolve_path(
        self,
        sandbox: SandboxHandle,
        *,
        path: str,
        cwd: str | None = None,
        kind: str = "any",
    ) -> str:
        return resolve_via_realpath(self, sandbox, path=path, cwd=cwd, kind=kind)

    def start_process(
        self,
        sandbox: SandboxHandle,
        *,
        session_id: str,
        command: str,
        cwd: str,
        tty: bool,
    ) -> BackendProcessHandle:
        pidfile = f"/tmp/evoteam-process-{session_id}.pid"
        script = (
            f"printf '%s\\n' $$ > {shlex.quote(pidfile)}; "
            f"exec bash -lc {shlex.quote(command)}"
        )
        exec_kwargs: dict = {}
        if cwd:
            exec_kwargs["workdir"] = cwd
        if tty:
            exec_kwargs["pty"] = True
        proc = sandbox.raw.exec("bash", "-lc", script, **exec_kwargs)
        state = _ModalProcess(proc=proc, pidfile=pidfile)

        def collect_stream(stream) -> None:
            try:
                for chunk in stream:
                    data = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                    with state.lock:
                        remaining = _MAX_PROCESS_OUTPUT_BYTES - len(state.output)
                        if remaining <= 0:
                            state.truncated = True
                            continue
                        if len(data) > remaining:
                            state.output.extend(data[:remaining])
                            state.truncated = True
                        else:
                            state.output.extend(data)
            except Exception:
                pass

        def collect() -> None:
            threads = [
                threading.Thread(
                    target=collect_stream,
                    args=(proc.stdout,),
                    name=f"modal-stdout-{session_id}",
                    daemon=True,
                )
            ]
            if not tty:
                threads.append(
                    threading.Thread(
                        target=collect_stream,
                        args=(proc.stderr,),
                        name=f"modal-stderr-{session_id}",
                        daemon=True,
                    )
                )
            for thread in threads:
                thread.start()
            try:
                code = proc.poll()
                if code is None:
                    code = proc.wait()
            except Exception:
                code = -1
            for thread in threads:
                thread.join(timeout=15)
            state.exit_code = 0 if code is None else int(code)
            state.stdin_open = False
            state.done.set()

        threading.Thread(
            target=collect, name=f"modal-process-{session_id}", daemon=True
        ).start()
        return BackendProcessHandle(session_id, state)

    def poll_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        wait_ms: int = 0,
    ) -> BackendProcessSnapshot:
        del sandbox
        state = handle.raw
        assert isinstance(state, _ModalProcess)
        if wait_ms:
            state.done.wait(wait_ms / 1_000)
        with state.lock:
            output = bytes(state.output).decode("utf-8", errors="replace")
            truncated = state.truncated
        if state.killed and (state.done.is_set() or state.kill_signal):
            status = "killed" if state.done.is_set() else "running"
            return BackendProcessSnapshot(
                status,
                output,
                state.exit_code,
                state.kill_signal or "SIGTERM",
                False,
                truncated,
            )
        if not state.done.is_set():
            return BackendProcessSnapshot(
                "running", output, None, None, state.stdin_open, truncated
            )
        status = "completed" if (state.exit_code or 0) == 0 else "failed"
        return BackendProcessSnapshot(
            status, output, state.exit_code, None, False, truncated
        )

    def write_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        data: str,
        eof: bool,
    ) -> None:
        del sandbox
        state = handle.raw
        assert isinstance(state, _ModalProcess)
        stdin = state.proc.stdin
        if data:
            stdin.write(data)
            drain = getattr(stdin, "drain", None)
            if callable(drain):
                drain()
        if eof:
            write_eof = getattr(stdin, "write_eof", None)
            if callable(write_eof):
                write_eof()
            state.stdin_open = False

    def kill_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        grace_ms: int = 1_000,
    ) -> None:
        state = handle.raw
        assert isinstance(state, _ModalProcess)
        state.killed = True
        state.kill_signal = "SIGTERM"
        pidfile = shlex.quote(state.pidfile)
        self.exec(
            sandbox,
            f"pid=$(cat {pidfile} 2>/dev/null || true); "
            "if [ -n \"$pid\" ]; then kill \"$pid\" 2>/dev/null || true; fi",
            timeout=15,
        )
        state.done.wait(max(0.2, grace_ms / 1_000))
        if not state.done.is_set():
            state.kill_signal = "SIGKILL"
            self.exec(
                sandbox,
                f"pid=$(cat {pidfile} 2>/dev/null || true); "
                "if [ -n \"$pid\" ]; then kill -9 \"$pid\" 2>/dev/null || true; fi",
                timeout=15,
            )
            state.done.wait(2)

    def upload_tree(
        self, sandbox: SandboxHandle, *, local_dir: Path, remote_dir: str
    ) -> None:
        """Upload a local directory as a gzipped tar and extract it."""

        local_dir = local_dir.resolve()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(local_dir, arcname=".")
        payload = buf.getvalue()
        archive = "/tmp/evoteam-upload.tar.gz"
        self.write_file(sandbox, path=archive, content=payload)
        result = self.exec(
            sandbox,
            f"mkdir -p {remote_dir} && tar -xzf {archive} -C {remote_dir} && rm -f {archive}",
            timeout=600,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"upload_tree failed: {result.stdout}")

    def set_network_blocked(
        self, sandbox: SandboxHandle, *, blocked: bool
    ) -> None:
        del sandbox, blocked
        raise NotImplementedError(
            "Modal network policy must be set at create() "
            "(block_network / outbound allowlists)"
        )

    def delete(self, sandbox: SandboxHandle) -> None:
        try:
            sandbox.raw.terminate(wait=True)
        except TypeError:
            sandbox.raw.terminate()
