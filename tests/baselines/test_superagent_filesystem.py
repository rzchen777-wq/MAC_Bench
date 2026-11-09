from .support import LocalFixtureBackend
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import tarfile
from types import SimpleNamespace
from threading import Barrier

import pytest

pytestmark = pytest.mark.superagent

from mac_bench.baselines.superagent.bridge import WorldBridge
from mac_bench.evaluation.episodes.budget import EpisodeBudget
from mac_bench.evaluation.tasks.execution import TaskExecution, TaskExecutionError
from mac_bench.evaluation.tasks.protocol import ToolDefinition
from mac_bench.evaluation.tasks.tools import AgentEnvironment
from mac_bench.runtime.agents.workflow import AgentBudget






@pytest.fixture
def files(tmp_path):
    files = TaskExecution(LocalFixtureBackend(), None, str(tmp_path))
    try:
        yield files
    finally:
        files.close()


def test_guarded_edits_and_aliases_use_one_actual_file(files, tmp_path):
    original = tmp_path / 'source.txt'
    original.write_text('first\n')
    (tmp_path / 'alias.txt').symlink_to(original.name)
    one = files.call('fs.resolve', {'path': 'source.txt'})
    two = files.call('fs.resolve', {'path': 'alias.txt'})
    assert one == two
    observed = files.call('fs.stat', {'path': one['displayPath']})

    def edit(replacement):
        try:
            return files.call('fs.edit', {'path': one['displayPath'], 'expected': {'version': observed['version']},
                'edit': {'oldString': 'first', 'newString': replacement, 'replaceAll': False}})
        except TaskExecutionError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(edit, ['worker-a', 'worker-b']))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert 'FS_STALE_VERSION' in outcomes
    assert original.read_text().strip() in {'worker-a', 'worker-b'}
    assert (tmp_path / 'alias.txt').is_symlink()


def test_text_and_byte_reads_are_distinct_and_bounded(files, tmp_path):
    (tmp_path / 'binary').write_bytes(b'\0\xff\x01\x02')
    with pytest.raises(TaskExecutionError) as error:
        files.call('fs.read_text', {'path': 'binary'})
    assert error.value.code == 'FS_NOT_TEXT'
    assert base64.b64decode(files.call('fs.read_range', {'path': 'binary', 'offset': 1, 'length': 2})) == b'\xff\x01'
    with pytest.raises(TaskExecutionError) as error:
        files.call('fs.read_bytes', {'path': 'binary', 'maxBytes': 3})
    assert error.value.code == 'FS_TOO_LARGE'
    assert files.call('fs.stat', {'path': 'absent'}) is None


def test_metadata_probe_preserves_aliases_and_refreshes_changes(files, tmp_path):
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested/child').mkdir()
    (tmp_path / 'link').symlink_to('nested/child', target_is_directory=True)
    (tmp_path / 'nested/AGENTS.md').write_text('first')
    paths = ['link/../AGENTS.md', 'nested/AGENTS.md', 'missing']
    rows = files.call('fs.probe', {'paths': paths})
    assert rows[0]['targetKey'] == rows[1]['targetKey']
    assert rows[0]['info'] == rows[1]['info']
    assert rows[2]['info'] is None
    (tmp_path / 'nested/AGENTS.md').unlink()
    (tmp_path / 'missing').write_text('new')
    fresh = files.call('fs.probe', {'paths': paths})
    assert fresh[0]['info'] is None and fresh[1]['info'] is None
    assert fresh[2]['info']['type'] == 'file'


def test_persistent_connection_transfers_large_utf8_files_without_truncation(files):
    content = ('中文 source line\n' * 20000)
    files.call('fs.write', {'path': 'large.txt', 'content': content, 'expected': {'kind': 'createIfAbsent'}})
    assert files.call('fs.read_text', {'path': 'large.txt'}) == content
    assert base64.b64decode(files.call('fs.read_range', {'path': 'large.txt', 'offset': 65535, 'length': 65536})) == content.encode()[65535:131071]


