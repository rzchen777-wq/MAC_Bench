"""Full output remains accessible and previewing never reruns the command."""
import subprocess
import json
from hashlib import sha256
from types import SimpleNamespace

from mac_bench.evaluation.tasks.terminal_output import capture_command, output_result


def run(command, log, cwd):
    proc = subprocess.run(['sh', '-c', capture_command(command, str(log), cwd=str(cwd))],
        capture_output=True, text=True, timeout=10)
    return output_result(SimpleNamespace(stdout=proc.stdout, exit_code=proc.returncode,
        duration_seconds=0), str(log))


def test_large_output_is_saved_once_and_retains_actual_exit(tmp_path):
    log = tmp_path / 'logs' / 'full.log'
    command = "printf x >> counter; python3 -c 'print(\"a\" * 15000 + \"UNIQUE_MIDDLE\" + \"z\" * 15000)'; exit 7"
    result = run(command, log, tmp_path)
    assert result['exit_code'] == 7 and result['output_truncated']
    assert len(result['output'].encode()) < 25000 and 'UNIQUE_MIDDLE' not in result['output']
    assert 'UNIQUE_MIDDLE' in log.read_text()
    assert (tmp_path / 'counter').read_text() == 'x'
    assert result['full_output_path'] == str(log)
    recovered = run(f"rg -o UNIQUE_MIDDLE '{log}'", tmp_path / 'logs' / 'lookup.log', tmp_path)
    assert recovered['output'].strip() == 'UNIQUE_MIDDLE'
    assert (tmp_path / 'counter').read_text() == 'x'


def test_small_output_and_stderr_and_shell_quoting(tmp_path):
    result = run("printf '%s' '$(touch SHOULD_NOT_EXIST)'; printf 'error' >&2; exit 3",
        tmp_path / 'with space' / 'out.log', tmp_path)
    assert result['exit_code'] == 3 and not result['output_truncated']
    assert result['output'] == '$(touch SHOULD_NOT_EXIST)error'
    assert not (tmp_path / 'SHOULD_NOT_EXIST').exists()


def test_json_escaping_cannot_inflate_terminal_preview(tmp_path):
    log = tmp_path / 'controls.log'
    result = run("python3 -c 'import sys; sys.stdout.write(chr(1) * 10000)'", log, tmp_path)
    assert result['output_bytes'] == 10000 and result['output_truncated']
    assert len(json.dumps(result['output'], ensure_ascii=False).encode()) <= 24000
    assert log.read_bytes() == b'\x01' * 10000


def test_deadline_stops_descendants_and_preserves_workspace(tmp_path):
    import time
    log = tmp_path / 'timed.log'
    command = "printf durable > saved; (sleep 1; printf leaked > late) & wait"
    wrapped = capture_command(command, str(log), cwd=str(tmp_path), timeout_seconds=.15)
    proc = subprocess.run(['sh', '-c', wrapped], capture_output=True, text=True, timeout=5)
    result = output_result(SimpleNamespace(stdout=proc.stdout, exit_code=proc.returncode,
        duration_seconds=0), str(log))
    assert result['exit_code'] == 124 and result['resource_limit_exceeded'] == 'timeout'
    assert 'workspace preserved' in result['output'] and log.exists()
    time.sleep(1.1)
    assert not (tmp_path / 'late').exists()
    assert (tmp_path / 'saved').read_text() == 'durable'
    assert run('cat saved', tmp_path/'after.log', tmp_path)['output'] == 'durable'


def test_runaway_allocation_fails_without_destroying_workspace(tmp_path):
    log = tmp_path/'memory.log'
    command = "printf durable > saved; python3 -c 'a=bytearray(256*1024*1024)'"
    wrapped = capture_command(command, str(log), cwd=str(tmp_path), memory_limit_mib=64)
    proc = subprocess.run(['sh','-c',wrapped],capture_output=True,text=True,timeout=5)
    result = output_result(SimpleNamespace(stdout=proc.stdout,exit_code=proc.returncode,
        duration_seconds=0),str(log))
    assert result['exit_code'] != 0 and 'MemoryError' in result['output']
    assert result['command_memory_limit_mib'] == 64
    assert (tmp_path/'saved').read_text() == 'durable'


def test_resident_limit_includes_parallel_children(tmp_path):
    log = tmp_path/'group.log'
    # Each child is individually below the data cap; their group exceeds it.
    command = "python3 -c 'import time;a=bytearray(45*1024*1024);time.sleep(3)' & " \
              "python3 -c 'import time;a=bytearray(45*1024*1024);time.sleep(3)' & wait"
    wrapped = capture_command(command,str(log),cwd=str(tmp_path),memory_limit_mib=80)
    proc = subprocess.run(['sh','-c',wrapped],capture_output=True,text=True,timeout=5)
    result = output_result(SimpleNamespace(stdout=proc.stdout,exit_code=proc.returncode,
        duration_seconds=0),str(log))
    assert result['exit_code'] == 137 and result['resource_limit_exceeded'] == 'memory'


def test_concurrent_commands_share_resident_budget_and_leave_small_reads_usable(tmp_path):
    # Each command is within its own limit, but the pair exceeds the shared one.
    command = "python3 -c 'import time;a=bytearray(55*1024*1024);time.sleep(1.5)'"
    processes = []
    try:
        for i in range(2):
            log = tmp_path/f'parallel-{i}.log'
            wrapped = capture_command(command, str(log), cwd=str(tmp_path), memory_limit_mib=96)
            processes.append((log, subprocess.Popen(['sh', '-c', wrapped],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
        results = []
        for log, proc in processes:
            stdout, stderr = proc.communicate(timeout=5)
            assert not stderr
            results.append(output_result(SimpleNamespace(stdout=stdout,
                exit_code=proc.returncode, duration_seconds=0), str(log)))
        assert any(r.get('resource_limit_exceeded') == 'shared_memory' for r in results)
        assert any(r['exit_code'] == 0 for r in results)
        assert all(r['shared_command_memory_limit_mib'] == 96 for r in results)
        registry = tmp_path.parent / '.mac-bench-command-resources' / sha256(str(tmp_path.resolve()).encode()).hexdigest()[:16]
        assert {p.name for p in registry.iterdir()} == {'lock'}
        assert run('printf available', tmp_path/'read.log', tmp_path)['output'] == 'available'
    finally:
        for _, proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
