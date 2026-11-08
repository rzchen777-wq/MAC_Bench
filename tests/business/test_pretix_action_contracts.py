"""Reviewed handler controls and state guards agree with the public invocation path."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac_bench.environments.catalog.entities import EntityCatalog
from mac_bench.environments.errors import NativeHTTPFailure
from mac_bench.environments.tools import build_registry
from mac_bench.environments.tools.contracts import ToolError, validate
from mac_bench.environments.tools.adapters.pretix_actions import STATE_GUARDS
from mac_bench.environments.tools.error_feedback import feedback


@pytest.fixture(scope='module')
def registry():
    import os
    from mac_bench.runtime.paths import repository_root
    task_root = Path(os.environ.get('MAC_BENCH_TASK_ROOT', repository_root()/'data/tasks'))
    path = task_root/'op26_business_finance_008/environment/runtime/catalog/entity/resources/catalog.json.gz'
    if not path.is_file():
        pytest.skip('Install task data to check native application contracts')
    catalog = EntityCatalog.load(path)
    bindings = [v['bindings'][0] for e in ('pretix.order', 'pretix.order_payment', 'pretix.order_refund', 'pretix.order_position')
                for v in catalog.variants(e)]
    return build_registry(catalog, bindings=bindings)


def action(registry, entity, name):
    return next(t for t in registry.tools() if t.entity == entity and t.operation.split('@')[0] == name)


@pytest.mark.parametrize('entity,body', [
    ('pretix.order_payment', {'amount': '1.00', 'provider': 'manual', 'state': 'created', 'send_email': False, 'force': False}),
    ('pretix.order_refund', {'amount': '1.00', 'provider': 'manual', 'state': 'created', 'source': 'admin',
                           'payment': 1, 'mark_refunded': False, 'mark_pending': True}),
])
def test_handler_controls_are_accepted_and_forwarded_without_rewriting(registry, entity, body):
    t = action(registry, entity, 'create')
    args = {'path': {'organizer': 'org', 'event': 'event', 'order': 'ABC12'}, 'body': body}
    validate(t.input_schema, args)
    calls = []
    def request(method, path, payload):
        calls.append((method, path, payload))
        return SimpleNamespace(status=201, body={'local_id': 2, 'state': body['state']})
    result = t.handler(SimpleNamespace(settings={}, transport=SimpleNamespace(request_response=request)), args)
    assert calls == [('POST', 'api/v1/organizers/org/events/event/orders/ABC12/'+
                       ('payments/' if entity.endswith('payment') else 'refunds/'), body)]
    assert result.data['local_id'] == 2
    # String "false" is truthy in the native handler; reject it before execution.
    flag = 'send_email' if entity.endswith('payment') else 'mark_pending'
    with pytest.raises(ToolError):
        validate(t.input_schema, {**args, 'body': {**body, flag: 'false'}})


def test_expiry_is_required_and_is_a_date_not_a_session_timestamp(registry):
    t = action(registry, 'pretix.order', 'extend')
    args = {'path': {'organizer': 'org', 'event': 'event', 'code': 'ABC12'}}
    validate(t.input_schema, {**args, 'body': {'expires': '2027-06-01', 'force': False}})
    for body in ({}, {'expires': None}, {'expires': '2027-06-01T12:00:00Z'}, {'expires': 'not a date'}):
        with pytest.raises(ToolError): validate(t.input_schema, {**args, 'body': body})


def test_checkin_uses_ticket_secret_while_editing_uses_position_id(registry):
    t = action(registry, 'pretix.order_position', 'redeem')
    args = {'path': {'organizer': 'org', 'event': 'event', 'list': 2, 'pk': 'ticket-secret'}, 'body': {}}
    validate(t.input_schema, args)
    assert 'barcode or secret' in t.input_schema['properties']['path']['properties']['pk']['description']
    assert not any('pk is the position id' in n for n in t.notes)
    calls = []
    def request(method, path, body):
        calls.append((method, path, body))
        return SimpleNamespace(status=200, body={'status': 'ok'})
    t.handler(SimpleNamespace(settings={}, transport=SimpleNamespace(request_response=request)), args)
    assert calls == [('POST', 'api/v1/organizers/org/events/event/checkinlists/2/positions/ticket-secret/redeem/', {})]
    edit = action(registry, 'pretix.order_position', 'partial_update')
    assert any('pk is the position id' in n for n in edit.notes)


@pytest.mark.parametrize('entity,name', [('pretix.order_payment','refund'), ('pretix.order_refund','process'),
                                         ('pretix.order_refund','create')])
def test_refund_control_has_one_public_name(registry, entity, name):
    t = action(registry, entity, name)
    body = {'mark_refunded': False}
    if name == 'create':
        body.update(amount='1.00', provider='manual', source='admin', state='created')
    path = {'organizer': 'org', 'event': 'event', 'order': 'ABC12'}
    if name != 'create': path['local_id'] = 1
    validate(t.input_schema, {'path': path, 'body': body})
    with pytest.raises(ToolError):
        validate(t.input_schema, {'path': path, 'body': {**body, 'mark_canceled': False}})


@pytest.mark.parametrize('key,message', list(STATE_GUARDS.items()))
def test_only_exact_reviewed_prewrite_guard_is_known_rejection(registry, key, message):
    t = action(registry, *key)
    locators = t.input_schema['properties']['path']['required']
    args = {'path': {k: 1 if k=='local_id' else 'test' for k in locators}, 'body': {}}
    for status, payload, outcome in [
        (400, {'detail':message}, 'not_started'),
        (500, {'detail':message}, 'unknown'),
        (400, {'detail':message, 'other':'later failure'}, 'unknown'),
        (400, {'detail':'External error: failure after creation'}, 'unknown'),
    ]:
        error = NativeHTTPFailure('POST', status, payload)
        def request(*a): raise error
        connection = SimpleNamespace(settings={},transport=SimpleNamespace(request_response=request))
        with pytest.raises(NativeHTTPFailure) as caught:
            t.handler(connection,args)
        assert caught.value is error and error.outcome == outcome
        explanation = feedback(error, t)
        if outcome == 'not_started':
            assert explanation['code'] == 'native_state_rejected'
            assert 'rejected before changing data' in explanation['message']
            assert explanation['field_errors'][0]['field'] == ('status' if key[0] == 'pretix.order' else 'state')
        else:
            assert explanation['code'] != 'native_state_rejected'
            assert 'rejected before changing data' not in explanation['message']