def test_concurrent_rpc_flushes_preserve_results_and_freshness(files, tmp_path):
    writes = []
    original = files._channel.write
    def slow_transport(data):
        writes.append(data)
        time.sleep(.02)
        original(data)
    files._channel.write = slow_transport
    gate = Barrier(12)
    for i in range(12):
        (tmp_path / f'file-{i}').write_text('x' * (i + 1))
    def read(i):
        gate.wait()
        return files.call('fs.stat', {'path': f'file-{i}'})['size']
    with ThreadPoolExecutor(12) as pool:
        assert list(pool.map(read, range(12))) == list(range(1, 13))
    assert len(writes) < 12, 'Concurrent requests should share transport acknowledgements'
    requests = [json.loads(line) for batch in writes for line in batch.splitlines()]
    assert len({request['id'] for request in requests}) == 12
    (tmp_path / 'file-0').write_text('new contents')
    assert files.call('fs.stat', {'path': 'file-0'})['size'] == len('new contents')


def spawn(files, name, command, *, stdin='ignore', capture=1024):
    return files.call('process.spawn', {'id': name, 'argv': ['/bin/bash', '-c', command],
        'cwd': files.workspace, 'graceMs': 100,
        'stdio': {'stdin': stdin, 'stdout': {'maxBytes': capture, 'spill': {'maxBytes': 1024 * 1024}},
                  'stderr': {'maxBytes': capture}}})


def completed(files, name):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = files.call('process.poll', {'id': name, 'waitMs': 100})
        if value['done']:
            return value
    raise AssertionError('The fixture command did not finish')


def test_remote_process_is_started_once_and_preserves_bounded_streams(files, tmp_path):
    command = "printf 'started\\n' >> starts; python3 -c 'print(\"abcdefghij\" * 1000)'; printf 'warning' >&2"
    spawn(files, 'once', command, capture=64)
    spawn(files, 'once', command, capture=64)
    result = completed(files, 'once')
    assert result['exitCode'] == 0
    assert result['stdout']['lossy']
    assert len(base64.b64decode(result['stdout']['data'])) == 64
    assert base64.b64decode(result['stderr']['data']) == b'warning'
    assert Path(result['stdout']['spillPath']).read_text() == 'abcdefghij' * 1000 + '\n'
    assert (tmp_path / 'starts').read_text() == 'started\n'
    # Two readers can inspect the same output without consuming one another's data.
    assert completed(files, 'once')['stdout'] == result['stdout']


def test_remote_stdin_and_process_session_cleanup(files, tmp_path):
    spawn(files, 'input', 'cat > input.txt', stdin='pipe')
    files.call('process.write', {'id': 'input', 'data': base64.b64encode(b'hello\n').decode(), 'eof': True})
    assert completed(files, 'input')['exitCode'] == 0
    assert (tmp_path / 'input.txt').read_text() == 'hello\n'
    spawn(files, 'stop', "sleep 60 & child=$!; printf '%s\\n' $$ $child > active-pids; wait")
    deadline = time.monotonic() + 3
    while not (tmp_path / 'active-pids').exists():
        assert time.monotonic() < deadline
        time.sleep(.01)
    pids = list(map(int, (tmp_path / 'active-pids').read_text().splitlines()))
    for pid in pids:
        os.kill(pid, 0)
    files.call('process.terminate', {'id': 'stop'})
    assert completed(files, 'stop')['signal'] in {'SIGTERM', 'SIGKILL'}
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        # An orphan zombie is no longer executing and is reaped by its init.
        assert Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[0] == 'Z'


def test_long_poll_wakes_on_output_then_exit(files):
    spawn(files, 'streaming', "sleep .15; printf ready; sleep .3")
    start = time.monotonic()
    output = files.call('process.poll', {'id': 'streaming', 'waitMs': 30000})
    assert base64.b64decode(output['stdout']['data']) == b'ready'
    assert not output['done']
    end = files.call('process.poll', {'id': 'streaming', 'waitMs': 30000,
        'stdoutOffset': output['stdout']['nextOffset']})
    assert end['done'] and end['exitCode'] == 0
    assert time.monotonic() - start < 3


def test_long_polls_leave_control_and_file_operations_available(files):
    spawn(files, 'quiet', 'exec sleep 60')
    with ThreadPoolExecutor(16) as pool:
        waiting = [pool.submit(files.call, 'process.poll', {'id': 'quiet', 'waitMs': 30000})
                   for _ in range(16)]
        # Wait until the requests have been sent through the transport.
        deadline = time.monotonic() + 3
        while len(files._pending) < 16:
            assert time.monotonic() < deadline
            time.sleep(.01)
        started = time.monotonic()
        assert files.call('fs.stat', {'path': '.'})['type'] == 'directory'
        files.call('process.terminate', {'id': 'quiet'})
        assert all(future.result(timeout=3)['done'] for future in waiting)
        assert time.monotonic() - started < 3


