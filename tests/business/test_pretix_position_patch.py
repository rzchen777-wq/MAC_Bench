"""The public PATCH contract matches the native booking-change handler."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac_bench.environments.catalog.entities import EntityCatalog
from mac_bench.environments.tools.build import build_registry
from mac_bench.environments.tools.contracts import ToolError, validate


@pytest.fixture(scope='module')
def registry():
    import os
    from mac_bench.runtime.paths import repository_root
    task_root = Path(os.environ.get('MAC_BENCH_TASK_ROOT', repository_root()/'data/tasks'))
    path = task_root/'op26_business_finance_008/environment/runtime/catalog/entity/resources/catalog.json.gz'
    if not path.is_file():
        pytest.skip('Install task data to check native application contracts')
    catalog = EntityCatalog.load(path)
    bindings = [b for entity in ('pretix.order_position', 'pretix.item')
                for v in catalog.variants(entity) for b in v['bindings']]
    return build_registry(catalog, bindings=bindings)


@pytest.fixture(scope='module')
def tool(registry):
    return next(t for t in registry.tools() if t.name == 'pretix.order_position.partial_update_organizer_event_pk')


def test_booking_and_attendee_changes_are_discoverable_and_sent_together(tool):
    args = {'path': {'organizer': 'org', 'event': 'training', 'pk': 27},
            'body': {'price': '30.00', 'subevent': 2, 'attendee_email': 'volunteer@example.test'}}
    validate(tool.input_schema, args)
    body = tool.input_schema['$defs'][tool.input_schema['properties']['body']['$ref'].split('/')[-1]]
    assert {'price', 'subevent', 'attendee_email'} <= body['properties'].keys()
    requests = []

    class Transport:
        def request_response(self, method, path, body):
            requests.append((method, path, body))
            return SimpleNamespace(status=200, body={'id': 27, **body})

    result = tool.handler(SimpleNamespace(settings={}, transport=Transport()), args)
    assert requests == [('PATCH', 'api/v1/organizers/org/events/training/orderpositions/27/', args['body'])]
    assert result.data['subevent'] == 2


@pytest.mark.parametrize('body', [{'pricee': '30.00'}, {'positionid': 1}, {'subevent': 'week two'}])
def test_typos_read_only_fields_and_wrong_reference_types_are_rejected(tool, body):
    with pytest.raises(ToolError):
        validate(tool.input_schema, {'path': {'organizer': 'org', 'event': 'event', 'pk': 27}, 'body': body})


def test_partial_update_does_not_require_unmodified_booking_fields(tool):
    validate(tool.input_schema, {'path': {'organizer': 'org', 'event': 'event', 'pk': 27},
                                 'body': {'attendee_email': 'new@example.test'}})


def test_item_listing_is_json_and_keeps_native_pagination(registry):
    tool = next(t for t in registry.tools() if t.entity == 'pretix.item' and t.operation == 'list@organizer.event')
    args = {'path': {'organizer':'org','event':'training'}, 'query': {'page':1,'page_size':2}}
    validate(tool.input_schema,args)
    calls=[]
    class Transport:
        def request_response(self,method,path,body):
            calls.append((method,path,body))
            return SimpleNamespace(status=200,body={'count':3,'results':[{'id':1},{'id':2}],
                'next':'https://pretix.test/api/v1/organizers/org/events/training/items/?page=2&page_size=2','previous':None})
    result=tool.handler(SimpleNamespace(settings={},transport=Transport()),args)
    assert calls == [('GET','api/v1/organizers/org/events/training/items/?page=1&page_size=2',None)]
    assert result.data['count']==3
    assert result.pagination['next_query']=={'page':2,'page_size':2}
    assert any(e['reason']=='binary_endpoint' and e['operation'].startswith('download@') for e in registry.exclusions)


def test_create_position_retains_required_fields_and_optional_attendee_fields(registry):
    tool=next(t for t in registry.tools() if t.entity=='pretix.order_position' and t.operation=='create@organizer.event')
    args={'path':{'organizer':'org','event':'training'},
          'body':{'order':'ABCDE','item':3,'attendee_name':'Test attendee','attendee_email':'test@example.test'}}
    validate(tool.input_schema,args)
    for missing in ('order','item'):
        invalid={**args,'body':{k:v for k,v in args['body'].items() if k!=missing}}
        with pytest.raises(ToolError):validate(tool.input_schema,invalid)
    for invalid_body in ({**args['body'],'item':'wrong'}, {**args['body'],'attendee_emali':'x'}, {**args['body'],'positionid':10}):
        with pytest.raises(ToolError):validate(tool.input_schema,{**args,'body':invalid_body})
    calls=[]
    class Transport:
        def request_response(self,method,path,body):
            calls.append((method,path,body));return SimpleNamespace(status=201,body={'id':37,**body})
    result=tool.handler(SimpleNamespace(settings={},transport=Transport()),args)
    assert calls==[('POST','api/v1/organizers/org/events/training/orderpositions/',args['body'])]
    assert result.data['id']==37
