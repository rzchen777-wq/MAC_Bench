"""Compound tools preserve native effects, visibility, permissions and uncertainty."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from mac_bench.environments.tools import CallJournal, NativeResult, Role, Tool, ToolError, WorldTools
from mac_bench.environments.tools.contracts import object_schema
from mac_bench.environments.tools.registry import ToolRegistry


@pytest.fixture
def world(tmp_path):
    entities = ('note', 'task', 'person', 'calendar_event', 'calendar_channel',
                'calendar_event_participant', 'calendar_channel_event_association')
    rows = {'twenty.' + name: {} for name in entities}
    rows['twenty.person']['person-a'] = {'id': 'person-a'}
    rows['twenty.calendar_channel']['channel-a'] = {'id': 'channel-a', 'visibility': 'SHARE_EVERYTHING'}
    rows['twenty.calendar_channel']['private'] = {'id': 'private', 'visibility': 'METADATA'}
    control = {'writes': [], 'fail': None, 'unknown': None, 'normalize_dates': False}

    def handler(entity, op):
        def run(connection, args):
            table = rows[entity]
            if op == 'findOne':
                return NativeResult(deepcopy(table.get(args['filter']['id']['eq'])))
            if op == 'findMany':
                if any(not field.startswith(('edges.node.','pageInfo.')) for field in args.get('fields',[])):
                    raise ToolError('invalid_projection','Collection projections must start at edges.node or pageInfo.')
                match = lambda row: all(row.get(k) == v['eq'] for k, v in args['filter'].items())
                return NativeResult({'edges': [{'node': deepcopy(r)} for r in table.values() if match(r)],
                                     'pageInfo': {'hasNextPage': False}})
            if control['fail'] == entity:
                raise ToolError('native_rejected', 'The native operation was rejected.')
            control['writes'].append((entity, op))
            if control['unknown'] == entity:
                raise TimeoutError('private transport details')
            if op == 'createOne':
                identity = entity + '-' + str(len(table))
                table[identity] = {'id': identity, **deepcopy(args['data'])}
            else:
                identity = args['id']
                table[identity].update(deepcopy(args['data']))
            if entity == 'twenty.task' and control['normalize_dates'] and table[identity].get('dueAt'):
                table[identity]['dueAt'] = table[identity]['dueAt'].replace('00Z', '00.000Z')
            return NativeResult(deepcopy(table[identity]))
        return run

    tools = [Tool(entity + '.' + op, 'twenty', entity, op, entity, {}, 'Native test operation',
                  object_schema({'data': object_schema({'status': {'enum': ['TODO', 'IN_PROGRESS', 'DONE']},
                      'dueAt': {'type': ['string', 'null']}}, additional=True)}, additional=True)
                  if entity == 'twenty.task' and op in ('createOne', 'updateOne') else object_schema(additional=True),
                  'Native result', op.startswith('find'), handler(entity, op))
             for entity in rows for op in ('findOne', 'findMany', 'createOne', 'updateOne')]
    connection = SimpleNamespace(application='twenty', scope={})
    journal = CallJournal(tmp_path / 'calls.sqlite')
    owner = WorldTools('one-world', ToolRegistry(tools, catalog_revision='test'), [
        Role('operator', (connection,), ('*',)),
        Role('reader', (connection,), ('*.findOne', '*.findMany'))], journal)
    yield owner, owner.agent('agent', 'operator'), rows, control
    owner.close()
    journal.close()


def appointment():
    return {'title': 'A public appointment', 'startsAt': '2027-01-01T09:00:00Z',
            'endsAt': '2027-01-01T10:00:00Z', 'calendar_channel_id': 'channel-a',
            'participants': [{'personId': 'person-a', 'handle': 'a@example.org', 'isOrganizer': True}]}


def test_compute_to_typed_register_uses_own_saved_data_and_preserves_native_state(world):
    owner, agent, rows, control = world
    spec={'format':'mac_bench.registers.v1','registers':[{'id':'museum','title':'Museum Closeout',
        'entity':'twenty.note','schema':object_schema({'rows':{'type':'array','items':object_schema(
            {'id':{'type':'string'},'amount':{'type':'number'}},['id','amount'])}},['rows'])}]}
    owner.configure_data({'public/facts.json':{'rows':[{'id':'A','count':3,'rate':4}]}},spec)
    computed=agent.compute(sources={'t':{'resource':'public/facts.json','path':'/rows'}},
        sql="SELECT json_object('content',json_object('rows',json_group_array(json_object('id',id,'amount',count*rate)))) AS args FROM t",
        output='json_value',request_id='derive')
    assert computed['status']=='succeeded'
    reference={'result_ref':computed['result_ref'],'path':'/data/value'}
    result=agent.call_tool('registers.museum.create',arguments_ref=reference,request_id='save')
    assert result['status']=='succeeded'
    identity=result['data']['record_id']
    assert json.loads(rows['twenty.note'][identity]['bodyV2']['markdown'])=={'rows':[{'id':'A','amount':12}]}
    assert control['writes']==[('twenty.note','createOne')]
    assert agent.compute(sources={'t':{'resource':'public/facts.json','path':'/rows'}},
        sql="SELECT json_object('content',json_object('rows',json_group_array(json_object('id',id,'amount',count*rate)))) AS args FROM t",
        output='json_value',request_id='derive')['replayed']
    other=owner.agent('other','operator')
    with pytest.raises(ToolError):
        other.call_tool('registers.museum.create',arguments_ref=reference,request_id='steal')
    denied=agent.compute(sources={'t':{'resource':'private/initial.json'}},sql='SELECT * FROM t',request_id='private')
    assert denied['status']=='rejected'
    reader=owner.agent('reader','reader')
    assert 'registers.museum.read' in {t.name for t in reader.registry.tools()}
    assert 'registers.museum.create' not in {t.name for t in reader.registry.tools()}
    fresh=agent.call_tool('registers.museum.read',{'record_id':identity},'read-register')
    edited=agent.call_tool('registers.museum.update',{'record_id':identity,'expected_version':fresh['data']['content_version'],
        'content':{'rows':[{'id':'A','amount':13}]}},'edit-register')
    assert edited['status']=='succeeded'
    stale=agent.call_tool('registers.museum.update',{'record_id':identity,'expected_version':fresh['data']['content_version'],
        'content':{'rows':[{'id':'A','amount':99}]}},'stale-register')
    assert stale['status']=='rejected' and stale['error']['code']=='stale_record_version'
    with pytest.raises(ToolError):
        agent.call_tool('registers.museum.create',{'content':{'rows':[{'id':'A','amount':'twelve'}]}},'bad-format')


def call(agent, name, arguments, request='one'):
    return agent.call_tool(name, arguments, request)


def test_json_note_avoids_double_encoding_and_does_not_do_other_work(world):
    owner, agent, rows, control = world
    content = {'rows': [{'business_id': 'CASE1', 'amount': 12.5, 'confirmed': False}]}
    args = {'title': 'Business handover', 'content': content}
    result = call(agent, 'records.note.create', args)
    assert result['status'] == 'succeeded'
    identity = result['data']['record_id']
    assert json.loads(rows['twenty.note'][identity]['bodyV2']['markdown']) == content
    assert control['writes'] == [('twenty.note', 'createOne')]
    assert call(agent, 'records.note.create', args)['replayed']
    assert len(rows['twenty.note']) == 1
    read = call(agent, 'records.note.read', {'record_id': identity}, 'read')
    assert read['data']['content'] == content
    with pytest.raises(ToolError):
        call(agent, 'records.note.create', {**args, 'content': json.dumps(content)}, 'encoded')
    assert all(r['actor'] == 'agent' for r in owner.journal.records(owner.world_id))


def test_create_links_visible_calendar_and_participant_then_update_is_idempotent(world):
    _, agent, rows, control = world
    args = appointment()
    result = call(agent, 'calendar.appointment.create', args)
    assert result['status'] == 'succeeded'
    identity = result['data']['event_id']
    assert len(rows['twenty.calendar_channel_event_association']) == 1
    assert result['data']['participants'][0]['personId'] == 'person-a'
    assert result['data']['calendar_links'][0]['calendarChannelId'] == 'channel-a'
    assert call(agent, 'calendar.appointment.create', args)['replayed']
    for i in range(2):
        updated = call(agent, 'calendar.appointment.update', {'event_id': identity, **args}, 'update-' + str(i))
        assert updated['status'] == 'succeeded'
    assert len(rows['twenty.calendar_event']) == len(rows['twenty.calendar_event_participant']) == 1
    assert len(rows['twenty.calendar_channel_event_association']) == 1


def test_task_readback_accepts_native_timestamp_normalization(world):
    _, agent, _, control = world
    control['normalize_dates'] = True
    result = call(agent, 'records.task.create', {'title': 'Follow-up', 'content': {'approved': False},
        'status': 'TODO', 'dueAt': '2027-01-20T10:00:00Z'})
    assert result['status'] == 'succeeded'
    assert result['data']['dueAt'] == '2027-01-20T10:00:00.000Z'


@pytest.mark.parametrize('change', [
    {'startsAt': '2027-01-01T09:00:00'}, {'endsAt': '2027-01-01T08:00:00Z'},
    {'calendar_channel_id': 'private'},
    {'participants': [{'personId': 'missing', 'handle': 'x', 'isOrganizer': False}]},
    {'participants': [appointment()['participants'][0]] * 2},
])
def test_invalid_schedule_or_references_have_no_writes(world, change):
    _, agent, _, control = world
    result = call(agent, 'calendar.appointment.create', {**appointment(), **change})
    assert result['status'] == 'rejected' and not control['writes']


def test_failed_link_returns_event_id_and_update_repairs_without_duplicate_event(world):
    _, agent, rows, control = world
    control['fail'] = 'twenty.calendar_channel_event_association'
    args = appointment()
    result = call(agent, 'calendar.appointment.create', args)
    assert result['status'] == 'partial' and result['data']['event_id']
    assert call(agent, 'calendar.appointment.create', args)['replayed']
    control['fail'] = None
    repair = call(agent, 'calendar.appointment.update', {'event_id': result['data']['event_id'], **args}, 'repair')
    assert repair['status'] == 'succeeded' and len(rows['twenty.calendar_event']) == 1


def test_unknown_link_is_not_retried_and_no_private_transport_text_escapes(world):
    _, agent, _, control = world
    control['unknown'] = 'twenty.calendar_channel_event_association'
    result = call(agent, 'calendar.appointment.create', appointment())
    assert result['status'] == 'unknown' and result['data']['event_id']
    before = list(control['writes'])
    assert call(agent, 'calendar.appointment.create', appointment())['replayed']
    assert control['writes'] == before and 'private transport' not in json.dumps(result)


def test_reader_does_not_gain_writes_or_other_actors_receipts(world):
    owner, agent, _, _ = world
    reader = owner.agent('reader', 'reader')
    assert all(t.read_only for t in reader.registry.tools())
    assert not any(t.name.endswith('.create') for t in reader.registry.tools())
    result = call(agent, 'records.note.create', {'title': 'x', 'content': {}})
    with pytest.raises(ToolError):
        reader.read_result(result['call_id'])


def register(world, content=None):
    _, agent, rows, control = world
    content = content or {'programme': 'existing source reference', 'rows': [
        {'id': f'G{i:03d}', 'status': 'pending', 'preserved': {'consent': f'CONS-{i}'}} for i in range(160)]}
    result = call(agent, 'records.note.create', {'title': 'Large business register', 'content': content}, 'initialize')
    identity = result['data']['record_id']
    rows['twenty.note'][identity]['unrelated_relation'] = ['keep']
    control['writes'].clear()
    return identity


def read_rows(agent, identity, request='read-rows', **extra):
    return call(agent, 'records.note.read_rows', {'record_id': identity, 'key_field': 'id', **extra}, request)


def test_local_edit_preserves_other_159_rows_and_omitted_fields(world):
    owner, agent, rows, control = world
    identity = register(world)
    before = deepcopy(rows['twenty.note'][identity])
    read = read_rows(agent, identity, row_ids=['G001'])
    assert len(read['data']['items']) == 1 and read['data']['total_rows'] == 160
    patch = {'record_id': identity, 'key_field': 'id', 'expected_version': read['data']['content_version'],
             'updates': [{'key': 'G001', 'set': {'status': 'reserved'}}]}
    edited = call(agent, 'records.note.patch_rows', patch, 'edit-row')
    assert edited['status'] == 'succeeded' and len(edited['data']['changed_rows']) == 1
    old = json.loads(before['bodyV2']['markdown'])
    expected = deepcopy(old)
    expected['rows'][1]['status'] = 'reserved'
    assert json.loads(rows['twenty.note'][identity]['bodyV2']['markdown']) == expected
    assert rows['twenty.note'][identity]['title'] == before['title']
    assert rows['twenty.note'][identity]['unrelated_relation'] == ['keep']
    assert control['writes'] == [('twenty.note', 'updateOne')]
    assert call(agent, 'records.note.patch_rows', patch, 'edit-row')['replayed']
    assert len(control['writes']) == 1
    assert all(r['actor'] == 'agent' for r in owner.journal.records(owner.world_id))


def test_register_two_agents_stale_snapshot_rejected_without_lost_update(world):
    owner, agent, rows, control = world
    identity = register(world)
    other = owner.agent('second-agent', 'operator')
    v = read_rows(agent, identity)['data']['content_version']
    assert read_rows(other, identity)['data']['content_version'] == v
    args = {'record_id': identity, 'key_field': 'id', 'expected_version': v,
            'updates': [{'key': 'G001', 'set': {'status': 'reserved'}}]}
    assert call(agent, 'records.note.patch_rows', args, 'first-editor')['status'] == 'succeeded'
    rejected = call(other, 'records.note.patch_rows', {**args, 'updates': [{'key': 'G002', 'set': {'status': 'ready'}}]}, 'stale-editor')
    assert rejected['status'] == 'rejected' and rejected['error']['code'] == 'stale_record_version'
    assert control['writes'] == [('twenty.note', 'updateOne')]
    fresh = read_rows(other, identity, 'refresh')['data']['content_version']
    assert call(other, 'records.note.patch_rows', {**args, 'expected_version': fresh,
        'updates': [{'key': 'G002', 'set': {'status': 'ready'}}]}, 'fresh-editor')['status'] == 'succeeded'
    result = json.loads(rows['twenty.note'][identity]['bodyV2']['markdown'])['rows']
    assert result[1]['status'] == 'reserved' and result[2]['status'] == 'ready'


def test_register_pagination_is_bounded_and_bound_to_live_content(world):
    _, agent, _, _ = world
    identity = register(world)
    first = read_rows(agent, identity, limit=10)['data']
    assert len(first['items']) == 10 and first['next_cursor'] and first['total'] == 160
    second = read_rows(agent, identity, 'second-page', limit=10, cursor=first['next_cursor'])['data']
    assert second['items'][0]['id'] == 'G010'
    call(agent, 'records.note.patch_rows', {'record_id': identity, 'key_field': 'id',
        'expected_version': first['content_version'], 'delete_ids': ['G001']}, 'delete')
    stale = read_rows(agent, identity, 'stale-page', cursor=first['next_cursor'])
    assert stale['status'] == 'rejected' and stale['error']['code'] == 'invalid_row_pagination'


@pytest.mark.parametrize('edit,code', [
    ({'updates': [{'key': 'missing', 'set': {'status': 'x'}}]}, 'row_not_found'),
    ({'updates': [{'key': 'G001', 'set': {'id': 'G002'}}]}, 'row_key_change'),
    ({'updates': [{'key': 'G001', 'set': {'status': 'x'}}], 'delete_ids': ['G001']}, 'overlapping_row_edits'),
    ({'insert_rows': [{'id': 'G001'}]}, 'row_already_exists'),
    ({'insert_rows': [{'status': 'ready'}]}, 'missing_row_key'),
    ({'delete_ids': ['missing']}, 'row_not_found'),
    ({'insert_rows': [{'id': ''}]}, 'invalid_row_key'),
])
def test_invalid_row_batch_is_rejected_before_any_write(world, edit, code):
    _, agent, rows, control = world
    identity = register(world)
    before = deepcopy(rows['twenty.note'][identity])
    v = read_rows(agent, identity)['data']['content_version']
    # A valid first update must not be applied when another edit is invalid.
    args = {'updates': [{'key': 'G003', 'set': {'status': 'ready'}}], **edit}
    result = call(agent, 'records.note.patch_rows', {'record_id': identity, 'key_field': 'id', 'expected_version': v, **args}, 'bad-batch')
    assert result['status'] == 'rejected' and result['error']['code'] == code
    assert not control['writes'] and rows['twenty.note'][identity] == before


def test_register_explicit_insert_delete_and_noop(world):
    _, agent, rows, control = world
    identity = register(world, {'scope': 'preserve', 'rows': [{'id': 'A', 'value': 1}, {'id': 'B', 'value': 2}]})
    v = read_rows(agent, identity)['data']['content_version']
    result = call(agent, 'records.note.patch_rows', {'record_id': identity, 'key_field': 'id', 'expected_version': v,
        'updates': [{'key': 'A', 'set': {'value': 3}}], 'insert_rows': [{'id': 'C', 'value': 4}], 'delete_ids': ['B']}, 'explicit-edits')
    assert result['status'] == 'succeeded'
    assert json.loads(rows['twenty.note'][identity]['bodyV2']['markdown']) == {'scope': 'preserve', 'rows': [{'id': 'A', 'value': 3}, {'id': 'C', 'value': 4}]}
    n = len(control['writes'])
    noop = call(agent, 'records.note.patch_rows', {'record_id': identity, 'key_field': 'id',
        'expected_version': result['data']['content_version'], 'updates': [{'key': 'A', 'set': {'value': 3}}]}, 'noop')
    assert noop['status'] == 'succeeded' and not noop['data']['changed'] and len(control['writes']) == n


@pytest.mark.parametrize('content', [{'rows':[{'id':'A'},{'id':'A'}]}, {'rows':[{'other':'A'}]}, {'rows':[{'id':True}]}, {'rows':{}}, {'other':[]}])
def test_bad_register_structure_never_becomes_silent_row_selection(world, content):
    _, agent, _, control = world
    identity = register(world, content)
    assert read_rows(agent, identity)['status'] == 'rejected' and not control['writes']


def test_unknown_row_write_is_not_retried_and_reader_cannot_edit(world):
    owner, agent, _, control = world
    identity = register(world)
    reader = owner.agent('reader', 'reader')
    assert read_rows(reader, identity)['status'] == 'succeeded'
    assert not any(t.name.endswith('patch_rows') for t in reader.registry.tools())
    v = read_rows(agent, identity)['data']['content_version']
    args = {'record_id': identity, 'key_field': 'id', 'expected_version': v,
        'updates': [{'key': 'G001', 'set': {'status': 'ready'}}]}
    control['unknown'] = 'twenty.note'
    result = call(agent, 'records.note.patch_rows', args, 'uncertain-edit')
    assert result['status'] == 'unknown' and result['data']['record_id'] == identity
    before = list(control['writes'])
    assert call(agent, 'records.note.patch_rows', args, 'uncertain-edit')['replayed']
    assert control['writes'] == before and 'private transport' not in json.dumps(result)


def test_task_row_edit_preserves_status_due_date_and_handles_empty_register(world):
    _, agent, rows, _ = world
    result = call(agent, 'records.task.create', {'title': 'Task register', 'content': {'rows': []},
        'status': 'TODO', 'dueAt': '2027-01-20T10:00:00Z'}, 'empty-task')
    identity = result['data']['record_id']
    read = call(agent, 'records.task.read_rows', {'record_id': identity, 'key_field': 'business_id'}, 'read-empty')
    edited = call(agent, 'records.task.patch_rows', {'record_id': identity, 'key_field': 'business_id',
        'expected_version': read['data']['content_version'], 'insert_rows': [{'business_id': 'X', 'approved': False}]}, 'insert')
    assert edited['status'] == 'succeeded'
    assert rows['twenty.task'][identity]['status'] == 'TODO' and rows['twenty.task'][identity]['dueAt'] == '2027-01-20T10:00:00Z'


@pytest.mark.parametrize('array,key', [('volunteers','id'),('records','id'),('applications','id'),
    ('results','business_id'),('sessions','session_id'),('restaurants','restaurant_id'),('households','id'),
    ('students','id'),('traders','id'),('children','id'),('placements','id'),('work','id')])
def test_task_defined_register_array_is_edited_without_renaming_or_losing_other_content(world,array,key):
    _, agent, rows, _ = world
    content = {array:[{key:'A','value':1},{key:'B','value':2}], 'other_register':[{'id':'preserve'}], 'budget':100}
    identity = register(world,content)
    selected = read_rows(agent,identity,array_path='/'+array,key_field=key) if key=='id' else call(agent,'records.note.read_rows',
        {'record_id':identity,'key_field':key,'array_path':'/'+array},'select')
    assert selected['status']=='succeeded'
    patch={'record_id':identity,'key_field':key,'array_path':'/'+array,'expected_version':selected['data']['content_version'],
        'updates':[{'key':'A','set':{'value':3}}],'insert_rows':[{key:'C','value':4}],'delete_ids':['B']}
    result=call(agent,'records.note.patch_rows',patch,'edit-real-format')
    assert result['status']=='succeeded' and result['data']['array_path']=='/'+array
    expected={**content,array:[{key:'A','value':3},{key:'C','value':4}]}
    assert json.loads(rows['twenty.note'][identity]['bodyV2']['markdown'])==expected


def test_nested_and_escaped_array_paths_preserve_other_arrays_and_bind_cursors(world):
    _,agent,rows,_=world
    content={'envelope':{'a/b~c':[{'id':'A'},{'id':'B'}]},'rows':[{'id':'X'},{'id':'Y'}]}
    identity=register(world,content)
    selected=read_rows(agent,identity,array_path='/envelope/a~1b~0c',limit=1)['data']
    wrong=read_rows(agent,identity,'wrong-array',array_path='/rows',limit=1,cursor=selected['next_cursor'])
    assert wrong['status']=='rejected'
    result=call(agent,'records.note.patch_rows',{'record_id':identity,'key_field':'id','array_path':'/envelope/a~1b~0c',
        'expected_version':selected['content_version'],'updates':[{'key':'A','set':{'value':3}}]},'nested-edit')
    assert result['status']=='succeeded'
    saved=json.loads(rows['twenty.note'][identity]['bodyV2']['markdown'])
    assert saved['rows']==content['rows'] and saved['envelope']['a/b~c'][0]['value']==3


@pytest.mark.parametrize('path',['/missing','/metadata','/rows/0','/bad~2key'])
def test_invalid_array_path_never_changes_the_record(world,path):
    _,agent,_,control=world
    identity=register(world,{'rows':[{'id':'A'}],'metadata':{}})
    assert read_rows(agent,identity,array_path=path)['status']=='rejected'
    assert not control['writes']


def test_large_compute_starts_at_rows_and_pages_saved_receipt(world):
    owner, agent, _, control = world
    values = [{'id': i, 'description': 'An ordinary business record. ' * 12} for i in range(100)]
    result = agent.compute(sources={'t': {'rows': values}}, sql='SELECT * FROM t', request_id='large-table')
    assert not result['data_complete']
    assert result['columns'] == ['description', 'id'] or result['columns'] == ['id', 'description']
    assert result['row_count'] == 100
    page = result['result_page']
    assert page['path'] == '/data/rows' and page['items'][0]['value']['id'] == 0
    collected = [item['value'] for item in page['items']]
    while page['next_cursor']:
        page = agent.read_result(result['result_ref'], path=page['path'], cursor=page['next_cursor'])
        collected.extend(item['value'] for item in page['items'])
    assert collected == values and not control['writes']
    replay = agent.compute(sources={'t': {'rows': values}}, sql='SELECT * FROM t', request_id='large-table')
    assert replay['replayed'] and replay['result_ref'] == result['result_ref']


def test_unknown_tool_suggestions_stay_in_actor_registry(world):
    owner, _, _, _ = world
    reader = owner.agent('reader', 'reader')
    with pytest.raises(ToolError) as exc:
        reader.registry.get('twenty.note.findOn')
    assert 'twenty.note.findOne' in str(exc.value)
    assert 'createOne' not in str(exc.value)
    assert 'list_tools' in str(exc.value)
