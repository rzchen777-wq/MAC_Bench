"""Exercise the production Python -> Node -> DSH -> public-world path."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

pytestmark = pytest.mark.superagent

from mac_bench.baselines.superagent import runner
from mac_bench.baselines.superagent.tasks import Superagent
from mac_bench.baselines.superagent.model_relay import ModelRelay
from mac_bench.evaluation.tasks.adapters.engineering import TERMINAL
from mac_bench.evaluation.tasks.execution import TaskExecution
from mac_bench.evaluation.tasks.protocol import TaskView
from mac_bench.evaluation.tasks.protocol import ToolDefinition
from mac_bench.evaluation.tasks.tools import AgentEnvironment
from mac_bench.evaluation.episodes.budget import BudgetExceeded
from .support import LocalFixtureBackend
from .support import recorder, sse, START


def reply(value, sequence):
    tool = isinstance(value, dict)
    block = ({'type': 'tool_use', 'id': f'call-{sequence}', 'name': value['name'], 'input': {}}
             if tool else {'type': 'text', 'text': ''})
    delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(value['arguments'])}
             if tool else {'type': 'text_delta', 'text': value})
    return sse(START, {'type': 'content_block_start', 'index': 0, 'content_block': block},
        {'type': 'content_block_delta', 'index': 0, 'delta': delta},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tool else 'end_turn'},
         'usage': {'output_tokens': 5}}, {'type': 'message_stop'})


@pytest.mark.parametrize('domain', ['software', 'business', 'business-script'])
@pytest.mark.parametrize('model_calls', [30, 1])
def test_production_runner_routes_executes_and_reports(tmp_path, monkeypatch, domain, model_calls):
    root = Path(__file__).resolve().parents[2]
    work = tmp_path / 'work'
    work.mkdir()
    installed = []
    client = recorder(tmp_path, calls=model_calls)
    events, requests = [], []
    states = {'routed': False, 'acted': False, 'reported': False, 'replied': False}

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        assert 'dsh_session_log' not in body
        assert len(requests) <= 15
        names = {tool['name'] for tool in body['tools']}
        if 'orchestrator_route' in names:
            if not states['routed']:
                states['routed'] = True
                value = {'name': 'orchestrator_route', 'arguments': {'decisions': [{'kind': 'accept',
                    'trigger_id': 'task:runner-fixture', 'title': 'Create result'}]}}
            elif states['reported'] and not states['replied']:
                states['replied'] = True
                # Read the ticket identifier supplied by the actual routing tool.
                ticket = next(json.loads(block['text'])['ticket']['id']
                    for message in body['messages'] for block in message.get('content', [])
                    if isinstance(block, dict) and block.get('type') == 'text'
                    and block['text'].startswith('{"kind":"worker_report"'))
                value = {'name': 'orchestrator_reply', 'arguments': {'ticket_id': ticket, 'text': 'Created result.'}}
            else:
                value = 'The worker is handling the request.'
        elif not states['acted']:
            states['acted'] = True
            value = ({'name': 'bash', 'arguments': {'description': 'Create and verify the result',
                'command': "printf 'completed\\n' > result.txt; cat result.txt"}} if domain == 'software'
                else {'name': 'bash', 'arguments': {'description': 'Update the business record from a script',
                    'command': '''python3 -c "from mac_tools import call; print(call('update_record', status='completed'))"'''}}
                if domain == 'business-script' else {'name': 'update_record', 'arguments': {'status': 'completed'}})
        elif not states['reported']:
            states['reported'] = True
            value = {'name': 'orchestrator_report', 'arguments': {'state': 'ready', 'summary': 'Created and checked result.txt.'}}
        else:
            value = 'Finished.'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=reply(value, len(requests)))

    monkeypatch.setattr(runner, 'ModelRelay', lambda recording: ModelRelay(recording, transport=httpx.MockTransport(provider)))
    tools = ((TERMINAL,) if domain == 'software' else (ToolDefinition('update_record', 'Update the shared business record.',
        {'type': 'object', 'properties': {'status': {'type': 'string'}}, 'required': ['status']}),))
    records = []
    environment = AgentEnvironment(tools, lambda name, args: records.append(args) or args)
    environment.begin(admit_call=client.budget.admit_tool, event=events.append)
    execution = TaskExecution(LocalFixtureBackend(), None, str(work),
        public_environment=environment if domain != 'software' else None, tool_definitions=tools)
    # Local fixture uses its existing rg; production uploads this published binary.
    execution.install_executable = lambda name, content: installed.append((name, content[:4]))
    instructions = ('Edit and verify the actual workspace through terminal.' if domain == 'software' else
        (root / 'src/mac_bench/environments/packaging/guide.txt').read_text())
    context = SimpleNamespace(task=TaskView('runner-fixture', 'fixture', 'Complete the requested result.',
        tools, instructions), client=client, environment=environment,
        budget=client.budget, event=events.append, artifacts=tmp_path / 'native', open_execution=lambda: execution)
    try:
        baseline = Superagent()
        baseline.spec['model_parameters']['max_tokens'] = 4096
        baseline.spec['execution']['background_grace_seconds'] = 10
        baseline.validate()
        if model_calls == 1:
            with pytest.raises(BudgetExceeded, match='model_calls'):
                baseline.run(context)
            assert not (work / 'result.txt').exists() and not records
            assert client.count == 1
            return
        result = baseline.run(context)
    finally:
        environment.end()
        execution.close()
    assert result['stop_reason'] == 'ready'
    assert json.loads((tmp_path / 'native/method.json').read_text())['method'] == 'superagent'
    assert len(result['replies']) == 1 and result['replies'][0]['text'] == 'Created result.'
    if domain == 'software':
        assert (work / 'result.txt').read_text() == 'completed\n'
    else:
        assert records == [{'status': 'completed'}]
    assert installed == [('rg', b'\x7fELF')]
    assert client.count == len(requests)
    assert all(json.loads(path.read_text())['status'] == 'completed'
               for path in (tmp_path / 'calls').glob('*.json'))
    assert context.budget.tool_calls == (2 if domain == 'business-script' else 1)
    if domain == 'business-script':
        assert all('No shell' not in json.dumps(request) for request in requests)
        assert any('mac_tools' in json.dumps(request) for request in requests)
        calls = [event for event in events if event.get('kind') == 'task_tool_start' and event.get('tool') == 'update_record']
        assert len(calls) == 1 and calls[0]['agent_id'] != 'superagent-router'
        assert calls[0]['execution_process_id'] and calls[0]['parent_tool_call_id']
    started = {event['agent_id'] for event in events if event['kind'] == 'agent_start'}
    ended = {event['agent_id'] for event in events if event['kind'] == 'agent_end'}
    assert len(started) == 2 and started == ended
    assert any(event['kind'] == 'method_control_call' for event in events)
    assert 'provider-secret' not in (tmp_path / 'native/episode.json').read_text()
