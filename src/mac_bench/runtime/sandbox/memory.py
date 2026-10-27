"""In-memory sandbox backend for tests and offline smoke runs.

This is not a security boundary; production artifact tasks should use Daytona.
It still exercises the same create/upload/exec/read/delete contract so gold
tasks never rely on the host working directory as the solver workspace.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
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

_MAX_PROCESS_OUTPUT_BYTES = 20 * 1024 * 1024


@dataclass
class _SandboxFS:
    files: dict[str, bytes] = field(default_factory=dict)
    root: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="evoteam-memory-")))


@dataclass
class _MemoryProcess:
    process: subprocess.Popen[bytes]
    filesystem: _SandboxFS
    output: bytearray = field(default_factory=bytearray)
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)
    input_fd: int | None = None
    killed: bool = False
    kill_signal: str | None = None
    stdin_open: bool = True
    output_truncated: bool = False


class MemorySandboxBackend(SandboxBackend):
    def __init__(self) -> None:
        self._sandboxes: dict[str, _SandboxFS] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.uploaded: list[str] = []
        self.commands: list[str] = []
        self.create_options: list[tuple[str | None, str | None, bool]] = []
        self.snapshots: set[str] = set()
        self._processes: dict[tuple[str, str], _MemoryProcess] = {}

    def create(
        self,
        *,
        name: str,
        snapshot: str | None = None,
        image: str | None = None,
        network_blocked: bool = False,
    ) -> SandboxHandle:
        if image and snapshot:
            raise ValueError("pass only one of image= or snapshot=")
        self.created.append(name)
        self.create_options.append((image, snapshot, network_blocked))
        self._sandboxes[name] = _SandboxFS()
        return SandboxHandle(id=name, provider="fake", raw=self._sandboxes[name])

    def snapshot_exists(self, *, name: str) -> bool:
        return name in self.snapshots

    def create_snapshot(
        self, sandbox: SandboxHandle, *, name: str, timeout: int = 600
    ) -> None:
        del sandbox, timeout
        self.snapshots.add(name)

    def clone(
        self,
        sandbox: SandboxHandle,
        *,
        url: str,
        path: str,
        revision: str | None = None,
    ) -> None:
        self.commands.append(f"clone {url}@{revision} -> {path}")

    def exec(
        self,
        sandbox: SandboxHandle,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandResult:
        py_timeout = timeout
        del env, timeout
        self.commands.append(command)
        started = time.monotonic()
        fs = self._sandboxes[sandbox.id]
        cwd_prefix = (cwd or "").rstrip("/")

        def resolve(path: str) -> str:
            if path.startswith("/"):
                return str(PurePosixPath(path))
            if cwd_prefix:
                return str(PurePosixPath(f"{cwd_prefix}/{path}"))
            return str(PurePosixPath(path))

        stripped = command.strip()
        if stripped.startswith("mkdir -p "):
            # Directories are implicit in the file map.
            return CommandResult(command, 0, "", time.monotonic() - started)
        if "omega.py" in stripped and stripped.split()[0] in {"python", "python3"}:
            return self._exec_python_omega(
                command=command,
                sandbox=sandbox,
                cwd=cwd,
                timeout=py_timeout,
                started=started,
            )
        if stripped.startswith("base64 -w0 -- ") or stripped.startswith("base64 -- "):
            parts = shlex.split(stripped)
            path = resolve(parts[-1])
            content = fs.files.get(path)
            if content is None:
                return CommandResult(command, 1, f"missing {path}", time.monotonic() - started)
            return CommandResult(
                command, 0, base64.b64encode(content).decode("ascii"), time.monotonic() - started
            )
        if "find . -type f -name '*.json'" in stripped or stripped.startswith("find "):
            root = cwd_prefix or "."
            lines: list[str] = []
            for path in sorted(fs.files):
                if root != "." and not path.startswith(root + "/") and path != root:
                    continue
                rel = path[len(root) + 1 :] if root != "." and path.startswith(root + "/") else path
                if not rel.endswith(".json"):
                    continue
                if rel.endswith("event.json") or rel.endswith("metadata.json"):
                    continue
                lines.append(f"./{rel}" if not rel.startswith("./") else rel)
            return CommandResult(command, 0, "\n".join(lines), time.monotonic() - started)
        if stripped.startswith("ls"):
            parts = shlex.split(stripped)
            target = "."
            for part in parts[1:]:
                if part.startswith("-"):
                    continue
                target = part
                break
            root = resolve(target) if target != "." else (cwd_prefix or ".")
            names = set()
            prefix = root.rstrip("/") + "/"
            for path in fs.files:
                if path == root:
                    names.add(PurePosixPath(path).name)
                    continue
                if not path.startswith(prefix):
                    continue
                rel = path[len(prefix) :]
                names.add(rel.split("/", 1)[0])
            return CommandResult(command, 0, "\n".join(sorted(names)), time.monotonic() - started)
        if "pytest" in stripped:
            return CommandResult(command, 0, "3 passed in 0.1s", time.monotonic() - started)
        if stripped.startswith("rg "):
            return self._exec_rg(
                command=command,
                stripped=stripped,
                files=fs.files,
                cwd_prefix=cwd_prefix,
                started=started,
            )
        if stripped == "git diff --binary HEAD":
            return CommandResult(
                command, 0, "diff --git a/src/x.py b/src/x.py\n", time.monotonic() - started
            )
        return CommandResult(command, 0, "", time.monotonic() - started)

    @staticmethod
    def _exec_rg(
        *,
        command: str,
        stripped: str,
        files: Mapping[str, bytes],
        cwd_prefix: str,
        started: float,
    ) -> CommandResult:
        tokens = shlex.split(stripped)
        separator = tokens.index("--") if "--" in tokens else len(tokens)
        options = tokens[1:separator]
        positional = tokens[separator + 1 :]

        def relative_files(path: str) -> list[tuple[str, bytes]]:
            root = str(PurePosixPath(cwd_prefix or ".") / path)
            root = str(PurePosixPath(root))
            selected: list[tuple[str, bytes]] = []
            for absolute, content in files.items():
                if absolute != root and not absolute.startswith(root.rstrip("/") + "/"):
                    continue
                relative = (
                    absolute[len(cwd_prefix) + 1 :]
                    if cwd_prefix and absolute.startswith(cwd_prefix + "/")
                    else absolute
                )
                if any(part.startswith(".") for part in PurePosixPath(relative).parts):
                    continue
                selected.append((relative, content))
            return sorted(selected)

        if "--files" in options:
            path = positional[0] if positional else "."
            output = "\n".join(item for item, _ in relative_files(path))
            return CommandResult(command, 0 if output else 1, output, time.monotonic() - started)

        if len(positional) < 2:
            return CommandResult(command, 2, "invalid rg arguments", time.monotonic() - started)
        query, path = positional[-2:]
        flags = 0 if "--ignore-case" not in options else re.IGNORECASE
        try:
            pattern = re.compile(re.escape(query) if "--fixed-strings" in options else query, flags)
        except re.error as exc:
            return CommandResult(command, 2, str(exc), time.monotonic() - started)
        context_lines = 0
        glob_pattern: str | None = None
        for index, option in enumerate(options):
            if option == "--context" and index + 1 < len(options):
                context_lines = int(options[index + 1])
            if option == "--glob" and index + 1 < len(options):
                glob_pattern = options[index + 1]
        events: list[str] = []
        match_count = 0
        for relative, content in relative_files(path):
            if glob_pattern and not fnmatch.fnmatch(relative, glob_pattern):
                continue
            try:
                lines = content.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError:
                continue
            matched_lines = [index for index, line in enumerate(lines) if pattern.search(line)]
            emitted_context: set[int] = set()
            for line_index in matched_lines:
                for context_index in range(max(0, line_index - context_lines), line_index):
                    if context_index in emitted_context:
                        continue
                    events.append(
                        json.dumps(
                            {
                                "type": "context",
                                "data": {
                                    "path": {"text": relative},
                                    "lines": {"text": lines[context_index]},
                                    "line_number": context_index + 1,
                                },
                            }
                        )
                    )
                    emitted_context.add(context_index)
                match = pattern.search(lines[line_index])
                assert match is not None
                events.append(
                    json.dumps(
                        {
                            "type": "match",
                            "data": {
                                "path": {"text": relative},
                                "lines": {"text": lines[line_index]},
                                "line_number": line_index + 1,
                                "submatches": [{"start": match.start(), "end": match.end()}],
                            },
                        }
                    )
                )
                match_count += 1
                for context_index in range(
                    line_index + 1,
                    min(len(lines), line_index + context_lines + 1),
                ):
                    if context_index in emitted_context or context_index in matched_lines:
                        continue
                    events.append(
                        json.dumps(
                            {
                                "type": "context",
                                "data": {
                                    "path": {"text": relative},
                                    "lines": {"text": lines[context_index]},
                                    "line_number": context_index + 1,
                                },
                            }
                        )
                    )
                    emitted_context.add(context_index)
        return CommandResult(
            command,
            0 if match_count else 1,
            "\n".join(events),
            time.monotonic() - started,
        )

    def _exec_python_omega(
        self,
        *,
        command: str,
        sandbox: SandboxHandle,
        cwd: str | None,
        timeout: int | None,
        started: float,
    ) -> CommandResult:
        fs = self._sandboxes[sandbox.id]
        host_app = fs.root / "app"
        host_app.mkdir(parents=True, exist_ok=True)
        rewritten = command.replace("/app", str(host_app))
        proc = subprocess.run(
            rewritten,
            shell=True,
            cwd=str(host_app if not cwd else (fs.root / cwd.lstrip("/"))),
            capture_output=True,
            text=True,
            timeout=timeout or 60,
        )
        for path in host_app.rglob("*"):
            if path.is_file():
                rel = "/app/" + path.relative_to(host_app).as_posix()
                fs.files[str(PurePosixPath(rel))] = path.read_bytes()
        out = (proc.stdout or "") + (proc.stderr or "")
        return CommandResult(command, int(proc.returncode), out, time.monotonic() - started)

    def write_file(self, sandbox: SandboxHandle, *, path: str, content: bytes) -> None:
        self.uploaded.append(path)
        fs = self._sandboxes[sandbox.id]
        key = str(PurePosixPath(path))
        fs.files[key] = content
        target = fs.root / key.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def replace_file(
        self, sandbox: SandboxHandle, *, path: str, content: bytes
    ) -> None:
        fs = self._sandboxes[sandbox.id]
        key = str(PurePosixPath(path))
        target = fs.root / key.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".evoteam-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            mode = target.stat().st_mode & 0o7777 if target.exists() else 0o644
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        fs.files[key] = content

    def delete_file(self, sandbox: SandboxHandle, *, path: str) -> None:
        key = str(PurePosixPath(path))
        try:
            del self._sandboxes[sandbox.id].files[key]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc
        target = self._sandboxes[sandbox.id].root / key.lstrip("/")
        target.unlink(missing_ok=True)

    def read_file(self, sandbox: SandboxHandle, *, path: str) -> bytes:
        key = str(PurePosixPath(path))
        fs = self._sandboxes[sandbox.id]
        host = fs.root / key.lstrip("/")
        if host.is_file():
            content = host.read_bytes()
            fs.files[key] = content
            return content
        try:
            return fs.files[key]
        except KeyError as exc:
            raise RuntimeError(f"missing sandbox file: {path}") from exc

    def resolve_path(
        self,
        sandbox: SandboxHandle,
        *,
        path: str,
        cwd: str | None = None,
        kind: str = "any",
    ) -> str:
        fs = self._sandboxes[sandbox.id]
        resolved = str(
            PurePosixPath(path)
            if path.startswith("/")
            else PurePosixPath(cwd or ".") / path
        )
        if kind == "write":
            return resolved
        is_file = resolved in fs.files
        prefix = resolved.rstrip("/") + "/"
        is_directory = resolved in {".", str(PurePosixPath(cwd or "."))} or any(
            item.startswith(prefix) for item in fs.files
        )
        if kind == "file" and not is_file:
            if is_directory:
                raise IsADirectoryError(path)
            raise FileNotFoundError(path)
        if kind == "directory" and not is_directory:
            if is_file:
                raise NotADirectoryError(path)
            raise FileNotFoundError(path)
        if kind == "any" and not (is_file or is_directory):
            raise FileNotFoundError(path)
        return resolved

    def set_network_blocked(self, sandbox: SandboxHandle, *, blocked: bool) -> None:
        del sandbox, blocked

    def start_process(
        self,
        sandbox: SandboxHandle,
        *,
        session_id: str,
        command: str,
        cwd: str,
        tty: bool,
    ) -> BackendProcessHandle:
        fs = self._sandboxes[sandbox.id]
        host_cwd = fs.root / cwd.lstrip("/")
        host_cwd.mkdir(parents=True, exist_ok=True)
        master_fd: int | None = None
        slave_fd: int | None = None
        if tty:
            master_fd, slave_fd = os.openpty()
            process = subprocess.Popen(
                ["/bin/bash", "-lc", command],
                cwd=host_cwd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
            )
            os.close(slave_fd)
            source = master_fd
        else:
            process = subprocess.Popen(
                ["/bin/bash", "-lc", command],
                cwd=host_cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            assert process.stdout is not None
            source = process.stdout.fileno()
        state = _MemoryProcess(process=process, filesystem=fs, input_fd=master_fd)
        self._processes[(sandbox.id, session_id)] = state

        def collect() -> None:
            try:
                while True:
                    try:
                        chunk = os.read(source, 65_536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    with state.lock:
                        remaining = _MAX_PROCESS_OUTPUT_BYTES - len(state.output)
                        if remaining > 0:
                            state.output.extend(chunk[:remaining])
                        if len(chunk) > max(0, remaining):
                            state.output_truncated = True
                process.wait()
            finally:
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                self._sync_process_files(fs)
                state.stdin_open = False
                state.done.set()

        threading.Thread(target=collect, name=f"memory-process-{session_id}", daemon=True).start()
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
        assert isinstance(state, _MemoryProcess)
        if wait_ms > 0:
            state.done.wait(wait_ms / 1_000)
        returncode = state.process.poll()
        with state.lock:
            output = bytes(state.output).decode("utf-8", errors="replace")
        if returncode is None:
            status = "running"
            signal_name = None
        elif state.killed:
            status = "killed"
            signal_name = state.kill_signal or (
                "SIGKILL" if returncode == -signal.SIGKILL else "SIGTERM"
            )
        else:
            status = "completed" if returncode == 0 else "failed"
            signal_name = signal.Signals(-returncode).name if returncode < 0 else None
        return BackendProcessSnapshot(
            status,
            output,
            returncode,
            signal_name,
            state.stdin_open,
            state.output_truncated,
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
        assert isinstance(state, _MemoryProcess)
        if state.process.poll() is not None or not state.stdin_open:
            raise BrokenPipeError("process stdin is closed")
        if state.input_fd is not None:
            if data:
                os.write(state.input_fd, data.encode("utf-8"))
            if eof:
                os.write(state.input_fd, b"\x04")
                state.stdin_open = False
        else:
            assert state.process.stdin is not None
            if data:
                state.process.stdin.write(data.encode("utf-8"))
                state.process.stdin.flush()
            if eof:
                state.process.stdin.close()
                state.stdin_open = False

    def kill_process(
        self,
        sandbox: SandboxHandle,
        handle: BackendProcessHandle,
        *,
        grace_ms: int = 1_000,
    ) -> None:
        del sandbox
        state = handle.raw
        assert isinstance(state, _MemoryProcess)
        if state.process.poll() is not None:
            return
        state.killed = True
        process_group = state.process.pid
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return
        state.kill_signal = "SIGTERM"
        deadline = time.monotonic() + grace_ms / 1_000
        while time.monotonic() < deadline:
            state.process.poll()
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        try:
            os.killpg(process_group, signal.SIGKILL)
            state.kill_signal = "SIGKILL"
        except ProcessLookupError:
            return
        try:
            state.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _sync_process_files(fs: _SandboxFS) -> None:
        synchronized: dict[str, bytes] = {}
        for path in fs.root.rglob("*"):
            if path.is_file():
                synchronized[path.relative_to(fs.root).as_posix()] = path.read_bytes()
        fs.files = synchronized

    def delete(self, sandbox: SandboxHandle) -> None:
        for (sandbox_id, _), process in list(self._processes.items()):
            if sandbox_id == sandbox.id and process.process.poll() is None:
                try:
                    os.killpg(process.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.deleted.append(sandbox.id)
        fs = self._sandboxes.pop(sandbox.id, None)
        if fs is not None:
            shutil.rmtree(fs.root, ignore_errors=True)