def test_close_interrupts_a_write_to_a_child_that_does_not_read_stdin(tmp_path):
    execution = TaskExecution(LocalFixtureBackend(), None, str(tmp_path))
    spawn(execution, 'unread-input', 'exec sleep 60', stdin='pipe')
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(execution.call, 'process.write', {
            'id': 'unread-input', 'data': base64.b64encode(b'x' * 1048576).decode(), 'eof': True})
        # The RPC write has reached the worker once another request is served.
        execution.call('fs.stat', {'path': '.'})
        execution.close()
        with pytest.raises(TaskExecutionError):
            pending.result(timeout=3)
    assert execution._channel.process.returncode == 0


def test_full_native_output_survives_workspace_cleanup(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    archive = tmp_path / 'receipts/execution_outputs.tar'
    execution = TaskExecution(LocalFixtureBackend(), None, str(workspace), output_archive=archive)
    try:
        spawn(execution, 'logged-command', "python3 -c 'print(\"trace\" * 2000)'", capture=64)
        assert completed(execution, 'logged-command')['stdout']['lossy']
    finally:
        execution.close()
    shutil.rmtree(tmp_path / 'tool-output')
    with tarfile.open(archive) as saved:
        assert saved.extractfile('logged-command/stdout.log').read() == b'trace' * 2000 + b'\n'


def test_creates_are_atomic_and_ambiguous_edits_do_not_change_files(files, tmp_path):
    created = files.call('fs.write', {'path': 'nested/new.txt', 'content': 'one\r\none\r\n',
                                  'expected': {'kind': 'createIfAbsent'}})
    assert created['after'] == 'one\none\n' and created['operation'] == 'create'
    with pytest.raises(TaskExecutionError) as error:
        files.call('fs.write', {'path': 'nested/new.txt', 'content': 'lost', 'expected': {'kind': 'createIfAbsent'}})
    assert error.value.code == 'FS_NOT_OBSERVED'
    with pytest.raises(TaskExecutionError) as error:
        files.call('fs.edit', {'path': 'nested/new.txt',
                            'edit': {'oldString': 'one', 'newString': 'two', 'replaceAll': False}})
    assert error.value.code == 'FS_AMBIGUOUS_EDIT'
    assert (tmp_path / 'nested/new.txt').read_text() == 'one\none\n'


def test_native_file_tools_share_state_and_reject_another_agents_stale_edit(files, tmp_path):
    root = Path(__file__).resolve().parents[2]
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node 24 is required for native DSH tests')
    (tmp_path / 'shared.txt').write_text('initial\n')
    events = []
    budget = EpisodeBudget(AgentBudget(max_model_calls=50, max_tool_calls=50,
        max_env_actions=50, max_tokens=100000, max_wall_seconds=60))
    context = SimpleNamespace(budget=budget, event=events.append)
    with WorldBridge(context, files) as bridge:
        process = subprocess.run([node, str(root / 'tests/baselines/superagent/filesystem_client.mts')],
            cwd=root, env={**os.environ, 'MAC_TEST_WORLD_URL': bridge.url,
                'MAC_TEST_WORLD_KEY': bridge.token, 'MAC_TEST_WORKSPACE': str(tmp_path)},
            capture_output=True, text=True, timeout=40)
        assert process.returncode == 0, process.stdout + process.stderr
    assert (tmp_path / 'shared.txt').read_text() == 'verified\n'
    assert budget.snapshot()['tool_calls'] == 7
    starts = [event for event in events if event['kind'] == 'task_tool_start']
    assert {event['agent_id'] for event in starts} == {'file-worker-a', 'file-worker-b'}
    assert sum(event['tool'] == 'edit' for event in starts) == 3
    assert len({event['tool_call_id'] for event in starts}) == 7


def test_parent_and_native_child_share_the_public_application_api(files, tmp_path):
    root = Path(__file__).resolve().parents[2]
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node 24 is required for native DSH tests')
    definitions = (
        ToolDefinition('read_record', 'Read the current shared record.', {
            'type': 'object', 'properties': {}, 'additionalProperties': False}),
        ToolDefinition('update_record', 'Update the shared record status.', {
            'type': 'object', 'properties': {'status': {'type': 'string'}},
            'required': ['status'], 'additionalProperties': False}),
    )
    state = {'status': 'initial'}
    def dispatch(name, args):
        if name == 'update_record':
            state['status'] = args['status']
        return dict(state)
    events = []
    budget = EpisodeBudget(AgentBudget(max_model_calls=50, max_tool_calls=50,
        max_env_actions=50, max_tokens=100000, max_wall_seconds=60))
    environment = AgentEnvironment(definitions, dispatch)
    environment.begin(admit_call=budget.admit_tool, event=events.append)
    context = SimpleNamespace(environment=environment, budget=budget, event=events.append)
    try:
        with WorldBridge(context, files) as bridge:
            process = subprocess.run([node, str(root / 'tests/baselines/superagent/application_client.mts')],
                cwd=root, env={**os.environ, 'MAC_TEST_WORLD_URL': bridge.url, 'MAC_TEST_WORLD_KEY': bridge.token,
                    'MAC_TEST_WORKSPACE': str(tmp_path),
                    'MAC_TEST_TOOLS': json.dumps([tool.as_function()['function'] for tool in definitions])},
                capture_output=True, text=True, timeout=25)
            assert process.returncode == 0, process.stdout + process.stderr
    finally:
        environment.end()
    starts = [event for event in events if event['kind'] == 'task_tool_start']
    assert budget.snapshot()['tool_calls'] == 2, 'Application operations must be counted once'
    assert [event['tool'] for event in starts] == ['update_record', 'read_record']
    assert starts[0]['agent_id'] != starts[1]['agent_id']
    assert starts[1]['agent_id'] == 'application-worker'


def test_remote_job_wakes_native_worker_and_hands_off_once(files, tmp_path):
    root = Path(__file__).resolve().parents[2]
    node = shutil.which('node')
    events = []
    # A task-style foreground check crosses DSH's 5 s worker window. DSH
    # must own the job and wake the worker without a second background layer.
    (tmp_path / 'develop.sh').write_text("#!/bin/sh\nset -eu\n[ \"$1\" = check ]\n"
        "printf 'started\\n' >> starts\nsleep 6\nprintf 'done\\n' > result.txt\n"
        "cat result.txt\nprintf 'REMOTE_FINISHED\\n'\n")
    budget = EpisodeBudget(AgentBudget(max_model_calls=50, max_tool_calls=50,
        max_env_actions=50, max_tokens=100000, max_wall_seconds=60))
    context = SimpleNamespace(budget=budget, event=events.append)
    with WorldBridge(context, files) as bridge:
        process = subprocess.run([node, str(root / 'tests/baselines/superagent/process_client.mts')],
            cwd=root, env={**os.environ, 'MAC_TEST_WORLD_URL': bridge.url,
                'MAC_TEST_WORLD_KEY': bridge.token, 'MAC_TEST_WORKSPACE': str(tmp_path)},
            capture_output=True, text=True, timeout=35)
        assert process.returncode == 0, process.stdout + process.stderr
    assert budget.snapshot()['tool_calls'] == 3
    assert (tmp_path / 'starts').read_text() == 'started\n'


@pytest.mark.parametrize('client', ['process_provider_client.mts', 'parallel_client.mts'])
def test_remote_process_control_and_parallel_children(tmp_path, client, monkeypatch):
    # Set the inherited variable before starting the execution worker so that
    # the native unset check cannot pass merely because it was already absent.
    monkeypatch.setenv('MAC_TEST_AMBIENT', 'must-be-removed')
    execution = TaskExecution(LocalFixtureBackend(), None, str(tmp_path))
    root = Path(__file__).resolve().parents[2]
    node = shutil.which('node')
    budget = EpisodeBudget(AgentBudget(max_model_calls=50, max_tool_calls=50,
        max_env_actions=50, max_tokens=100000, max_wall_seconds=60))
    context = SimpleNamespace(budget=budget, event=lambda event: None)
    try:
        with WorldBridge(context, execution) as bridge:
            process = subprocess.run([node, str(root / 'tests/baselines/superagent' / client)],
                cwd=root, env={**os.environ, 'MAC_TEST_WORLD_URL': bridge.url,
                    'MAC_TEST_WORLD_KEY': bridge.token, 'MAC_TEST_WORKSPACE': str(tmp_path)},
                capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stdout + process.stderr
    finally:
        execution.close()


def test_zero_capture_limit_retains_no_output(files):
    spawn(files, 'no-capture', "printf 'not retained'", capture=0)
    result = completed(files, 'no-capture')['stdout']
    assert result['data'] == ''
    assert result['lossy']
    assert result['nextOffset'] == len('not retained')


def test_absolute_executable_works_with_a_custom_child_path(files):
    files.call('process.spawn', {'id': 'custom-path', 'argv': ['/bin/echo', 'working'],
        'cwd': files.workspace, 'env': {'PATH': '/nonexistent'}, 'graceMs': 100,
        'stdio': {'stdin': 'ignore', 'stdout': {'maxBytes': 1024}, 'stderr': {'maxBytes': 1024}}})
    result = completed(files, 'custom-path')
    assert result['exitCode'] == 0
    assert base64.b64decode(result['stdout']['data']) == b'working\n'


def test_instruction_discovery_batches_probes_and_refreshes_after_shell_edits(files, tmp_path):
    root = Path(__file__).resolve().parents[2]
    node = shutil.which('node')
    (tmp_path / '.git').mkdir()
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'AGENTS.md').write_text('ROOT_GUIDANCE_ONE\n')
    (tmp_path / 'nested/AGENTS.md').write_text('NESTED_GUIDANCE_ONE\n')
    (tmp_path / 'nested/file.txt').write_text('initial\n')
    operations = []
    timing_file = tmp_path / 'timings.jsonl'
    call = files.call
    def record(operation, args, **kwargs):
        operations.append(operation)
        return call(operation, args, **kwargs)
    files.call = record
    context = SimpleNamespace(budget=EpisodeBudget(AgentBudget(max_tokens=100000,
        max_model_calls=50, max_tool_calls=50, max_env_actions=50, max_wall_seconds=60)),
        event=lambda event: None)
    with WorldBridge(context, files) as bridge:
        process = subprocess.run([node, str(root / 'tests/baselines/superagent/instructions_client.mts')],
            cwd=root, env={**os.environ, 'MAC_TEST_WORLD_URL': bridge.url,
                'MAC_TEST_WORLD_KEY': bridge.token, 'MAC_TEST_WORKSPACE': str(tmp_path),
                'MAC_TEST_TIMING_FILE': str(timing_file)},
            capture_output=True, text=True, timeout=40)
    assert process.returncode == 0, process.stdout + process.stderr
    assert operations.count('fs.probe') > 0, 'Native instruction discovery must enter the batching scope'
    rows = [json.loads(line) for line in timing_file.read_text().splitlines()]
    refreshes = [row for row in rows if row.get('phase') == 'instruction_refresh'
                 and row['kind'] == 'span']
    assert {row['refresh_phase'] for row in refreshes} == {'pre_step', 'file_touch'}
    assert {row['agent_id'] for row in refreshes} == {'instructions-reader', 'instructions-editor'}
    for refresh in refreshes:
        calls = [row for row in rows if row['kind'] == 'rpc' and row.get('span') == refresh['span']]
        assert refresh['status'] == 'completed' and refresh['duration_ms'] >= 0
        assert not any(row['operation'] in {'fs.resolve', 'fs.stat'} for row in calls), (
            'Both refresh paths must batch metadata instead of serial remote resolve/stat calls')
        assert sum(row['operation'] == 'fs.probe' for row in calls) <= 6
    assert all('arguments' not in row and 'credential' not in row for row in rows)


def test_task_stops_execution_before_capturing_submission():
    from mac_bench.evaluation.tasks.adapters.base import OwnedTask
    from mac_bench.evaluation.tasks.protocol import TaskView
    events = []

    class Task(OwnedTask):
        def _dispatch(self, name, arguments):
            raise AssertionError('No application calls in this fixture')
        def _open_execution(self):
            events.append('open')
            return SimpleNamespace(close=lambda: events.append('join processes'))
        def _stop(self):
            events.append('capture submission')
        def _close(self):
            events.append('delete sandbox')

    task = Task()
    task._configure(TaskView('fixture', 'fixture', '', (), ''))
    task._opened = task._episode = True
    assert task.open_execution() is task.open_execution()
    task.end_episode()
    task.close()
    assert events == ['open', 'join processes', 'capture submission', 'delete sandbox']


def test_business_workspace_contains_only_public_materials(tmp_path, monkeypatch):
    from mac_bench.evaluation.tasks.adapters.operational import OperationalTask
    from mac_bench.evaluation.tasks import execution as capability
    from mac_bench.runtime.sandbox import modal
    (tmp_path / 'public').mkdir()
    (tmp_path / 'private').mkdir()
    (tmp_path / 'public/task.txt').write_text('Complete the public assignment.')
    (tmp_path / 'public/records.json').write_text('{"records": []}')
    (tmp_path / 'private/answer.json').write_text('private reference')
    uploads = {}
    closed = []
    sandbox = SimpleNamespace(id='business-workspace-fixture')
    backend = SimpleNamespace(
        create=lambda **kwargs: sandbox,
        exec=lambda *args, **kwargs: SimpleNamespace(exit_code=0),
        write_files=lambda target, files: uploads.update(files),
        delete=lambda target: closed.append(target),
    )
    monkeypatch.setattr(modal, 'ModalSandboxBackend', lambda **kwargs: backend)
    monkeypatch.setattr(capability, 'TaskExecution', lambda backend, sandbox, workspace, **kwargs:
        SimpleNamespace(workspace=workspace, close=lambda: None))
    task = object.__new__(OperationalTask)
    task.directory = tmp_path
    task.bundle = SimpleNamespace(root=tmp_path)
    task.resources = SimpleNamespace(names=('public/task.txt', 'public/records.json'))
    task._configure(SimpleNamespace(tools=()))
    from mac_bench.runtime.sandbox.providers import resolve_config
    task._backend_config = resolve_config("business")
    task._scratch = task._execution = None
    task._execution = task._open_execution()
    assert json.loads((tmp_path / 'agent_workspace.json').read_text()) == {
        'id': sandbox.id, 'status': 'running'}
    assert uploads == {
        '/work/project/public/task.txt': b'Complete the public assignment.',
        '/work/project/public/records.json': b'{"records": []}',
    }
    assert task._execution.workspace == '/work/project'
    task._close_execution()
    assert closed == [sandbox]
    assert json.loads((tmp_path / 'agent_workspace.json').read_text())['status'] == 'terminated'


@pytest.mark.parametrize('other_interface', ['process', 'terminal'])
def test_command_memory_budget_is_shared_across_interfaces(tmp_path, monkeypatch, other_interface):
    from mac_bench.evaluation.tasks import execution
    from mac_bench.evaluation.tasks.terminal_output import capture_command, output_result
    monkeypatch.setattr(execution, '_WORKER', execution._WORKER.replace(
        'COMMAND_MEMORY_MIB = 6144', 'COMMAND_MEMORY_MIB = 96'))
    tmp_path = tmp_path / 'workspace'
    tmp_path.mkdir()
    command = "python3 -c 'import time;a=bytearray(55*1024*1024);time.sleep(2)'"
    files = TaskExecution(LocalFixtureBackend(), None, str(tmp_path))
    terminal = None
    try:
        spawn(files, 'large-one', command)
        if other_interface == 'process':
            spawn(files, 'large-two', command)
            results = [completed(files, name) for name in ['large-one', 'large-two']]
            assert sorted(result['exitCode'] for result in results) == [0, 137]
            assert any(result.get('resourceLimit') == 'shared_memory' for result in results)
        else:
            log = tmp_path / 'terminal.log'
            terminal = subprocess.Popen(['sh', '-c', capture_command(command, str(log),
                cwd=str(tmp_path), memory_limit_mib=96)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            result = completed(files, 'large-one')
            stdout, stderr = terminal.communicate(timeout=8)
            assert not stderr
            other = output_result(SimpleNamespace(stdout=stdout, exit_code=terminal.returncode,
                                  duration_seconds=0), str(log))
            assert sorted([result['exitCode'], other['exit_code']]) == [0, 137]
            assert 'shared_memory' in [result.get('resourceLimit'), other.get('resource_limit_exceeded')]
        spawn(files, 'small-read', 'printf usable')
        result = completed(files, 'small-read')
        assert result['exitCode'] == 0
        assert base64.b64decode(result['stdout']['data']) == b'usable'
    finally:
        files.close()
        if terminal and terminal.poll() is None:
            terminal.kill()
            terminal.communicate()
