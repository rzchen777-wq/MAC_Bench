"""Bounded documents/results, actor isolation, safe diagnostics and RPC transport."""

import asyncio
import io
import json
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from mac_bench.environments.errors import NativeGraphQLFailure, NativeHTTPFailure
from mac_bench.environments.packaging.modal_session import ModalWorldSession
from mac_bench.environments.runtime.infrastructure.transport import RestTransport
from mac_bench.environments.tools import (
    AppConnection,
    CallJournal,
    Role,
    WorldTools,
)
from mac_bench.environments.tools.api import create_mcp_server
from mac_bench.environments.tools.contracts import (
    NativeResult,
    Tool,
    ToolError,
    encoded,
    object_schema,
)
from mac_bench.environments.tools.error_feedback import feedback
from mac_bench.environments.tools.navigation import page, resolve
from mac_bench.environments.tools.registry import ToolRegistry


def recover(document, path=''):
    cursor, items, text = None, {}, ''
    while True:
        result = page(document, identity='test', path=path, cursor=cursor, limit=3)
        assert len(encoded(result).encode()) <= 8000
        if result['kind'] == 'scalar':
            return result['value']
        if result['kind'] == 'string':
            text += result['value']
        else:
            for item in result['items']:
                items[item['key']] = item['value'] if item['complete'] else recover(document, item['path'])
        cursor = result['next_cursor']
        if not cursor:
            if result['kind'] == 'string':
                return text
            return [items[k] for k in sorted(items)] if result['kind'] == 'array' else items


def test_navigation_is_lossless_for_deep_large_unicode_data_and_long_leaf():
    document = {'a/b~c': {'rows': [{'id': str(i), 'content': '中😀\n' * 400} for i in range(12)]},
                'required': [str(i) for i in range(100)], 'string': '😀' * 5000, 'bool': False}
    assert recover(document) == document
    assert resolve(document, '/a~1b~0c/rows/0/id') == '0'
    with pytest.raises(ToolError, match='does not exist'):
        resolve(document, '/a~1b~0c/rows/-1')
    cursor = page(document, identity='test', limit=1)['next_cursor']
    with pytest.raises(ToolError) as failure:
        page(document, identity='other', cursor=cursor, limit=1)
    assert failure.value.code == 'invalid_cursor'


@pytest.fixture
def world(tmp_path):
    data = {'rows': [{'id': str(i), 'value': 'large record' * 1000} for i in range(30)]}
    calls = []
    def read(connection, args):
        calls.append(args)
        return NativeResult(data)
    tool = Tool('hrms.example.read', 'hrms', 'hrms.example', 'read', 'binding', {},
                'Read example', object_schema(), 'Native document', True, read)
    registry = ToolRegistry([tool], catalog_revision='test')
    journal = CallJournal(tmp_path / 'calls.sqlite')
    runtime = WorldTools('instance', registry, [Role('operator', (AppConnection('hrms', {}, None),), ('*',))], journal)
    yield runtime, calls, data
    runtime.close()
    journal.close()


def test_large_result_is_saved_scoped_and_replay_never_reexecutes(world):
    runtime, calls, data = world
    a, b = runtime.agent('a', 'operator'), runtime.agent('b', 'operator')
    receipt = a.call_tool('hrms.example.read', {}, 'read-1')
    assert len(encoded(receipt).encode()) < 14000
    assert not receipt['data_complete'] and receipt['data'] is None
    leaf = a.read_result(receipt['result_ref'], path='/data/rows/3/id')
    assert leaf['value'] == '3' and leaf['source'] == 'saved_receipt_not_live_state'
    projected = a.query_result(receipt['result_ref'], path='/data/rows', keys=['3', '29'],
        fields={'id': '/id'}, aggregates=[{'name': 'count', 'op': 'count'}])
    assert [r['values']['id'] for r in projected['rows']] == ['3', '29']
    assert projected['aggregates']['count']['value'] == 2 and len(calls) == 1
    with pytest.raises(ToolError, match='accessible'):
        b.query_result(receipt['result_ref'])
    replay = a.call_tool('hrms.example.read', {}, 'read-1')
    assert replay['replayed'] and len(calls) == 1
    with pytest.raises(ToolError, match='accessible'):
        b.read_result(receipt['result_ref'])
    data['rows'][3]['id'] = 'changed'
    assert a.read_result(receipt['result_ref'], path='/data/rows/3/id')['value'] == '3'
    assert a.query_result(receipt['result_ref'], path='/data/rows', keys=['3'],
        fields={'id': '/id'})['rows'][0]['values']['id'] == '3'
    new = a.call_tool('hrms.example.read', {}, 'read-2')
    assert a.read_result(new['result_ref'], path='/data/rows/3/id')['value'] == 'changed'
    runtime.close()
    with pytest.raises(ToolError, match='closed'):
        a.read_result(receipt['result_ref'])


