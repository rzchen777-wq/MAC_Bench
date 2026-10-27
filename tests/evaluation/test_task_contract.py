import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import pytest
from mac_bench.evaluation.tasks import (
    AgentEnvironment, ToolDefinition, task_record, load_catalog, open_task,
)
from mac_bench.environments.packaging.bundle import seal_bundle
from mac_bench.runtime.sandbox.base import CommandResult


def bundle(tmp_path, category, name='task'):
    root=tmp_path/name; (root/'public').mkdir(parents=True); (root/'private').mkdir()
    (root/'public/task.txt').write_text('Complete the public task.')
    (root/'private/secret.txt').write_text('PRIVATE ANSWER')
    meta={'world_family':category} if category in {'platform_data','developer_release','product_application'} else {'scenario_family':category,'tool_api_version':2}
    (root/'private/initial.json').write_text('{"value":0}')
    return seal_bundle(root,world_id=name,public_files=['public/task.txt'],metadata=meta)


class CodeWorld:
    def __init__(self):
        self.changed=False; self.closed=False; self.commands=[]; self.graded=[]
    def open(self):self.closed=False
    def close(self):self.closed=True
    def reset(self):self.changed=False
    def execute(self, command, *, timeout):
        self.commands.append((command,timeout));self.changed=True
        return CommandResult(command,0,'updated',.01)
    def export_submission(self,target):target.write_text('changed' if self.changed else 'starter');return target
    def grade_submission(self,target):
        self.graded.append(target.read_text());ok=target.read_text()=='changed'
        return {'status':'passed' if ok else 'failed','task_success':ok,'gc':float(ok)}


@pytest.mark.parametrize('category',['business_finance','service_administration','project_content_operations','product_application','platform_data','developer_release'])
def test_catalogue_classification_and_public_boundary(tmp_path,category):
    b=bundle(tmp_path,category); r=task_record(b.root)
    assert r.category==category
    if category in {'product_application','platform_data','developer_release'}:
        task=open_task(r,tmp_path/'runtime',world=CodeWorld())
    else:
        task=open_task(r,tmp_path/'runtime',owner=SimpleNamespace())
    public=json.dumps(task.view.as_dict())
    assert 'PRIVATE ANSWER' not in public and str(b.root) not in public and 'evaluate' not in [t.name for t in task.view.tools]
    assert {t.name for t in task.view.tools}==({'terminal'} if category in {'product_application','platform_data','developer_release'} else {'list_apps','list_tools','describe_tool','call_tool','read_result','query_result','list_resources','read_resource','query_resource','search_resource'})


def test_catalogue_rejects_revision_duplicates_and_category_conflict(tmp_path):
    b=bundle(tmp_path,'platform_data');r=task_record(b.root)
    with pytest.raises(ValueError):task_record(b.root,category='developer_release')
    with pytest.raises(ValueError):task_record(b.root,revision='changed')
    row={'task_id':'alias','world_revision':r.world_revision,'category':r.category,'split':'test','bundle':'task'}
    p=tmp_path/'catalog.json';p.write_text(json.dumps({'format':'collaboration.tasks.v1','tasks':[row]}))
    assert load_catalog(p)[0].task_id=='alias'
    p.write_text(json.dumps({'format':'collaboration.tasks.v1','tasks':[row,dict(row,task_id='other')]}))
    with pytest.raises(ValueError,match='Duplicate'):load_catalog(p)


def test_tool_schema_is_independent_and_invalid_calls_do_not_dispatch():
    calls=[];schema={'type':'object','properties':{'value':{'type':'integer'}},'required':['value'],'additionalProperties':False}
    definition=ToolDefinition('set','Set a value',schema);schema['required'].clear()
    definition.input_schema['required'].clear()
    env=AgentEnvironment([definition],lambda n,a:calls.append(a) or {'ok':True})
    env.begin(admit_call=lambda:None,event=lambda e:None)
    assert env.call('set',{})['error']['code']=='invalid_arguments'
    assert env.call('set',{'value':float('nan')})['error']['code']=='invalid_arguments'
    assert env.call('missing',{})['error']['code']=='unknown_tool'
    assert env.call('set',{'value':3})=={'ok':True} and calls==[{'value':3}]
    env.end()
    with pytest.raises(RuntimeError,match='closed'):env.call('set',{'value':4})


def test_end_drains_admitted_calls_before_capture():
    started,release,ended=Event(),Event(),Event()
    definition=ToolDefinition('work','Work',{'type':'object'})
    def dispatch(n,a):started.set();assert release.wait(2);return {'ok':True}
    env=AgentEnvironment([definition],dispatch);env.begin(admit_call=lambda:None,event=lambda e:None)
    with ThreadPoolExecutor(2) as pool:
        call=pool.submit(env.call,'work',{});assert started.wait(2)
        stop=pool.submit(lambda:(env.end(),ended.set()))
        assert not ended.wait(.05)
        release.set();call.result();stop.result();assert ended.is_set()
    with pytest.raises(RuntimeError):env.call('work',{})


