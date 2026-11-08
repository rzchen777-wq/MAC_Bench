"""Scripts use the same admitted public calls as direct agents, over real stdio."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac_bench.environments.tools.navigation import page
from mac_bench.evaluation.episodes.budget import EpisodeBudget, BudgetExceeded
from mac_bench.evaluation.episodes.identity import call_identity, call_scope
from mac_bench.evaluation.tasks.execution import TaskExecution, TaskExecutionError
from mac_bench.evaluation.tasks.protocol import ToolDefinition
from mac_bench.evaluation.tasks.tools import AgentEnvironment
from mac_bench.runtime.agents.workflow import AgentBudget
import subprocess
import time
from mac_bench.runtime.sandbox.base import CommandResult

class LocalFixtureBackend:
    """Execute the same remote worker as a subprocess, only for fixture tests."""
    def exec(self, sandbox, command, *, cwd=None, timeout):
        started = time.monotonic()
        process = subprocess.run(command, shell=True, cwd=cwd, timeout=timeout,
                                 capture_output=True, text=True)
        return CommandResult(command, process.returncode, process.stdout + process.stderr,
                             time.monotonic() - started)

    def open_channel(self, sandbox, *, argv, cwd):
        return LocalChannel(argv, cwd)

    def read_file(self, sandbox, *, path):
        return Path(path).read_bytes()

class LocalChannel:
    def __init__(self, argv, cwd):
        self.process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    def write(self, data):
        pending = memoryview(data)
        while pending:
            pending = pending[self.process.stdin.write(pending):]
        self.process.stdin.flush()

    def read(self):
        while chunk := self.process.stdout.read(65536):
            yield chunk

    def close(self):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=10)
            assert code == 0, self.process.stderr.read().decode()
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.process.stderr.close()

def completed(files, name):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = files.call('process.poll', {'id': name, 'waitMs': 100})
        if value['done']:
            return value
    raise AssertionError('The fixture command did not finish')


@pytest.fixture
def runtime(tmp_path):
    definitions = (
        ToolDefinition('read_record', 'Read the current value.', {'type': 'object', 'additionalProperties': False}),
        ToolDefinition('set_record', 'Set a nonnegative value.', {'type': 'object', 'required': ['value'],
            'properties': {'value': {'type': 'integer'}}, 'additionalProperties': False}),
        ToolDefinition('read_result', 'Read a saved result.', {'type': 'object', 'required': ['result_ref'],
            'properties': {'result_ref': {'type': 'string'}, 'path': {'type': 'string'},
                'cursor': {'type': 'string'}, 'limit': {'type': 'integer'}}, 'additionalProperties': False}),
    )
    budget = EpisodeBudget(AgentBudget(max_model_calls=10, max_tool_calls=1000,
        max_env_actions=1000, max_tokens=100000, max_wall_seconds=60))
    events, calls = [], []
    state = {'value': 10}
    observation = {'data': {'rows': [{'id': i, 'body': '完整内容🙂' * 1500} for i in range(21)],
        'empty': [], 'null': None, 'mixed/key~': {'one': True, 'two': []}, 'more_app_records': True}}

    def dispatch(name, arguments):
        calls.append((name, arguments, call_identity()))
        if name == 'read_record':
            return dict(state)
        if name == 'set_record':
            if arguments['value'] < 0:
                return {'status': 'rejected', 'error': {'code': 'negative_value'}}
            state.update(arguments)
            return {'status': 'ok', 'data': dict(state)}
        return {'result_ref': arguments['result_ref'], 'source': 'saved_receipt_not_live_state', 'observed_at': '2026-11-06T00:00:00Z',
            **page(observation, identity='fixed', path=arguments.get('path', '/data'),
                cursor=arguments.get('cursor'), limit=arguments.get('limit', 20), compact=True)}

    environment = AgentEnvironment(definitions, dispatch)
    environment.begin(admit_call=budget.admit_tool, event=events.append)
    execution = TaskExecution(LocalFixtureBackend(), None, str(tmp_path),
        public_environment=environment, tool_definitions=definitions)
    value = SimpleNamespace(execution=execution, environment=environment, budget=budget,
        events=events, calls=calls, state=state, observation=observation, root=tmp_path)
    try:
        yield value
    finally:
        environment.end()
        execution.close()


def launch(runtime, name, code, *, actor='worker-a', allowed=True, extra_env=None):
    script = runtime.root / (name + '.py')
    script.write_text(code)
    args = {'id': name, 'argv': ['python3', str(script)], 'cwd': str(runtime.root), 'graceMs': 100,
        'env': extra_env or {}, 'stdio': {'stdin': 'ignore', 'stdout': 'pipe', 'stderr': 'pipe'}}
    with call_scope(agent_id=actor, tool_call_id=name + '-bash', tool='bash'):
        runtime.execution.call('process.spawn', args, public_tools=allowed)
    return args


def result(runtime, name):
    value = completed(runtime.execution, name)
    assert value['exitCode'] == 0, base64.b64decode(value['stderr']['data']).decode()
    return json.loads(base64.b64decode(value['stdout']['data']))


def test_script_read_compute_write_save_and_another_worker_resume(runtime):
    launch(runtime, 'first', """
import json
from mac_tools import call, definitions
before = call('read_record')
changed = call('set_record', value=before['value'] + 7)
json.dump({'before': before, 'changed': changed}, open('handover.json', 'w'))
print(json.dumps({'changed': changed, 'tools': [tool['name'] for tool in definitions()]}))
""")
    one = result(runtime, 'first')
    assert one['changed'] == {'status': 'ok', 'data': {'value': 17}}
    assert one['tools'] == ['read_record', 'set_record', 'read_result']
    with call_scope(agent_id='direct-worker'):
        runtime.environment.call('set_record', {'value': 20})
    launch(runtime, 'next', """