def test_request_lookup_does_not_execute_and_preserves_actor_scope(world):
    from mac_bench.environments.tools.api import dispatch
    runtime, calls, _ = world
    a, b = runtime.agent('a', 'operator'), runtime.agent('b', 'operator')
    receipt = a.call_tool('hrms.example.read', {}, 'lost-reply')
    recovered = dispatch(a, 'read_result', {'request_id': 'lost-reply', 'path': '/data/rows/3/id'})
    assert recovered['result_ref'] == receipt['call_id'] and recovered['value'] == '3'
    assert recovered['status'] == 'succeeded' and len(calls) == 1
    for actor, request in [(b, 'lost-reply'), (a, 'never-sent')]:
        with pytest.raises(ToolError) as error:
            dispatch(actor, 'read_result', {'request_id': request})
        assert error.value.code == 'request_not_recorded'
    assert len(calls) == 1
    for args in [{}, {'result_ref': receipt['call_id'], 'request_id': 'lost-reply'}]:
        with pytest.raises(ToolError):
            dispatch(a, 'read_result', args)


def test_request_lookup_distinguishes_unfinished_from_failed_receipts(world):
    runtime, calls, _ = world
    a = runtime.agent('a', 'operator')
    identity, _ = runtime.journal.begin(runtime.world_id, a.actor, a.role.name,
        'interrupted', 'hrms.example.read', {})
    with pytest.raises(ToolError) as error:
        a.read_result(request_id='interrupted')
    assert error.value.code == 'result_unfinished'
    runtime.journal.finish(identity, {'call_id': identity, 'status': 'unknown', 'data': None,
                                    'error': {'code': 'native_call_failed'}})
    result = a.read_result(request_id='interrupted', path='/error/code')
    assert result['value'] == 'native_call_failed' and result['status'] == 'unknown'
    assert not calls


def test_large_partial_receipt_keeps_failure_reason(world):
    runtime, _, data = world
    a = runtime.agent('a', 'operator')
    identity, _ = runtime.journal.begin(runtime.world_id, a.actor, a.role.name,
        'partial', 'compound', {})
    result = {'call_id': identity, 'status': 'partial', 'data': data,
              'error': {'code': 'native_steps_incomplete', 'message': 'Read current state before continuing.'}}
    runtime.journal.finish(identity, result)
    response = a._deliver(result, False)
    assert response['data_complete'] is False and response['error'] == result['error']
    assert a.read_result(request_id='partial', path='/error/code')['value'] == 'native_steps_incomplete'


def test_saved_observation_survives_journal_reopen_and_does_not_expose_arguments(tmp_path):
    p = tmp_path / 'journal.sqlite'
    journal = CallJournal(p)
    call, _ = journal.begin('world', 'a', 'role', 'request', 'tool', {'private_argument': 'secret'})
    journal.finish(call, {'data': {'id': 1}, 'status': 'succeeded'})
    journal.close()
    reopened = CallJournal(p)
    try:
        receipt, _ = reopened.receipt('world', 'a', 'role', call)
        assert receipt == {'data': {'id': 1}, 'status': 'succeeded'}
        with pytest.raises(ToolError):
            reopened.receipt('reset-world', 'a', 'role', call)
    finally:
        reopened.close()


