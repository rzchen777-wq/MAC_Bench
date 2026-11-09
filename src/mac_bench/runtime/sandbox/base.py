from __future__ import annotations

import io
import json
import shlex
import tarfile
import uuid
from hashlib import sha256
from pathlib import PurePosixPath

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Protocol, Sequence


def shell_from_file(path: str) -> str:
    """Evaluate uploaded command text in bash, removing transport before execution."""
    quoted = shlex.quote(path)
    return (
        f"IFS= read -r -d '' mac_bench_shell_program < {quoted} || true; "
        f'rm -f -- {quoted}; eval "$mac_bench_shell_program"'
    )


@dataclass(frozen=True)
class SandboxHandle:
    """Provider-neutral reference to a live sandbox."""

    id: str
    provider: str
    raw: Any = field(compare=False, repr=False)


@dataclass(frozen=True)
class PreparedImage:
    """An immutable reference to a provider image for one preparation recipe."""

    provider: str
    reference: str
    recipe: str
    scope: dict

    def __post_init__(self):
        if (
            self.provider not in {"modal", "daytona"}
            or not self.reference
            or not self.recipe
        ):
            raise ValueError("Invalid prepared image reference")
        if not isinstance(self.scope, dict):
            raise ValueError("Prepared image scope must be an object")


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    duration_seconds: float


class SandboxChannel(Protocol):
    """A persistent stdin/stdout channel to a trusted program in the sandbox."""

    def write(self, data: bytes) -> None: ...
    def read(self) -> Iterator[bytes]: ...
    def close(self) -> None: ...


class SandboxBackend(ABC):
    """Provider-neutral contract for task environments and execution workers."""

    @abstractmethod
    def create(
        self,
        *,
        name: str,
        prepared: PreparedImage | None = None,
        image: str | None = None,
        network_blocked: bool = False,
    ) -> SandboxHandle:
        """Create from a registry/native base image or a prepared image reference."""
        raise NotImplementedError

    @property
    @abstractmethod
    def image_scope(self) -> dict:
        """Platform and resource scope required to restore this backend's images."""
        raise NotImplementedError

    @abstractmethod
    def image_exists(self, image: PreparedImage) -> bool:
        """Return false only for absent images; surface provider errors and unready images."""
        raise NotImplementedError

    @abstractmethod
    def capture_image(self, sandbox, *, recipe, timeout=600) -> PreparedImage:
        raise NotImplementedError

    @abstractmethod
    def business_image(self, common, task):
        """Build the host image from the shared OCI inputs."""
        raise NotImplementedError

    @abstractmethod
    def capture_business_image(self, sandbox, *, recipe, timeout=600) -> PreparedImage:
        raise NotImplementedError

    @abstractmethod
    def upload_file(self, sandbox, *, source, path):
        raise NotImplementedError

    def validate_image(self, image):
        if image.provider != self.provider or image.scope != self.image_scope:
            raise ValueError("Prepared image does not match the selected backend")

    @abstractmethod
    def exec(
        self,
        sandbox: SandboxHandle,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandResult:
        raise NotImplementedError

    @abstractmethod
    def write_file(self, sandbox: SandboxHandle, *, path: str, content: bytes) -> None:
        raise NotImplementedError

    def write_files(
        self, sandbox: SandboxHandle, *, files: Mapping[str, bytes]
    ) -> None:
        """Install a verified set of owner files with one provider upload.

        Validate the entire transfer before replacing any destination. This is
        for provisioning a stopped service, not a transaction or a tool write.
        """
        if not files:
            return
        for path in files:
            if (
                not path.startswith("/")
                or path == "/"
                or ".." in PurePosixPath(path).parts
                or str(PurePosixPath(path)) != path
            ):
                raise ValueError("Batch sandbox paths must be absolute and contained")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for path, content in sorted(files.items()):
                entry = tarfile.TarInfo(path.lstrip("/"))
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        content = stream.getvalue()
        expected = {path: sha256(value).hexdigest() for path, value in files.items()}
        transfer = "/tmp/mac-bench-files-" + uuid.uuid4().hex + ".tar.gz"
        self.write_file(sandbox, path=transfer, content=content)
        program = """import hashlib,io,json,os,pathlib,sys,tarfile
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
"""
        result = self.exec(
            sandbox,
            shlex.join(
                [
                    "python3",
                    "-I",
                    "-c",
                    program,
                    transfer,
                    sha256(content).hexdigest(),
                    json.dumps(expected, separators=(",", ":")),
                ]
            ),
            timeout=150,
        )
        if result.exit_code:
            raise RuntimeError("Batch sandbox file installation failed")

    def upload_tree(self, sandbox, *, local_dir, remote_dir):
        """Transfer a directory once, retaining executable modes and subdirectories."""
        from pathlib import Path

        remote = PurePosixPath(remote_dir)
        if not remote.is_absolute() or ".." in remote.parts:
            raise ValueError("Upload destination must be absolute and contained")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            archive.add(Path(local_dir).resolve(), arcname=".")
        transfer = "/tmp/mac-bench-tree-" + uuid.uuid4().hex + ".tar.gz"
        self.write_file(sandbox, path=transfer, content=stream.getvalue())
        command = (
            shlex.join(["mkdir", "-p", remote_dir])
            + " && "
            + shlex.join(["tar", "-xzf", transfer, "-C", remote_dir])
            + " && "
            + shlex.join(["rm", "-f", transfer])
        )
        result = self.exec(sandbox, command, timeout=600)
        if result.exit_code:
            raise RuntimeError("Directory upload failed: " + result.stdout)

    @abstractmethod
    def read_file(self, sandbox: SandboxHandle, *, path: str) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def open_channel(
        self, sandbox: SandboxHandle, *, argv: Sequence[str], cwd: str
    ) -> SandboxChannel:
        """Start one persistent capability worker, separate from agent commands."""
        raise NotImplementedError(
            "sandbox backend does not support persistent channels"
        )

    @abstractmethod
    def delete(self, sandbox: SandboxHandle) -> None:
        raise NotImplementedError
