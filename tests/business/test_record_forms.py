"""Administrative form semantics, side effects, recovery and role isolation."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac_bench.environments.tools import CallJournal, NativeResult, Role, Tool, ToolError, WorldTools
from mac_bench.environments.tools.contracts import object_schema
from mac_bench.environments.tools.registry import ToolRegistry
from mac_bench.environments.business.runtime import runtime_source


@pytest.fixture
def specification():
    path = Path('tests/fixtures/business_record_forms.py')
    spec = importlib.util.spec_from_file_location('public_record_form_definition', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.specification()


@pytest.fixture
def world(tmp_path, specification):
    rows = {e: {} for e in ('twenty.note', 'twenty.task', 'twenty.company', 'twenty.calendar_event',
                            'twenty.note_target', 'hrms.employee', 'hrms.shift_assignment')}
    rows['twenty.company']['venue-uuid'] = {'id': 'venue-uuid'}
    rows['twenty.company']['other-uuid'] = {'id': 'other-uuid'}
    rows['twenty.note']['source-uuid'] = {'id': 'source-uuid', 'title': 'Source', 'bodyV2': {'markdown': 'Evidence'}}
    for value in ('calendar-a', 'calendar-b'):
        rows['twenty.calendar_event'][value] = {'id': value}
    rows['hrms.employee']['HR-001'] = {'name': 'HR-001'}
    rows['hrms.shift_assignment']['DUTY-001'] = {'name': 'DUTY-001', 'docstatus': 1}
    control = {'writes': [], 'fail_link': False, 'unknown_link': False}

    def handler(entity, operation):
        def invoke(connection, args):
            table = rows[entity]
            if operation == 'findOne':
                return NativeResult(deepcopy(table.get(args['filter']['id']['eq'])))
            if operation == 'read':
                return NativeResult(deepcopy(table.get(args['name'])))
            if operation == 'findMany':
                found = [deepcopy(v) for v in table.values() if v.get('noteId') == args['filter']['noteId']['eq']]
                return NativeResult({'edges': [{'node': v} for v in found], 'pageInfo': {'hasNextPage': False}})
            if entity == 'twenty.note_target' and control['fail_link']:
                raise ToolError('link_rejected', 'Native relation was rejected.')
            if entity == 'twenty.note_target' and control['unknown_link']:
                raise TimeoutError('Transport timeout must not escape into the public receipt')
            control['writes'].append((entity, operation))
            if operation == 'createOne':
                identity = entity + '-' + str(len(table) + 1)
                table[identity] = {'id': identity, **deepcopy(args['data'])}
                return NativeResult(deepcopy(table[identity]))
            if operation == 'updateOne':
                table[args['id']].update(deepcopy(args['data']))
                return NativeResult(deepcopy(table[args['id']]))
            return NativeResult(table.pop(args['id']))
        return invoke

    tools = []
    for entity in rows:
        operations = ('read',) if entity.startswith('hrms.') else (
            'findMany', 'createOne', 'deleteOne') if entity == 'twenty.note_target' else ('findOne', 'createOne', 'updateOne')
        for operation in operations:
            tools.append(Tool(entity + '.' + operation, entity.split('.')[0], entity, operation, entity,
                              {}, 'Native test operation', object_schema(additional=True), 'Native state',
                              operation in ('findOne', 'findMany', 'read'), handler(entity, operation)))
    directory = [{'entity': entity, 'business_ids': [business], 'identity': identity}
                 for entity, business, identity in [
                     ('twenty.company', 'venue_one', {'id': 'venue-uuid'}),
                     ('twenty.company', 'venue_two', {'id': 'other-uuid'}),
                     ('pretix.event', 'course_one', {'slug': 'event-one'}),
                     ('twenty.note', 'source_one', {'id': 'source-uuid'}),
                     ('hrms.employee', 'teacher_one', {'name': 'HR-001'}),
                     ('hrms.shift_assignment', 'duty_one', {'name': 'DUTY-001'})]]
    journal = CallJournal(tmp_path / 'calls.sqlite')
    owner = WorldTools('world-1', ToolRegistry(tools, catalog_revision='native'),
                       [Role('operator', (SimpleNamespace(application='twenty', scope={}),
                                          SimpleNamespace(application='hrms', scope={})), ('*',)),
                        Role('reader', (SimpleNamespace(application='twenty', scope={}),), ('*.findOne',))], journal)
    agent = owner.agent('author', 'operator')
    owner.configure_record_forms(specification, directory)
    yield owner, agent, rows, control
    owner.close()
    journal.close()


def booking():
    return dict(title='Room booking', course_id='course_one', supplier_id='venue_one', amount=180,
                currency='USD', due_date='2027-10-22', source_note_id='source_one',
                calendar_event_ids=['calendar-a', 'calendar-b'])


def call(agent, operation, values, request='test-1'):
    return agent.call_tool('records.room_booking.' + operation, values, request)


def test_create_serializes_and_links_only_supplied_record(world):
    owner, agent, rows, control = world
    result = call(agent, 'create', booking())
    assert result['status'] == 'succeeded'
    record = rows['twenty.note'][result['data']['record_id']]
    body = json.loads(record['bodyV2']['markdown'])
    assert body['supplier_id'] == 'venue_one' and body['source_note_id'] == 'source-uuid'
    assert body['amount'] == 180 and body['kind'] == 'cac_room_booking'
    assert list(rows['twenty.note_target'].values())[0]['targetCompanyId'] == 'venue-uuid'
    assert control['writes'] == [('twenty.note', 'createOne'), ('twenty.note_target', 'createOne')]
    assert all(r['actor'] == 'author' for r in owner.journal.records(owner.world_id))
    assert len(result['data']['steps']) == 6


def test_partial_receipt_replay_and_update_repairs_link_without_second_note(world):
    _, agent, rows, control = world
    control['fail_link'] = True
    first = call(agent, 'create', booking())
    assert first['status'] == 'partial' and first['data']['record_id']
    before = deepcopy(control['writes'])
    assert call(agent, 'create', booking())['replayed']
    assert control['writes'] == before
    control['fail_link'] = False
    repaired = call(agent, 'update', {**booking(), 'record_id': first['data']['record_id']}, 'repair')
    assert repaired['status'] == 'succeeded' and len(rows['twenty.note']) == 2
    assert len(rows['twenty.note_target']) == 1
    again = call(agent, 'update', {**booking(), 'record_id': first['data']['record_id']}, 'fresh-update')
    assert again['status'] == 'succeeded' and len(rows['twenty.note_target']) == 1


def test_unknown_write_never_becomes_success_or_retries(world):
    _, agent, _, control = world
    control['unknown_link'] = True
    result = call(agent, 'create', booking())
    assert result['status'] == 'unknown' and result['data']['record_id']
    assert 'Transport timeout' not in json.dumps(result)
    before = deepcopy(control['writes'])
    assert call(agent, 'create', booking())['replayed']
    assert control['writes'] == before


@pytest.mark.parametrize('change', [dict(supplier_id='unlisted'), dict(source_note_id='wrong-entity'),
                                  dict(calendar_event_ids=['calendar-a', 'missing']), dict(due_date='2027-02-30')])
def test_reference_and_date_errors_are_rejected_before_any_write(world, change):
    _, agent, _, control = world
    result = call(agent, 'create', {**booking(), **change})
    assert result['status'] == 'rejected' and not control['writes']


def test_update_cannot_overwrite_source_note(world):
    _, agent, rows, control = world
    before = deepcopy(rows)
    result = call(agent, 'update', {**booking(), 'record_id': 'source-uuid'})
    assert result['status'] == 'rejected' and result['error']['code'] == 'record_type_mismatch'
    assert rows == before and not control['writes']


def test_read_returns_values_and_update_relinks_changed_venue(world):
    _, agent, rows, _ = world
    first = call(agent, 'create', booking())
    identity = first['data']['record_id']
    current = call(agent, 'read', {'record_id': identity}, 'read')
    assert current['data']['values']['amount'] == 180
    changed = {**current['data']['values'], 'supplier_id': 'venue_two', 'record_id': identity}
    assert call(agent, 'update', changed, 'change')['status'] == 'succeeded'
    links = list(rows['twenty.note_target'].values())
    assert len(links) == 1 and links[0]['targetCompanyId'] == 'other-uuid'


def test_roles_do_not_gain_native_write_capabilities_and_receipts_stay_private(world):
    owner, agent, _, _ = world
    result = call(agent, 'create', booking())
    reader = owner.agent('reader', 'reader')
    assert not any(t.name.startswith('records.room_booking.') for t in reader.registry.tools())
    assert all(t.read_only for t in reader.registry.tools() if t.name.startswith('records.'))
    other = owner.agent('other', 'operator')
    assert sum(t.entity == 'twenty.record_form' for t in other.registry.tools()) == 9
    with pytest.raises(ToolError, match='No saved result'):
        other.read_result(result['call_id'])


def test_substitute_form_normalizes_public_employee_reference(world):
    _, agent, rows, control = world
    values = dict(title='Unpaid substitute follow-up', status='IN_PROGRESS', due_at='2027-10-31T23:59:00Z',
                  employee_id='teacher_one', duty_id='duty_one', amount=120, currency='USD', source_note_id='source_one')
    result = agent.call_tool('records.substitute_followup.create', values, 'follow')
    assert result['status'] == 'succeeded'
    record = rows['twenty.task'][result['data']['record_id']]
    assert record['status'] == 'IN_PROGRESS' and record['dueAt'] == values['due_at']
    assert json.loads(record['bodyV2']['markdown'])['employee_id'] == 'HR-001'
    assert control['writes'] == [('twenty.task', 'createOne')]


def test_handover_checks_task_entity_and_preserves_scoped_ids(world):
    _, agent, rows, control = world
    rows['twenty.task']['follow-id'] = {'id': 'follow-id'}
    values = dict(title='Handover', as_of='2027-10-08', room_booking_ids=['source-uuid'],
                  substitute_task_id='follow-id', transferred_order='ORDER',
                  pending_refunds=[dict(event='event', order='ORDER', refund_id='1')],
                  preserved_college=dict(bill_id=1, payment_id='2', credit_id=3))
    result = agent.call_tool('records.financial_handover.create', values, 'handover')
    assert result['status'] == 'succeeded'
    assert result['data']['values']['pending_refunds'][0]['refund_id'] == '1'
    bad = {**values, 'substitute_task_id': 'source-uuid'}
    before = len(control['writes'])
    result = agent.call_tool('records.financial_handover.create', bad, 'wrong-task')
    assert result['status'] == 'rejected' and len(control['writes']) == before


def test_shared_runtime_supports_explicit_form_specs():
    import io, tarfile
    with tarfile.open(fileobj=io.BytesIO(runtime_source()), mode='r:gz') as archive:
        prefix = 'mac_bench/environments/'
        assert prefix + 'tools/record_forms.py' in archive.getnames()
        assert b'configure_record_forms' in archive.extractfile(prefix + 'packaging/server.py').read()
