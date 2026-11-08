import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

pytestmark = pytest.mark.superagent

from mac_bench.baselines.superagent.model_relay import ModelRelay, MessagesUsage
from mac_bench.evaluation.episodes.recording import RecordingClient
from mac_bench.evaluation.episodes.budget import EpisodeBudget, BudgetExceeded
from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.runtime.llm.config import LLMConfig


def sse(*events):
    return b''.join(b'event: ' + event['type'].encode() + b'\r\ndata: '
                    + json.dumps(event).encode() + b'\r\n\r\n' for event in events)


START = {'type': 'message_start', 'message': {'id': 'msg-fixture', 'type': 'message',
    'role': 'assistant', 'model': 'deepseek-v4-flash', 'content': [],
    'stop_reason': None, 'stop_sequence': None,
    'usage': {'input_tokens': 7, 'output_tokens': 0, 'cache_read_input_tokens': 11,
              'cache_creation_input_tokens': 3}}}
END = {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
       'usage': {'output_tokens': 5}}
STREAM = sse(START,
    {'type': 'content_block_start', 'index': 0,
     'content_block': {'type': 'thinking', 'thinking': ''}},
    {'type': 'content_block_delta', 'index': 0,
     'delta': {'type': 'thinking_delta', 'thinking': 'Read the public requirements.'}},
    {'type': 'content_block_delta', 'index': 0,
     'delta': {'type': 'signature_delta', 'signature': 'fixture-signature'}},
    {'type': 'content_block_stop', 'index': 0},
    {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
    {'type': 'content_block_delta', 'index': 1,
     'delta': {'type': 'text_delta', 'text': 'The shared workspace is ready.'}},
    {'type': 'content_block_stop', 'index': 1}, END, {'type': 'message_stop'})


class Fragments(httpx.SyncByteStream):
    def __init__(self, data):
        self.data = data

    def __iter__(self):
        for i in range(0, len(self.data), 7):
            yield self.data[i:i + 7]


def recorder(tmp_path, calls=20):
    config = LLMConfig('https://api.deepseek.com', 'provider-secret', 'deepseek-v4-flash',
                       timeout_seconds=2, max_retries=0)
    budget = EpisodeBudget(AgentBudget(max_model_calls=calls, max_tool_calls=20,
        max_env_actions=20, max_tokens=10000, max_wall_seconds=120))
    return RecordingClient(SimpleNamespace(config=config), tmp_path / 'calls', budget=budget)


def post(relay, session='worker-a', request=None):
    body = json.dumps(request or {'model': relay.config.model, 'stream': True, 'max_tokens': 128,
                                 'messages': [{'role': 'user', 'content': 'hello'}]}).encode()
    return httpx.post(relay.base_url + '/v1/messages', content=body, headers={
        'x-api-key': relay.token, 'x-deepseek-harness-session-id': session,
        'x-deepseek-harness-compact': '1' if session == 'compact' else '0'}, timeout=5, trust_env=False)


def test_relay_preserves_stream_and_accounts_concurrent_native_calls(tmp_path):
    client = recorder(tmp_path)
    requests = []

    def provider(request):
        requests.append(request)
        assert request.headers['x-api-key'] == 'provider-secret'
        assert 'x-deepseek-harness-session-id' not in request.headers
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Fragments(STREAM))

    with ModelRelay(client, transport=httpx.MockTransport(provider)) as relay:
        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda session: post(relay, session), ['worker-a', 'child-b', 'compact']))
        assert all(result.content == STREAM for result in results)
    client.budget.finish()
    assert len(requests) == client.count == client.completed_calls == 3
    assert all(request.content == requests[0].content for request in requests)
    assert client.input_tokens == 63 and client.output_tokens == 15
    assert client.budget.snapshot()['model_calls'] == 3
    records = [json.loads(path.read_text()) for path in (tmp_path / 'calls').glob('*.json')]
    assert {r['identity']['agent_id'] for r in records} == {'worker-a', 'child-b', 'compact'}
    assert next(r for r in records if r['identity']['agent_id'] == 'compact')['identity']['purpose'] == 'compaction'
    assert all(r['status'] == 'completed' and r['usage']['total_tokens'] == 26 for r in records)
    assert 'provider-secret' not in ''.join(p.read_text() for p in (tmp_path / 'calls').iterdir())


