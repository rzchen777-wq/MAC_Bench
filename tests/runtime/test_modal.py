"""Modal command transport and integrity-checked file transfers."""
from io import BytesIO
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import pytest
from mac_bench.runtime.sandbox.modal import ModalSandboxBackend

def test_binary_stdout_and_stderr_preserve_exit_status_and_valid_text():
    seen = {}
    process = SimpleNamespace(stdout=BytesIO(b'prefix\xff\xfe\n' + '中文'.encode()),
                              stderr=BytesIO(b'error\x80'), returncode=7, wait=lambda: 7)
    def execute(*args, **kwargs):
        seen.update(kwargs)
        assert kwargs['text'] is False
        return process
    backend = ModalSandboxBackend.__new__(ModalSandboxBackend)
    result = backend.exec(SimpleNamespace(raw=SimpleNamespace(exec=execute)), 'emit-binary',
                          cwd='/work/project', env={'FOO': 'bar'}, timeout=12)
    assert result.stdout == 'prefix\ufffd\ufffd\n中文error\ufffd'
    assert result.exit_code == 7
    assert seen['workdir'] == '/work/project' and seen['timeout'] == 12
    assert seen['env'] == {'FOO': 'bar'}


def test_large_utf8_command_uses_file_and_preserves_shell_result(tmp_path):
    backend, sandbox, uploads, calls = local_backend()
    # Bytes, not character count, determines the provider limit. The heredoc,
    # backslash, trailing newlines and incoming shell status must survive.
    command = ('#' + '中' * 24000 + '\n'
               'printf "status=%s env=%s\\n" "$?" "$EXEC_TEST_VALUE"\n'
               "cat > result.txt <<'DATA'\nquote ' and backslash \\\nDATA\n"
               "printf '\\377'; printf 'error' >&2; exit 7\n\n")
    result = backend.exec(sandbox, command, cwd=str(tmp_path),
                          env={'EXEC_TEST_VALUE': 'present'}, timeout=12)
    assert result.exit_code == 7 and result.command == command
    assert result.stdout == 'status=0 env=present\n\ufffderror'
    assert (tmp_path / 'result.txt').read_text() == "quote ' and backslash \\\n"
    assert len(uploads) == 1 and not Path(uploads[0]).exists()
    assert calls[0][1]['workdir'] == str(tmp_path) and calls[0][1]['timeout'] == 12


def test_large_command_upload_is_removed_if_exec_cannot_start():
    uploaded = []

    def write_bytes(content, path):
        uploaded.append(path)
        Path(path).write_bytes(content)

    def execute(*args, **kwargs):
        if args[0] == 'bash':
            raise RuntimeError('provider start failed')
        Path(args[-1]).unlink(missing_ok=True)
        return SimpleNamespace(wait=lambda: 0)

    backend = ModalSandboxBackend.__new__(ModalSandboxBackend)
    raw = SimpleNamespace(exec=execute, filesystem=SimpleNamespace(write_bytes=write_bytes))
    with pytest.raises(RuntimeError, match='provider start failed'):
        backend.exec(SimpleNamespace(raw=raw), '#' * 70000)
    assert uploaded and not Path(uploaded[0]).exists()


def backend(*, corrupt=False):
    uploaded=[]
    def write_file(sandbox, *, path, content):
        uploaded.append(path)
        Path(path).write_bytes(content+b'corrupt' if corrupt else content)
    def execute(sandbox, command, *, timeout):
        result=subprocess.run(command,shell=True,capture_output=True,text=True,timeout=timeout)
        return SimpleNamespace(exit_code=result.returncode)
    return SimpleNamespace(write_file=write_file,exec=execute),uploaded


