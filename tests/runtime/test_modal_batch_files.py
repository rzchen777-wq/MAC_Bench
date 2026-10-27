"""Verify batch provisioning completely before changing any deployed module."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from mac_bench.runtime.sandbox.modal import ModalSandboxBackend


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