def test_partial_stream_and_http_retry_each_get_a_receipt(tmp_path):
    client = recorder(tmp_path)
    replies = iter([(200, sse(START, END)), (503, b'{"error":{"type":"overloaded_error"}}'), (200, STREAM)])

    def provider(request):
        status, content = next(replies)
        return httpx.Response(status, content=content)

    with ModelRelay(client, transport=httpx.MockTransport(provider)) as relay:
        assert post(relay).content == sse(START, END)
        assert post(relay).status_code == 503
        assert post(relay).content == STREAM
    client.budget.finish()
    records = [json.loads(path.read_text()) for path in sorted((tmp_path / 'calls').glob('*.json'))]
    assert [r['status'] for r in records] == ['error', 'error', 'completed']
    assert records[0]['usage']['total_tokens'] == 26
    assert not records[0]['response_complete']
    assert client.count == 3 and client.completed_calls == 1
    assert client.budget.snapshot()['model_call_attempts'] == 3
    assert client.budget.snapshot()['input_tokens'] == 42


def test_budget_stops_upstream_before_another_request(tmp_path):
    client = recorder(tmp_path, calls=1)
    seen = []

    def provider(request):
        seen.append(request)
        return httpx.Response(200, content=STREAM)

    with ModelRelay(client, transport=httpx.MockTransport(provider)) as relay:
        assert post(relay).status_code == 200
        assert post(relay).status_code == 400
        assert isinstance(relay.stop_error, BudgetExceeded)
    client.budget.finish()
    assert len(seen) == 1
    assert client.budget.snapshot()['model_call_attempts'] == 1


def test_usage_uses_cumulative_fields_and_rejects_invalid_counts():
    usage = MessagesUsage()
    for byte in STREAM:
        usage.feed(bytes([byte]))
    usage.end()
    assert usage.usage()['total_tokens'] == 26
    with pytest.raises(RuntimeError, match='Invalid Messages usage'):
        usage.feed(sse({'type': 'message_delta', 'usage': {'output_tokens': -1}}))