def test_observation_time_is_receipt_completion_not_request_start(tmp_path):
    journal = CallJournal(tmp_path/'calls.sqlite')
    try:
        call, _ = journal.begin('w','a','r','q','tool',{})
        journal.finish(call, {'data':{},'status':'succeeded'})
        journal.db.execute('UPDATE tool_calls SET started=?, finished=? WHERE call_id=?',
            ('2026-11-06T00:00:00+00:00','2026-11-06T00:00:06+00:00',call))
        journal.db.commit()
        _, timestamp = journal.receipt('w','a','r',call)
        assert timestamp == '2026-11-06T00:00:06+00:00'
    finally:
        journal.close()


def test_native_error_retains_only_safe_field_guidance(world):
    runtime, _, _ = world
    tool = runtime.registry.get('hrms.example.read')
    object.__setattr__(tool, 'native_contract', {'fields': {'employee': {}}})
    payload = {'employee': ['This field is required. SECRET_VALUE'], 'traceback': 'SECRET_TRACE',
               'headers': {'Authorization': 'SECRET_TOKEN'}}
    transport = RestTransport('http://native.invalid', headers={})
    def fail(*args, **kwargs):
        raise HTTPError('http://native.invalid', 422, 'error', {}, io.BytesIO(json.dumps(payload).encode()))
    transport.opener = SimpleNamespace(open=fail)
    with pytest.raises(NativeHTTPFailure) as failure:
        transport.request('POST', 'record', {})
    result = feedback(failure.value, tool)
    assert result['field_errors'] == [{'field': 'employee', 'rule': 'required'}]
    assert 'SECRET' not in encoded(result)
    assert 'SECRET' not in str(failure.value)


def test_unsupported_refund_preserves_safe_lifecycle_guidance(world):
    runtime, _, _ = world
    tool = runtime.registry.get('hrms.example.read')
    object.__setattr__(tool, 'native_contract', {'fields': {'amount': {}}})
    transport = RestTransport('http://native.invalid', headers={})

    def fail(*args, **kwargs):
        payload = {'amount': ['Full refund not available for this payment method. SECRET_VALUE'],
                   'traceback': 'SECRET_TRACE'}
        raise HTTPError('http://native.invalid', 400, 'error', {},
                        io.BytesIO(json.dumps(payload).encode()))

    transport.opener = SimpleNamespace(open=fail)
    with pytest.raises(NativeHTTPFailure) as failure:
        transport.request('POST', 'refund', {})
    result = feedback(failure.value, tool)
    assert result['code'] == 'unsupported_native_operation'
    assert result['field_errors'] == [{'field': 'amount', 'rule': 'unsupported_operation'}]
    assert 'alternative lifecycle' in result['message']
    assert 'SECRET' not in encoded(result)



def test_mcp_sets_protocol_error_flag_for_native_rejection(world):
    runtime, _, _ = world
    actor = runtime.agent('a', 'operator')
    server = create_mcp_server(actor)
    async def check():
        result = await server.call_tool('call_tool', {'tool_name': 'unavailable', 'arguments': {}, 'request_id': 'request'})
        assert result.isError
        assert result.structuredContent['status'] == 'rejected'
        tools = await server.list_tools()
        assert {'read_result', 'query_result'} <= {t.name for t in tools}
        receipt = actor.call_tool('hrms.example.read', {}, 'mcp-query-source')
        queried = await server.call_tool('query_result', {'result_ref': receipt['result_ref'],
            'path': '/data/rows', 'fields': {'id': '/id'}, 'limit': 2})
        payload = json.loads(next(block.text for block in queried if block.type == 'text'))
        assert payload['total_matched'] == 30
        assert [r['values']['id'] for r in payload['rows']] == ['0', '1']
    asyncio.run(check())