def test_engineering_submission_is_frozen_and_reset_required(tmp_path):
    b=bundle(tmp_path,'platform_data');world=CodeWorld();task=open_task(task_record(b.root),tmp_path/'runtime',world=world)
    task.open();task.begin_episode('episode')
    with pytest.raises(RuntimeError):task.evaluate()
    task.environment.call('terminal',{'command':'printf edited','timeout':7})
    assert len(world.commands) == 1 and world.commands[0][1] == 22
    import shlex
    invocation = shlex.split(world.commands[0][0])
    assert invocation[3:5] == ['printf edited', '/work/project']
    assert invocation[6] == '7'  # Public deadline enforced before provider grace.
    assert '/work/tool-output/' in world.commands[0][0]
    task.end_episode();world.changed=False
    assert task.evaluate()['task_success'] and world.graded==['changed']
    with pytest.raises(RuntimeError):task.environment.call('terminal',{'command':'pwd'})
    with pytest.raises(RuntimeError,match='Reset'):task.begin_episode('again')
    task.reset();task.begin_episode('again');task.end_episode();assert task.evaluate()['task_success'] is False
    task.close();assert world.closed


def test_public_inspection_does_not_construct_provider(tmp_path,monkeypatch):
    import mac_bench.runtime.sandbox.modal as modal_backend
    def forbidden(*a,**kw):raise AssertionError('Inspection connected to provider')
    monkeypatch.setattr(modal_backend.ModalSandboxBackend,'__init__',forbidden)
    for category in ['business_finance','platform_data']:
        b=bundle(tmp_path,category,name=category)
        handle=open_task(task_record(b.root),tmp_path/'runtime')
        assert handle.view.instruction and not handle.is_open
        handle.close()


@pytest.mark.parametrize('with_tutorial', [False, True])
def test_operational_tutorial_is_discoverable_on_demand_without_private_material(tmp_path, with_tutorial):
    root = tmp_path / 'world'
    (root / 'public').mkdir(parents=True)
    (root / 'private').mkdir()
    (root / 'public/task.txt').write_text('Complete the public task.')
    (root / 'private/initial.json').write_text('{}')
    (root / 'private/answer.txt').write_text('PRIVATE ANSWER')
    public = ['public/task.txt']
    tutorial = 'Independent museum tutorial.\n' * 400
    if with_tutorial:
        (root / 'public/tool_tutorial.md').write_text(tutorial)
        public.append('public/tool_tutorial.md')
    b = seal_bundle(root, world_id='task', public_files=public,
                    metadata={'scenario_family': 'service_administration', 'tool_api_version': 2})
    session = SimpleNamespace(observe=lambda: {}, start_episode=lambda i: None, stop_episode=lambda: None)
    task = open_task(task_record(b.root), tmp_path / 'runtime', session=session)
    assert ('public/tool_tutorial.md' in task.view.interaction_instructions) == with_tutorial
    assert tutorial not in task.view.interaction_instructions
    task.open(); task.begin_episode('tutorial-read')
    call = task.environment.call
    names = {r['resource'] for r in call('list_resources', {})['resources']}
    assert ('public/tool_tutorial.md' in names) == with_tutorial
    if with_tutorial:
        text = ''; offset = 0
        while True:
            part = call('read_resource', {'resource': 'public/tool_tutorial.md', 'offset': offset})
            text += part['text']
            if part['next_offset'] is None: break
            offset = part['next_offset']
        assert text == tutorial
    else:
        assert call('read_resource', {'resource': 'public/tool_tutorial.md'})['error']['code'] == 'unknown_resource'
    assert call('read_resource', {'resource': 'private/answer.txt'})['error']['code'] == 'unknown_resource'
    task.end_episode(); task.close()