def test_many_runtime_modules_use_one_integrity_checked_upload(tmp_path):
    host,uploads=backend()
    files={str(tmp_path/'runtime/tools/query.py'):b'query',
           str(tmp_path/'runtime/tools/cards.py'):b'cards',
           str(tmp_path/'public/record_forms.json'):b'{}'}
    ModalSandboxBackend.write_files(host,None,files=files)
    assert len(uploads)==1 and not Path(uploads[0]).exists()
    assert {path:Path(path).read_bytes() for path in files}==files


def test_corrupt_batch_does_not_partially_replace_existing_runtime(tmp_path):
    host,uploads=backend(corrupt=True)
    original=tmp_path/'query.py';original.write_bytes(b'old')
    missing=tmp_path/'not-created.py'
    try:
        with pytest.raises(RuntimeError,match='installation failed'):
            ModalSandboxBackend.write_files(host,None,files={str(original):b'new',str(missing):b'new'})
        assert original.read_bytes()==b'old' and not missing.exists()
    finally:
        for path in uploads:Path(path).unlink(missing_ok=True)


def test_invalid_batch_path_is_rejected_before_provider_access():
    host,uploads=backend()
    for path in ['relative.py','/tmp/../etc/escape','/tmp/a//b.py','/tmp/a/./b.py','/']:
        with pytest.raises(ValueError):
            ModalSandboxBackend.write_files(host,None,files={path:b'bad'})
    assert not uploads


def local_backend(*, corrupt=False, fail=False, corrupt_once=False):
    writes, calls = [], []

    def write_bytes(content, path):
        writes.append(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if fail:
            raise OSError('Provider transfer failed')
        damaged = corrupt or (corrupt_once and len(writes) == 2)
        Path(path).write_bytes(content + (b'extra duplicate chunk' if damaged else b''))

    def execute(*args, **kwargs):
        assert sum(len(a.encode()) for a in args) < 65536
        calls.append((args, kwargs))
        result = subprocess.run(args, cwd=kwargs.get('workdir'),
            env={**os.environ, **kwargs.get('env', {})},
            capture_output=True, timeout=kwargs.get('timeout'))
        return SimpleNamespace(stdout=BytesIO(result.stdout), stderr=BytesIO(result.stderr),
                               returncode=result.returncode, wait=lambda: result.returncode)

    backend = ModalSandboxBackend.__new__(ModalSandboxBackend)
    sandbox = SimpleNamespace(raw=SimpleNamespace(
        filesystem=SimpleNamespace(write_bytes=write_bytes), exec=execute))
    return backend, sandbox, writes, calls


def test_large_binary_upload_preserves_exact_bytes_and_shell_metacharacters(tmp_path):
    backend, sandbox, writes, _ = local_backend()
    target = tmp_path / "a ' $() 中文.bin"
    data = bytes(range(256)) * 40000
    target.write_bytes(b'previous valid file')
    backend.write_file(sandbox, path=str(target), content=data)
    assert target.read_bytes() == data
    assert len(writes) == 3
    assert not list(tmp_path.glob('*.upload-*'))


def test_only_corrupt_staging_chunk_is_retransmitted(tmp_path):
    backend, sandbox, writes, _ = local_backend(corrupt_once=True)
    target = tmp_path / 'payload.tar.gz'
    data = bytes(range(256)) * 40000
    backend.write_file(sandbox, path=str(target), content=data)
    assert target.read_bytes() == data
    assert [Path(p).name for p in writes] == ['0', '1', '2', '1']
    assert not list(tmp_path.glob('*.upload-*'))


@pytest.mark.parametrize('mode', ['corrupt', 'fail'])
def test_failed_upload_retains_previous_file_and_cleans_staging(tmp_path, mode):
    backend, sandbox, _, _ = local_backend(**{mode: True})
    target = tmp_path / 'payload.tar.gz'
    target.write_bytes(b'previous valid file')
    with pytest.raises((RuntimeError, OSError)):
        backend.write_file(sandbox, path=str(target), content=b'x' * (4 * 1024 * 1024 + 1))
    assert target.read_bytes() == b'previous valid file'
    assert not list(tmp_path.glob('*.upload-*'))