def test_hrms_singleton_locator_and_explicit_create_identity(monkeypatch):
    from mac_bench.environments.tools.adapters import frappe
    from mac_bench.environments.tools.contracts import validate

    binding = {'id': 'settings', 'entity': 'hrms.settings', 'native_resource': 'Settings'}
    context = SimpleNamespace(contracts={'settings': {'fields': {}}})
    calls = []

    class Client:
        def __init__(self, transport):
            pass

        def read(self, doctype, name):
            calls.append(('read', doctype, name))
            return {'name': name}

        def create(self, doctype, values, name=None):
            calls.append(('create', doctype, name))
            return {'name': name, **values}

    monkeypatch.setattr(frappe, 'CatalogFrappeClient', Client)
    schema, read, _, _ = frappe.operation(context, binding, 'read', {'input': {'read_locator': 'singleton'}})
    validate(schema, {})
    assert read(SimpleNamespace(transport=None), {}).data == {'name': 'Settings'}
    with pytest.raises(ToolError):
        validate(schema, {'name': 'another-record'})
    schema, create, _, _ = frappe.operation(context, binding, 'create', {})
    arguments = {'name': 'NATIVE-ID', 'values': {}}
    validate(schema, arguments)
    assert create(SimpleNamespace(transport=None), arguments).data['name'] == 'NATIVE-ID'
    assert calls == [('read', 'Settings', 'Settings'), ('create', 'Settings', 'NATIVE-ID')]


def test_mcp_marks_executed_rejected_receipt_as_error(world):
    runtime, calls, _ = world
    tool = runtime.registry.get('hrms.example.read')

    def reject(connection, args):
        raise ToolError('stale_version', 'Read the document again before editing.')

    object.__setattr__(tool, 'handler', reject)
    server = create_mcp_server(runtime.agent('a', 'operator'))

    async def check():
        result = await server.call_tool('call_tool', {
            'tool_name': tool.name, 'arguments': {}, 'request_id': 'rejected-call'})
        assert result.isError
        assert result.structuredContent['status'] == 'rejected'
        assert result.structuredContent['error']['code'] == 'stale_version'
        assert result.structuredContent['result_ref']

    asyncio.run(check())
    assert not calls


def test_native_database_schema_failure_is_not_an_invalid_business_reference(world):
    runtime, _, _ = world
    tool = runtime.registry.get('hrms.example.read')
    error = NativeGraphQLFailure([{'message': 'column taskTarget.targetCompany does not exist',
                                   'extensions': {'traceback': 'SECRET_TRACE'}}])
    result = feedback(error, tool)
    assert result['code'] == 'native_schema_mismatch'
    assert result['field_errors'] == []
    assert 'SECRET' not in encoded(result)
    assert 'taskTarget' not in encoded(result)
    assert feedback(NativeGraphQLFailure([{'message': 'Record does not exist'}]), tool)['field_errors'] == [{'rule': 'invalid_reference'}]


def test_native_item_capability_error_keeps_known_code_without_private_payload(world):
    from mac_bench.environments.errors import NativeHTTPFailure
    runtime, _, _ = world
    tool = runtime.registry.get('hrms.example.read')
    error = NativeHTTPFailure('POST', 400, {'errors': [
        {'type': 'NOT_SELL_ABLE_ITEMS', 'message': 'SECRET business input',
         'stacktrace': 'SECRET_TRACE', 'statusCode': 400}]})
    result = feedback(error, tool)
    assert result['native_error_type'] == 'NOT_SELL_ABLE_ITEMS'
    assert result['code'] == 'native_item_capability_rejected'
    assert result['http_status'] == 400 and 'SECRET' not in encoded(result)
    unknown = feedback(NativeHTTPFailure('POST', 400,
        {'errors': [{'type': 'SECRET_UNKNOWN_TYPE', 'message': 'SECRET'}]}), tool)
    assert 'native_error_type' not in unknown and 'SECRET' not in encoded(unknown)