def test_native_dsh_replays_reasoning_through_recorded_transport(tmp_path):
    import os
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[2]
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node 24 is required for native DSH tests')
    client = recorder(tmp_path)
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(503, json={'type': 'error', 'error': {
                'type': 'overloaded_error', 'message': 'Transient fixture failure'}})
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Fragments(STREAM))

    with ModelRelay(client, transport=httpx.MockTransport(provider)) as relay:
        result = subprocess.run([node, str(root / 'tests/evaluation/superagent/relay_client.mts')],
            cwd=root, env={**os.environ, 'MAC_TEST_RELAY_URL': relay.base_url,
                           'MAC_TEST_RELAY_KEY': relay.token}, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr + result.stdout
    assert len(requests) == 5
    assert 'fixture-signature' in json.dumps(requests[2])
    assert 'Read the public requirements.' in json.dumps(requests[2])
    assert client.budget.snapshot()['model_call_attempts'] == 5
    records = [json.loads(path.read_text()) for path in (tmp_path / 'calls').glob('*.json')]
    assert sum(r['identity']['purpose'] == 'compaction' for r in records) == 1
    assert sum(r['status'] == 'error' for r in records) == 1
    assert client.budget.snapshot()['input_tokens'] == 84


def chat_stream(*, tool=False):
    delta = ({'tool_calls': [{'index': 0, 'id': 'call-check', 'type': 'function',
                            'function': {'name': 'check', 'arguments': '{"value":7}'}}]}
             if tool else {'content': 'Verified.'})
    chunks = [
        {'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if tool else 'stop'}]},
        {'choices': [], 'usage': {'prompt_tokens': 64, 'completion_tokens': 5, 'total_tokens': 69,
                                 'prompt_tokens_details': {'cached_tokens': 16},
                                 'completion_tokens_details': {'reasoning_tokens': 0}}},
    ]
    return b''.join(b'data: ' + json.dumps({'id': 'chat-fixture', 'object': 'chat.completion.chunk',
        'created': 1, 'model': 'openai/gpt-6-luna', **chunk}).encode() + b'\n\n' for chunk in chunks) + b'data: [DONE]\n\n'


def luna_recorder(tmp_path, calls=20):
    client = recorder(tmp_path, calls=calls)
    client.client.config = LLMConfig('https://openrouter.ai/api/v1', 'router-secret',
                                    'openai/gpt-6-luna', timeout_seconds=5, max_retries=0,
                                    thinking_mode='disabled')
    return client


def test_litellm_openrouter_preserves_tools_and_accounts_cached_input(tmp_path):
    client = luna_recorder(tmp_path)
    wire = []
    def provider(request):
        assert str(request.url) == 'https://openrouter.ai/api/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer router-secret'
        body = json.loads(request.content)
        wire.append(body)
        assert body['model'] == 'openai/gpt-6-luna'
        assert body['reasoning'] == {'effort': 'none'}
        assert body['stream_options']['include_usage'] is True
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              stream=Fragments(chat_stream(tool=len(wire) == 1)))

    request = {'model': client.config.model, 'stream': True, 'max_tokens': 128,
        'thinking': {'type': 'disabled'}, 'system': [{'type': 'text', 'text': 'Use the public check.'}],
        'tools': [{'name': 'check', 'description': 'Check a value.', 'input_schema': {
            'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']}}],
        'messages': [{'role': 'user', 'content': 'Check seven.'}]}
    with ModelRelay(client, transport=httpx.MockTransport(provider)) as relay:
        result = post(relay, request=request)
        assert result.status_code == 200 and b'"tool_use"' in result.content
        assert b'call-check' in result.content
        request['messages'].extend([
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-check',
                                             'name': 'check', 'input': {'value': 7}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-check',
                                        'content': 'Value verified.'}]},
        ])
        assert b'Verified.' in post(relay, session='compact', request=request).content
    assert client.count == client.completed_calls == len(wire) == 2
    assert wire[0]['messages'][0]['role'] == 'system'
    assert wire[1]['messages'][-1]['role'] == 'tool'
    assert wire[1]['messages'][-1]['tool_call_id'] == 'call-check'
    assert client.input_tokens == 128 and client.output_tokens == 10
    records = [json.loads(p.read_text()) for p in sorted((tmp_path/'calls').glob('*.json'))]
    assert records[1]['identity']['purpose'] == 'compaction'
    assert records[0]['provider_request'] == wire[0]
    assert all(r['transport'] == 'litellm_openrouter' and r['response_complete'] for r in records)
    assert 'router-secret' not in ''.join(p.read_text() for p in (tmp_path/'calls').iterdir())


def test_litellm_transport_does_not_retry_outside_episode_accounting(tmp_path):
    client = luna_recorder(tmp_path, calls=1)
    seen = []
    def provider(request):
        seen.append(request)
        return httpx.Response(503, json={'error': {'message': 'Temporarily unavailable', 'code': 503}})
    with ModelRelay(client, transport=httpx.MockTransport(provider)) as relay:
        response = post(relay)
        assert response.status_code == 503
        assert post(relay).status_code == 400
    assert len(seen) == client.count == 1
    record = json.loads((tmp_path/'calls/0001.json').read_text())
    assert record['status'] == 'error' and record['http_status'] == 503


def test_litellm_concurrent_requests_keep_their_own_provider_receipts(tmp_path):
    from threading import Barrier
    barrier = Barrier(3)
    client = luna_recorder(tmp_path)
    def provider(request):
        barrier.wait(timeout=5)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=chat_stream())
    def call(relay, session):
        return post(relay, session=session, request={
            'model': client.config.model, 'stream': True, 'max_tokens': 128,
            'messages': [{'role': 'user', 'content': session}]})
    with ModelRelay(client, transport=httpx.MockTransport(provider)) as relay:
        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda session: call(relay, session), ['worker', 'child', 'compact']))
        assert all(r.status_code == 200 and b'message_stop' in r.content for r in results)
    records = [json.loads(p.read_text()) for p in (tmp_path/'calls').glob('*.json')]
    assert client.completed_calls == 3
    for record in records:
        assert record['provider_request']['messages'][0]['content'] == record['identity']['agent_id']
        assert record['usage']['prompt_tokens'] == 64