def test_large_public_data_is_on_demand_and_private_paths_are_inaccessible(tmp_path):
    root = tmp_path / 'world'
    (root / 'public').mkdir(parents=True)
    (root / 'private').mkdir()
    text = 'Read these complete requirements.\n' * 600
    identities = {f'person:P{i}': {'identity': {'id': f'NATIVE-{i}'}} for i in range(500)}
    (root / 'public/task.txt').write_text(text)
    (root / 'public/identities.json').write_text(json.dumps(identities))
    (root / 'private/initial.json').write_text('{}')
    (root / 'private/secret.txt').write_text('PRIVATE ANSWER')
    b = seal_bundle(root, world_id='task', public_files=['public/task.txt', 'public/identities.json'],
                    metadata={'scenario_family': 'business_finance', 'tool_api_version': 2})
    session = SimpleNamespace(observe=lambda: {}, start_episode=lambda i: None, stop_episode=lambda: None)
    task = open_task(task_record(b.root), tmp_path / 'runtime', session=session)
    assert len(task.view.instruction) < 1000 and 'NATIVE-499' not in task.view.instruction
    task.open(); task.begin_episode('episode')
    call = task.environment.call
    listing = call('list_resources', {})
    assert {r['resource'] for r in listing['resources']} == set(b.manifest['public_files'])
    collected = ''; offset = 0
    while True:
        part = call('read_resource', {'resource': 'public/task.txt', 'offset': offset})
        collected += part['text']
        if part['next_offset'] is None:
            break
        offset = part['next_offset']
    assert collected == text
    exact = call('read_resource', {'resource': 'public/identities.json', 'path': '/person:P499'})
    assert next(i['value'] for i in exact['items'] if i['key'] == 'identity')['id'] == 'NATIVE-499'
    queried = call('query_resource', {'resource': 'public/identities.json',
        'keys': ['person:P0', 'person:P499'], 'fields': {'id': '/identity/id'}})
    assert [r['values']['id'] for r in queried['rows']] == ['NATIVE-0', 'NATIVE-499']
    assert queried['total_matched'] == 2
    counted = call('query_resource', {'resource': 'public/identities.json', 'limit': 3,
        'aggregates': [{'name': 'count', 'op': 'count'}], 'fields': {'id': '/identity/id'}})
    assert len(counted['rows']) == 3 and counted['aggregates']['count']['value'] == 500
    assert counted['next_cursor']
    search = call('search_resource', {'resource': 'public/task.txt', 'query': 'complete', 'limit': 1})
    assert search['matches'][0]['match_offset'] == text.index('complete')
    assert search['next_cursor']
    continued = call('search_resource', {'resource': 'public/task.txt', 'query': 'complete',
        'limit': 1, 'cursor': search['next_cursor']})
    assert continued['matches'][0]['match_offset'] == text.index('complete', text.index('complete') + 1)
    for resource in ['private/secret.txt', '../private/secret.txt', str(root / 'private/secret.txt')]:
        assert call('read_resource', {'resource': resource})['error']['code'] == 'unknown_resource'
        assert call('query_resource', {'resource': resource})['error']['code'] == 'unknown_resource'
        assert call('search_resource', {'resource': resource, 'query': 'ANSWER'})['error']['code'] == 'unknown_resource'
    task.end_episode(); task.close()


def test_engineering_loader_keeps_support_files_out_of_task_document(tmp_path, monkeypatch):
    from mac_bench.environments.engineering.product import runtime
    root = tmp_path / 'world'
    (root / 'public').mkdir(parents=True)
    (root / 'public/task.txt').write_text('Implement the documented feature.')
    support = b'LARGE SUPPORT DATA\n' * 1000
    (root / 'public/fixtures.txt').write_bytes(support)
    writes = {}
    class Backend:
        def write_file(self, sandbox, *, path, content):
            assert isinstance(content, bytes)
            writes[path] = content
    backend = Backend()
    class Runtime:
        def __init__(self):
            self.sandbox = None
            self.backend = backend
        def open(self, **kwargs):
            self.sandbox = object()
        def install_source(self, path):
            pass
        def build(self):
            return SimpleNamespace(exit_code=0)
        def close(self):
            self.sandbox = None
    monkeypatch.setattr(runtime, 'modal_backend', lambda *a, **kw: backend)
    world = object.__new__(runtime.LoadedProductWorld)
    world.runtime = Runtime()
    world.bundle = SimpleNamespace(root=root, manifest={'public_files': ['public/task.txt', 'public/fixtures.txt']})
    world.meta = {'initial_snapshot': 'snapshot', 'workspace_archive': 'private/starter.tar'}
    world._sealed_environment = {}  # Fixture bypasses the normal constructor.
    world.environment = {'image': 'python:3.12-slim', 'setup': 'true'}
    world.open()
    assert writes['/work/public/fixtures.txt'] == support
    assert b'LARGE SUPPORT DATA' not in writes['/work/TASK.md']
    assert b'/work/public/fixtures.txt' in writes['/work/TASK.md']
    assert b'Implement the documented feature.' in writes['/work/TASK.md']


def test_resource_unicode_and_escaping_are_byte_bounded_and_lossless(tmp_path):
    from mac_bench.evaluation.tasks.resources import PublicResources
    from mac_bench.environments.tools.contracts import ToolError
    (tmp_path / 'public').mkdir()
    text = '😀\\"\nTARGET' * 3000
    (tmp_path / 'public/task.txt').write_text(text)
    resources = PublicResources(SimpleNamespace(root=tmp_path,
        manifest={'revision': 'fixed', 'public_files': ['public/task.txt']}))
    collected = ''; offset = 0
    while True:
        result = resources.read('public/task.txt', offset=offset, length=12000)
        assert len(json.dumps(result, ensure_ascii=False).encode()) <= 12000
        collected += result['text']
        if result['next_offset'] is None:
            break
        assert result['next_offset'] > offset
        offset = result['next_offset']
    assert collected == text
    result = resources.search('public/task.txt', 'TARGET', context=500, limit=100)
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 12000
    assert result['next_cursor']
    with pytest.raises(ToolError, match='minLength'):
        resources.search('public/task.txt', '')
