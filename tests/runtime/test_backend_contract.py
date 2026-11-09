"""Both SDK adapters preserve the same task command and lifecycle semantics."""

import io
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
import pytest
from mac_bench.runtime.sandbox.base import SandboxHandle
from mac_bench.runtime.sandbox.modal import ModalSandboxBackend
from mac_bench.runtime.sandbox.daytona import DaytonaSandboxBackend


@pytest.fixture(params=["modal", "daytona"])
def local_adapter(request):
    uploads = []

    def write(content, path):
        uploads.append(Path(path))
        Path(path).write_bytes(content)

    def run(argv, cwd, env, timeout):
        return subprocess.run(
            argv,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            timeout=timeout or 30,
            capture_output=True,
        )

    if request.param == "modal":
        backend = ModalSandboxBackend.__new__(ModalSandboxBackend)

        def execute(*argv, **kwargs):
            result = run(
                argv, kwargs.get("workdir"), kwargs.get("env"), kwargs.get("timeout")
            )
            return SimpleNamespace(
                wait=lambda: None,
                returncode=result.returncode,
                stdout=io.BytesIO(result.stdout),
                stderr=io.BytesIO(result.stderr),
            )

        raw = SimpleNamespace(
            exec=execute, filesystem=SimpleNamespace(write_bytes=write)
        )
    else:
        backend = DaytonaSandboxBackend.__new__(DaytonaSandboxBackend)
        backend._networks = {}

        def execute(command, **kwargs):
            result = run(
                shlex.split(command),
                kwargs.get("cwd"),
                kwargs.get("env"),
                kwargs.get("timeout"),
            )
            return SimpleNamespace(
                exit_code=result.returncode,
                result=(result.stdout + result.stderr).decode(),
            )

        raw = SimpleNamespace(process=SimpleNamespace(exec=execute))
        backend.write_file = lambda sandbox, *, path, content: write(content, path)
    try:
        yield backend, SandboxHandle("fixture", request.param, raw), uploads
    finally:
        for path in uploads:
            path.unlink(missing_ok=True)


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("ending", ["exit 7", "set -e; false", "true"])
def test_shell_cwd_env_output_exit_and_transport_cleanup(
    local_adapter, tmp_path, large, ending
):
    backend, sandbox, uploads = local_adapter
    command = (
        'pwd; printf "%s\\n" "$MAC_CONTRACT_VALUE"; '
        'values=(alpha beta); printf "%s\\n" "${values[1]}"; echo stderr-output >&2;\n'
    )
    command += ("# padding " + ("x" * 70000) + "\n") if large else ""
    command += ending
    result = backend.exec(
        sandbox,
        command,
        cwd=str(tmp_path),
        env={"MAC_CONTRACT_VALUE": "with spaces 中文"},
        timeout=30,
    )
    assert result.command == command
    assert result.exit_code == ({"exit 7": 7, "set -e; false": 1, "true": 0}[ending])
    assert str(tmp_path) in result.stdout
    assert (
        "with spaces 中文\nbeta" in result.stdout and "stderr-output" in result.stdout
    )
    assert bool(uploads) == large
    assert all(not path.exists() for path in uploads)


def test_both_backends_fully_implement_current_contract():
    assert ModalSandboxBackend.__abstractmethods__ == frozenset()
    assert DaytonaSandboxBackend.__abstractmethods__ == frozenset()
