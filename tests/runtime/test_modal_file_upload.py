"""Large uploads must not replace a valid file with silently corrupted bytes."""
from io import BytesIO
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from mac_bench.runtime.sandbox.modal import ModalSandboxBackend


def local_backend(*, corrupt=False, fail=False, corrupt_once=False):
    writes = []

    def write_bytes(content, path):
        writes.append(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if fail:
            raise OSError('Provider transfer failed')
        damaged = corrupt or (corrupt_once and len(writes) == 2)
        Path(path).write_bytes(content + (b'extra duplicate chunk' if damaged else b''))

    def execute(*args, **kwargs):
        result = subprocess.run(args, capture_output=True, timeout=kwargs.get('timeout'))
        return SimpleNamespace(stdout=BytesIO(result.stdout), stderr=BytesIO(result.stderr),
                               returncode=result.returncode, wait=lambda: result.returncode)

    backend = ModalSandboxBackend.__new__(ModalSandboxBackend)
    sandbox = SimpleNamespace(raw=SimpleNamespace(
        filesystem=SimpleNamespace(write_bytes=write_bytes), exec=execute))
    return backend, sandbox, writes


def test_large_binary_upload_preserves_exact_bytes_and_shell_metacharacters(tmp_path):
    backend, sandbox, writes = local_backend()
    target = tmp_path / "a ' $() 中文.bin"
    data = bytes(range(256)) * 40000
    target.write_bytes(b'previous valid file')
    backend.write_file(sandbox, path=str(target), content=data)
    assert target.read_bytes() == data
    assert len(writes) == 3
    assert not list(tmp_path.glob('*.upload-*'))


def test_only_corrupt_staging_chunk_is_retransmitted(tmp_path):
    backend, sandbox, writes = local_backend(corrupt_once=True)
    target = tmp_path / 'payload.tar.gz'
    data = bytes(range(256)) * 40000
    backend.write_file(sandbox, path=str(target), content=data)
    assert target.read_bytes() == data
    assert [Path(p).name for p in writes] == ['0', '1', '2', '1']
    assert not list(tmp_path.glob('*.upload-*'))


@pytest.mark.parametrize('mode', ['corrupt', 'fail'])
def test_failed_upload_retains_previous_file_and_cleans_staging(tmp_path, mode):
    backend, sandbox, _ = local_backend(**{mode: True})
    target = tmp_path / 'payload.tar.gz'
    target.write_bytes(b'previous valid file')
    with pytest.raises((RuntimeError, OSError)):
        backend.write_file(sandbox, path=str(target), content=b'x' * (4 * 1024 * 1024 + 1))
    assert target.read_bytes() == b'previous valid file'
    assert not list(tmp_path.glob('*.upload-*'))