import json
from mac_tools import call
saved = json.load(open('handover.json'))
current = call('read_record')
print(json.dumps({'saved': saved['changed']['data'], 'current': current,
                  'final': call('set_record', value=current['value'] + 2)}))
""", actor='worker-b')
    two = result(runtime, 'next')
    assert two == {'saved': {'value': 17}, 'current': {'value': 20}, 'final': {'status': 'ok', 'data': {'value': 22}}}
    assert runtime.budget.tool_calls == 5
    assert [identity['agent_id'] for _, _, identity in runtime.calls] == [
        'worker-a', 'worker-a', 'direct-worker', 'worker-b', 'worker-b']
    starts = [event for event in runtime.events if event['kind'] == 'task_tool_start']
    assert len(starts) == len({event['tool_call_id'] for event in starts if 'tool_call_id' in event}) + 1
    assert starts[0]['tool'] == 'read_record'
    assert starts[0]['parent_tool_call_id'] == 'first-bash'


def test_complete_receipt_navigation_preserves_nested_data_and_does_not_query_live_state(runtime):
    launch(runtime, 'full', """
import json
from mac_tools import load_result
observation = load_result('observation-1')
json.dump(observation, open('complete.json', 'w'), ensure_ascii=False)
print(json.dumps({'rows': len(observation['data']['rows'])}))
""")
    assert result(runtime, 'full') == {'rows': 21}
    assert json.loads((runtime.root / 'complete.json').read_text()) == {
        'result_ref': 'observation-1', 'source': 'saved_receipt_not_live_state',
        'observed_at': '2026-11-06T00:00:00Z', 'path': '/data', 'data': runtime.observation['data']}
    assert {name for name, _, _ in runtime.calls} == {'read_result'}
    assert runtime.budget.tool_calls > 21


def test_schema_errors_and_business_rejections_have_the_same_return_as_direct_calls(runtime):
    launch(runtime, 'errors', """
import json
from mac_tools import call
print(json.dumps([call('not_a_tool'), call('set_record', value='seven'), call('set_record', value=-1)]))
""")
    assert result(runtime, 'errors') == [runtime.environment.call('not_a_tool', {}),
        runtime.environment.call('set_record', {'value': 'seven'}), runtime.environment.call('set_record', {'value': -1})]
    assert runtime.state == {'value': 10}
    assert runtime.budget.tool_calls == 6


def test_concurrent_scripts_keep_identity_while_process_polls_wait(runtime):
    for i in range(4):
        launch(runtime, f'concurrent{i}', """
import json
from concurrent.futures import ThreadPoolExecutor
from mac_tools import call
with ThreadPoolExecutor(8) as pool:
    rows = list(pool.map(lambda _: call('read_record'), range(8)))
print(json.dumps(rows))
""", actor=f'worker-{i}')
    with ThreadPoolExecutor(4) as pool:
        values = list(pool.map(lambda i: result(runtime, f'concurrent{i}'), range(4)))
    assert values == [[{'value': 10}] * 8] * 4
    assert runtime.budget.tool_calls == 32
    for i in range(4):
        identities = [identity for _, _, identity in runtime.calls if identity['agent_id'] == f'worker-{i}']
        assert len(identities) == 8
        assert {identity['parent_tool_call_id'] for identity in identities} == {f'concurrent{i}-bash'}


def test_budget_and_revocation_prevent_further_mutation(runtime):
    runtime.budget.limits = replace(runtime.budget.limits, max_tool_calls=1)
    launch(runtime, 'budget', """
import json
from mac_tools import call, ToolClientError
first = call('read_record')
try:
    call('set_record', value=999)
except ToolClientError as error:
    print(json.dumps({'first': first, 'error': error.code}))
""")
    assert result(runtime, 'budget') == {'first': {'value': 10}, 'error': 'EPISODE_BUDGET'}
    assert isinstance(runtime.execution.stop_error, BudgetExceeded)
    assert runtime.state == {'value': 10}
    runtime.environment.end()
    launch(runtime, 'closed', """
import json
from mac_tools import call, ToolClientError
try:
    call('set_record', value=999)
except ToolClientError as error:
    print(json.dumps({'error': error.code}))
""")
    assert result(runtime, 'closed')['error'] == 'TOOLS_CLOSED'
    assert runtime.state == {'value': 10}


def test_process_cannot_grant_itself_tools_or_claim_another_worker(runtime):
    # Knowledge of the socket location is not an access grant.
    client_root = str(Path(runtime.execution.public_tool_guide).parent)
    launch(runtime, 'ungranted', f"""
import json, os, sys
sys.path.insert(0, {client_root!r})
os.environ['MAC_BENCH_TOOLS_SOCKET'] = {client_root + '/tools.sock'!r}
from mac_tools import call, ToolClientError
try:
    call('set_record', value=999)
except ToolClientError as error:
    print(json.dumps({{'error': error.code}}))
""", actor='superagent-router', allowed=False)
    assert result(runtime, 'ungranted') == {'error': 'TOOLS_UNAVAILABLE'}
    assert not runtime.calls
    args = launch(runtime, 'owned', "print('{}')")
    assert result(runtime, 'owned') == {}
    with call_scope(agent_id='worker-b'):
        with pytest.raises(TaskExecutionError, match='already belongs'):
            runtime.execution.call('process.spawn', args, public_tools=True)




def test_cli_and_nested_subprocess_use_the_launching_identity(runtime):
    launch(runtime, 'cli', """
import subprocess, json
data = subprocess.check_output(['mac-tools', 'read_record'], text=True)
print(data)
""")
    assert result(runtime, 'cli') == {'value': 10}
    assert runtime.calls[0][2]['agent_id'] == 'worker-a'
