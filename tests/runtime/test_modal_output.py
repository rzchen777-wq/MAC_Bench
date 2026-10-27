"""Malformed terminal bytes remain observations, not fatal adapter errors."""
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
    uploads, calls = [], []

    def write_bytes(content, path):
        uploads.append(path)
        Path(path).write_bytes(content)

    def execute(*args, **kwargs):
        assert sum(len(a.encode()) for a in args) < 65536
        calls.append((args, kwargs))
        done = subprocess.run(args, cwd=kwargs.get('workdir'),
                              env={**os.environ, **kwargs.get('env', {})},
                              capture_output=True, timeout=kwargs.get('timeout'))
        return SimpleNamespace(stdout=BytesIO(done.stdout), stderr=BytesIO(done.stderr),
                               returncode=done.returncode, wait=lambda: done.returncode)

    backend = ModalSandboxBackend.__new__(ModalSandboxBackend)
    raw = SimpleNamespace(exec=execute, filesystem=SimpleNamespace(write_bytes=write_bytes))
    # Bytes, not character count, determines the provider limit. The heredoc,
    # backslash, trailing newlines and incoming shell status must survive.
    command = ('#' + '中' * 24000 + '\n'
               'printf "status=%s env=%s\\n" "$?" "$EXEC_TEST_VALUE"\n'
               "cat > result.txt <<'DATA'\nquote ' and backslash \\\nDATA\n"
               "printf '\\377'; printf 'error' >&2; exit 7\n\n")
    result = backend.exec(SimpleNamespace(raw=raw), command, cwd=str(tmp_path),
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
