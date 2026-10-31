"""Exercise original worker/orchestration code through the common episode boundary."""
import json
import os
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
import numpy as np
import pytest
from mac_bench.runtime.llm.client import ChatMessage, ChatResponse
from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.evaluation import run_episode,task_record,open_task
from mac_bench.environments.packaging.bundle import seal_bundle
from mac_bench.runtime.sandbox.base import CommandResult
from mac_bench.baselines.aorchestra.tasks import AOrchestra
from mac_bench.baselines.meta_team.tasks import MetaTeam

pytestmark = pytest.mark.baselines


class Encoder:
    def encode(self,text):return np.array([1.,2.,3.])


class CodeWorld:
    def __init__(self):self.writes=[];self.closed=False
    def open(self):pass
    def close(self):self.closed=True
    def execute(self,command,*,timeout):
        self.writes.append(command);return CommandResult(command,0,'changed',.01)
    def export_submission(self,path):path.write_text(str(len(self.writes)));return path
    def grade_submission(self,path):
        n=int(path.read_text());return {'status':'passed' if n>=2 else 'failed','task_success':n>=2,'gc':min(n/2,1)}


class NativeOwner:
    def __init__(self):self.session=self;self.writes=[];self.closed=False;self.active=False
    def __enter__(self):return self
    def close(self):self.closed=True
    def observe(self):return {'writes':len(self.writes)}
    def start_episode(self,instance):self.active=True
    def stop_episode(self):self.active=False
    def agent_call(self,name,args):
        assert self.active
        self.writes.append(args);return {'data':{'changed':True}}
    def evaluate(self):
        assert not self.active
        n=len(self.writes);return {'status':'passed' if n>=2 else 'failed','complete':n>=2,'gc':min(n/2,1),'task_success':n>=2}
    def observe_and_evaluate(self):return {'observation':self.observe(),'evaluation':self.evaluate()}
    def export_receipts(self,path):path.write_bytes(b'receipts')


def handle(tmp_path,domain,task_id='sample'):
    root=tmp_path/'world';(root/'public').mkdir(parents=True);(root/'private').mkdir()
    (root/'public/task.txt').write_text('Complete two real changes in the candidate.')
    if domain=='engineering':
        meta={'world_family':'platform_data'};owner=CodeWorld()
    else:
        meta={'scenario_family':'service_administration','tool_api_version':2};owner=NativeOwner()
        (root/'private/initial.json').write_text('{"writes":0}')
    b=seal_bundle(root,world_id=task_id,public_files=['public/task.txt'],metadata=meta)
    h=open_task(task_record(b.root),tmp_path/'runtime',**({'world':owner} if domain=='engineering' else {'owner':owner}))
    return h,owner


def response(value):
    if isinstance(value,list):
        return ChatResponse('',model='scripted',usage={'prompt_tokens':4,'completion_tokens':2},
            tool_calls=tuple({'id':'c'+str(i),'type':'function','function':{'name':n,'arguments':json.dumps(a)}}
                             for i,(n,a) in enumerate(value)))
    return ChatResponse(value,model='scripted',usage={'prompt_tokens':4,'completion_tokens':2})


class Script:
    config=SimpleNamespace(model='scripted',max_retries=0)
    def __init__(self,values):self.values=iter(values);self.calls=[];self.lock=Lock()
    def complete(self,messages,**options):
        with self.lock:
            self.calls.append((messages,options));value=next(self.values)
        if isinstance(value,Exception):raise value
        return response(value)


def operations(domain):
    if domain=='engineering':return [('terminal',{'command':'write '+str(i)}) for i in range(2)]
    return [('call_tool',{'tool_name':'synthetic.write','arguments':{'value':i},'request_id':'r'+str(i)}) for i in range(2)]


def scripted(method,domain):
    ops=operations(domain)
    if method=='aorchestra':
        params={'task_instruction':'Make changes','model':'scripted'}
        if domain=='engineering': params['tools']=['terminal']
        values=[json.dumps({'action':'delegate_task','params':params})]
        values += [json.dumps({'action':('execute' if domain=='engineering' else n),'params':a}) for n,a in ops]
        values += [json.dumps({'action':'finish','params':{'status':'done','result':'two changes'}}),
                   'Two changes observed.',json.dumps({'action':'submit','params':{}})]
        return AOrchestra(),Script(values)
    if method=='meta_team':return MetaTeam(),Script([[ops[0]],[ops[1]],[('set_final_output',{'output':'changed'})],[('terminate',{})]])
    raise ValueError('Unsupported method: '+method)


@pytest.mark.parametrize('domain',['engineering','operational'])
@pytest.mark.parametrize('method',['aorchestra','meta_team'])
def test_original_methods_reach_both_task_families(tmp_path,method,domain):
    task,owner=handle(tmp_path,domain);baseline,client=scripted(method,domain)
    before=dict(os.environ);cwd=Path.cwd()
    # CLI callers commonly use relative output paths; native methods change cwd.
    result=run_episode(task,baseline,client,output=Path(os.path.relpath(tmp_path/'episode')),
        budget=AgentBudget(max_model_calls=30,max_tool_calls=20,max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed',result
    assert result['ps']==result['rr']==1 and owner.closed and len(owner.writes)==2
    assert result['tool_calls']==2 and result['agents_active_at_return']==0
    assert result['model_calls']==len(client.calls)
    calls=[json.loads(p.read_text()) for p in sorted((tmp_path/'episode/model_calls').glob('*.json'))]
    assert all(c['identity']['agent_id'] and c['identity']['logical_call_id'] for c in calls)
    assert 'SECRET' not in json.dumps([c['messages'] for c in calls])
    assert set(before) == set(os.environ), 'Baseline changed environment variable names'
    assert all(os.environ[k] == v for k, v in before.items()), 'Baseline changed environment variable values'
    assert cwd == Path.cwd()
    manifest=json.loads((tmp_path/'episode/native/method.json').read_text())
    assert manifest['resolved_config_hash']==result['method_provenance']['resolved_config_hash']
    if method=='aorchestra':
        assert any(c['identity']['phase']=='trajectory_summary' for c in calls)
        assert calls[0]['messages'][0]['role'] == 'system'
        prompt = next(m['content'] for m in calls[0]['messages'] if m['role'] == 'user')
        assert 'DECISION PROCESS' in prompt and '[Attempt 1/10]' in prompt
        if domain == 'engineering':
            assert 'SHARED CANDIDATE LIFECYCLE' in prompt
            assert 'FRESH container' not in prompt
        else:
            assert 'live applications' in prompt and 'single word' not in prompt
            assert '"tools": choose' not in prompt
    if method=='meta_team':
        assert manifest['configuration']['assets']['domain']==domain
        assert manifest['configuration']['assets']['adaptation']=='upstream_pool_environment_adapted_no_dev_evolution'


@pytest.mark.parametrize('method',['aorchestra','meta_team'])
def test_budget_stops_native_loops_without_hidden_extra_calls(tmp_path,method):
    task,owner=handle(tmp_path,'engineering');baseline,client=scripted(method,'engineering')
    result=run_episode(task,baseline,client,output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=1,max_tool_calls=20,max_tokens=10000,max_wall_seconds=20))
    assert result['status']=='completed' and result['stop_reason']=='budget:model_calls',result
    assert result['model_calls']==1 and len(client.calls)==1 and owner.closed
    assert result['agents_active_at_return']==0


@pytest.mark.parametrize('method',['aorchestra','meta_team'])
def test_provider_failure_after_write_retains_final_state(tmp_path,method):
    task,owner=handle(tmp_path,'engineering');baseline,client=scripted(method,'engineering')
    prefix={'aorchestra':2,'meta_team':1,'agentnet':3}[method]
    values=list(client.values)[:prefix]+[RuntimeError('scripted provider failure')]
    client=Script(values)
    result=run_episode(task,baseline,client,output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=20,max_tool_calls=20,max_tokens=10000,max_wall_seconds=20))
    assert result['status']=='error' and result['ps']==.5 and result['rr']==0,result
    assert len(owner.writes)==1 and owner.closed and result['agents_active_at_return']==0




@pytest.mark.parametrize('domain', ['engineering', 'operational'])
def test_meta_team_recruits_independent_worker_and_messages(tmp_path, domain, monkeypatch):
    monkeypatch.setenv('META_TEAM_MODEL', 'unrelated-process-model')
    task,owner=handle(tmp_path,domain)
    chairman = 'planner' if domain == 'engineering' else 'chairman'
    worker = 'developer' if domain == 'engineering' else 'worker-a'
    class TeamClient(Script):
        def __init__(self):super().__init__([]);self.turns={}
        def complete(self,messages,**options):
            is_worker = messages[0].content.startswith('# Developer') if domain == 'engineering' else messages[0].content.startswith('# LOCA-bench Worker')
            name = worker if is_worker else chairman
            with self.lock:
                turn=self.turns.get(name,0);self.turns[name]=turn+1;self.calls.append((messages,options))
            schemas = {t['function']['name']: t['function'] for t in options['tools']}
            if name == worker:
                assert chairman in schemas['send_message']['parameters']['properties']['to']['items']['enum']
                if domain == 'engineering':
                    assert 'use_skill' in schemas
                    assert 'codebase-navigation' in messages[0].content
                    if turn == 0:
                        return response([('use_skill', {'skill_name': 'codebase-navigation'})])
                    if turn == 1:
                        assert any('Narrow your grep scope' in str(m.content) for m in messages)
                        return response(operations(domain))
                elif turn==0:return response(operations(domain))
                return response([('send_message',{'to':[chairman],'content':'Both candidate changes are done.'})])
            assert 'list_pool' in schemas and 'start_agent' in schemas
            if turn:
                assert worker in schemas['send_message']['parameters']['properties']['to']['items']['enum']
            values=[ [('start_agent',{'name':worker})],
                     [('send_message',{'to':[worker],'content':'Make two changes and report back.'})],
                     [('wait_for_replies',{'from_agents':[worker],'reason':'Need evidence'})],
                     [('set_final_output',{'output':'Both changes are done.'}),('terminate',{})] ]
            return response(values[min(turn,3)])
    result=run_episode(task,MetaTeam(),TeamClient(),output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=30,max_tool_calls=20,max_tokens=10000,max_wall_seconds=20))
    assert result['ps']==1 and set(result['agents_used'])=={chairman,worker},result
    assert result['active_agents_peak']==2 and result['internal_control_calls']>=3
    assert os.environ['META_TEAM_MODEL'] == 'unrelated-process-model'


def test_meta_team_rejects_unrecorded_prompt_replacement(tmp_path):
    import shutil
    from mac_bench.baselines.common.config import ROOT
    method = MetaTeam()
    pool = tmp_path / 'team'
    shutil.copytree(ROOT / method.spec['teams']['engineering']['artifact'], pool)
    method.spec['teams']['engineering']['artifact'] = str(pool)
    method.validate()
    (pool / 'planner/prompt.md').write_text('Do everything yourself.')
    with pytest.raises(ValueError, match='upstream adaptation drift'):
        method.validate()


def test_aorchestra_worker_sees_actual_steps_and_limit(tmp_path):
    task, owner = handle(tmp_path, 'engineering')
    baseline, client = scripted('aorchestra', 'engineering')
    baseline.spec['upstream_limits']['sub_agent_steps'] = 4
    result = run_episode(task, baseline, client, output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=6, max_tool_calls=20,
                           max_tokens=10000, max_wall_seconds=30))
    assert result['status'] == 'completed', result
    prompts = [next(m.content for m in messages if m.role == 'user')
               for messages, _ in client.calls]
    assert '[Step 1/4] Remaining: 3 step(s)' in prompts[1]
    assert '[Step 2/4] Remaining: 2 step(s)' in prompts[2]
    assert '[Step 3/4] Remaining: 1 step(s)' in prompts[3]
    assert '5 model calls remain BEFORE this response' in prompts[1]
    assert '3 model calls remain BEFORE this response' in prompts[3]
    assert 'Return finish now' in prompts[1]
    assert "Use 'finish' NOW!" in prompts[3]
    assert owner.closed and len(owner.writes) == 2


def test_aorchestra_main_reports_invalid_format_before_retrying(tmp_path):
    task, owner = handle(tmp_path, 'engineering')
    baseline, script = scripted('aorchestra', 'engineering')
    client = Script(['Wrong protocol: execute a command.'] + list(script.values))
    result = run_episode(task, baseline, client, output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=30,max_tool_calls=20,max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and len(owner.writes)==2
    assert len(client.calls[1][0])==3
    assert 'no action was executed' in client.calls[1][0][-1].content


def test_meta_team_members_can_access_public_resources_and_query_results(tmp_path):
    task, owner = handle(tmp_path, 'operational')
    baseline, script = scripted('meta_team', 'operational')
    class CheckTools(Script):
        def complete(self, messages, **options):
            names = {t['function']['name'] for t in options['tools']}
            assert {'list_resources','read_resource','query_resource','search_resource',
                    'query_result'} <= names
            return super().complete(messages, **options)
    client = CheckTools(list(script.values))
    result = run_episode(task, baseline, client, output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=30,max_tool_calls=20,max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and len(owner.writes)==2


def test_aorchestra_memory_keeps_assignment_and_rewrites_previous_state(tmp_path):
    task, owner = handle(tmp_path, 'engineering')
    instruction = 'Inspect the two files and report missing functions. Do NOT make changes.'
    values = [json.dumps({'action':'delegate_task', 'params':{
        'task_instruction':instruction, 'context':'The overall project needs implementation.',
        'model':'scripted'}})]
    inspect = json.dumps({'action':'execute','params':{'command':'inspect'},
                          'memory':'Findings collected for this report.'})
    values += [inspect]*10 + ['FIRST_STATE: missing function alpha; inspection only.']
    values += [inspect]*7 + ['SECOND_STATE: requested findings available; finish report.']
    values += [inspect, json.dumps({'action':'finish','params':{'status':'done','result':'findings'}}),
               'Inspection report returned.', json.dumps({'action':'submit','params':{}})]
    client = Script(values)
    result = run_episode(task, AOrchestra(), client, output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=30, max_tool_calls=25,
                           max_tokens=10000, max_wall_seconds=30))
    assert result['status'] == 'completed', result
    prompts = [next(m.content for m in messages if m.role == 'user')
               for messages, _ in client.calls]
    summaries = [p for p in prompts if 'memory compression module' in p]
    assert len(summaries) == 2
    assert all(instruction in p and 'completion conditions' in p for p in summaries)
    assert 'FIRST_STATE' in summaries[1]
    assert 'SECOND_STATE' in prompts[20] and 'FIRST_STATE' not in prompts[20]
    assert owner.closed




def magentic_ledger(speaker, *, done=False, progress=True, loop=False):
    return json.dumps({k:{'answer':v,'reason':'Recorded public progress.'} for k,v in {
        'is_request_satisfied':done,'is_in_loop':loop,'is_progress_being_made':progress,
        'next_speaker':speaker,'instruction_or_question':'Perform the next assigned change.'}.items()})


def magentic_changes(domain, *, facts='Facts.', plan='Plan.'):
    if domain == 'engineering':
        return [facts, plan, magentic_ledger('Coder'), '```sh\nwrite 0\n```',
                magentic_ledger('ComputerTerminal'), magentic_ledger('Coder'),
                '```sh\nwrite 1\n```', magentic_ledger('ComputerTerminal'),
                magentic_ledger('Coder', done=True), 'Done.']
    ops = operations(domain)
    return [facts, plan, magentic_ledger('Operator'), [ops[0]], 'First change observed.',
            magentic_ledger('Reviewer'), [ops[1]], 'Second change observed.',
            magentic_ledger('Operator', done=True), 'Done.']


def test_magentic_one_terminal_handover_preserves_output_recovery_location(tmp_path):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    task, owner = handle(tmp_path, 'engineering')
    def execute(command, *, timeout):
        owner.writes.append(command)
        return CommandResult(command,0,'{"output_bytes":30000}\nA bounded preview',.01)
    owner.execute = execute
    result = run_episode(task,MagenticOne(),Script(magentic_changes('engineering')),
        output=tmp_path/'episode',budget=AgentBudget(max_model_calls=30,max_tool_calls=20,
        max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and owner.closed
    messages = (tmp_path/'episode/native/messages.jsonl').read_text()
    assert 'full_output_path' in messages and '/work/tool-output/' in messages
    assert 'output_truncated' in messages and 'A bounded preview' in messages


def test_magentic_one_terminal_accepts_native_coordinator_code(tmp_path):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    task, owner = handle(tmp_path,'engineering')
    values = ['Facts.','Run this inspection.\n```sh\nwrite 0\n```',
        magentic_ledger('ComputerTerminal'),magentic_ledger('ComputerTerminal',done=True),'Done.']
    result = run_episode(task,MagenticOne(),Script(values),output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=20,max_tool_calls=10,max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and result['tool_calls']==1 and len(owner.writes)==1


def test_magentic_one_non_json_ledger_uses_native_retry_loop(tmp_path):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    task, owner = handle(tmp_path,'engineering')
    values = magentic_changes('engineering')
    values.insert(2,'```sh\nwrite NEVER_EXECUTE_THIS_DRAFT\n```')
    result = run_episode(task,MagenticOne(),Script(values),output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=20,max_tool_calls=10,max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and len(owner.writes)==2
    assert all('NEVER_EXECUTE_THIS_DRAFT' not in command for command in owner.writes)
    assert 'Invalid ledger format encountered, retrying' in (tmp_path/'episode/native/ledgers.jsonl').read_text()


def test_magentic_one_api_preserves_native_latest_request_and_tool_free_reflection(tmp_path):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    from mac_bench.evaluation.episodes.identity import call_identity
    task, owner = handle(tmp_path,'operational')
    class CheckRequests(Script):
        def complete(self, messages, **options):
            if call_identity().get('role')=='orchestrator':
                assert messages[-1].role=='user'
                assert messages[-1].content in messages[1].content
            return super().complete(messages, **options)
    method = MagenticOne()
    method.spec['execution']['worker_tool_iterations_per_turn']=1
    result = run_episode(task,method,CheckRequests(magentic_changes('operational')),
        output=tmp_path/'episode',budget=AgentBudget(max_model_calls=30,max_tool_calls=20,
        max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and len(owner.writes)==2


def test_magentic_one_file_surfer_can_read_public_task_outside_project(tmp_path):
    import subprocess
    from mac_bench.baselines.common.config import ROOT
    from mac_bench.baselines.common.source import source_modules
    root=ROOT/'.cache/baselines/Magentic-One/python/packages'
    with source_modules(root/'autogen-ext/src', ('autogen_ext','mac_bench.baselines.magentic_one.world_agents')):
        from mac_bench.baselines.magentic_one.world_agents import _BROWSE
    candidate=tmp_path/'candidate';candidate.mkdir()
    task=tmp_path/'TASK.md';task.write_text('PUBLIC REQUIREMENTS')
    program=_BROWSE.format(arguments=repr(json.dumps({'path':str(task),'page':0}))).replace(
        "pathlib.Path('/work/project')",f'pathlib.Path({str(candidate)!r})')
    result=subprocess.run([__import__('sys').executable,'-c',program],capture_output=True,text=True)
    assert result.returncode==0 and 'PUBLIC REQUIREMENTS' in result.stdout


@pytest.mark.parametrize('domain', ['engineering','operational'])
def test_magentic_one_native_team_calls_shared_world_and_grades(tmp_path, domain):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    task, owner = handle(tmp_path, domain)
    first, second = ('Coder','ComputerTerminal') if domain=='engineering' else ('Operator','Reviewer')
    ops=operations(domain)
    client=Script(magentic_changes(domain, facts='Given public task.', plan='Make two changes.'))
    result=run_episode(task,MagenticOne(),client,output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=30,max_tool_calls=20,max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and result['ps']==1 and owner.closed,result
    assert result['tool_calls']==2 and result['model_calls']==10
    assert set(result['agents_used'])=={'Orchestrator',first,second}
    assert result['agents_active_at_return']==0
    calls=[json.loads(p.read_text()) for p in sorted((tmp_path/'episode/model_calls').glob('*.json'))]
    assert all('SECRET' not in json.dumps(x['messages']) for x in calls)
    assert (tmp_path/'episode/native/team_state.json').exists()
    assert (tmp_path/'episode/native/messages.jsonl').exists()
    if domain == 'engineering':
        coder_calls = [x for x in calls if x['identity']['agent_id']=='Coder']
        assert coder_calls and all(not x.get('tools') for x in coder_calls)
        assert all('The user can\'t modify your code' in json.dumps(x['messages']) for x in coder_calls)
        assert not any(x['identity']['agent_id']=='ComputerTerminal' for x in calls)


def test_magentic_one_file_surfer_reads_pages_and_searches_in_candidate(tmp_path):
    import shlex
    import subprocess
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    task, owner = handle(tmp_path, 'engineering')
    candidate = tmp_path/'candidate'
    candidate.mkdir()
    content = 'a'*5120 + 'SECOND_PAGE_MARKER\n' + 'b'*6000
    (candidate/'notes.txt').write_text(content)
    commands = []
    def execute(command, *, timeout):
        commands.append(command)
        # Test-only transport: run the fixed browser program against a local
        # temporary candidate instead of a remote Modal project directory.
        wrapper = shlex.split(command)
        assert wrapper[:2] == ['python3', '-c']
        python, option, program = shlex.split(wrapper[3])
        assert option == '-c'
        program = program.replace("pathlib.Path('/work/project')", f'pathlib.Path({str(candidate)!r})')
        wrapper[3] = shlex.join([python, option, program])
        wrapper[4] = str(candidate)
        wrapper[5] = str(tmp_path / f'browser-{len(commands)}.log')
        run = subprocess.run(wrapper, capture_output=True, text=True, timeout=timeout)
        return CommandResult(command, run.returncode, run.stdout+run.stderr, .01)
    owner.execute = execute
    values = ['Facts.', 'Inspect notes.', magentic_ledger('FileSurfer'),
              [('open_path', {'path':'notes.txt'})], magentic_ledger('FileSurfer'),
              [('page_down', {})], magentic_ledger('FileSurfer'),
              [('find_on_page_ctrl_f', {'search_string':'SECOND_PAGE_MARKER'})],
              magentic_ledger('FileSurfer',done=True), 'Inspection complete.']
    result = run_episode(task, MagenticOne(), Script(values), output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=20,max_tool_calls=10,max_tokens=50000,max_wall_seconds=30))
    assert result['status']=='completed' and owner.closed, result
    assert result['tool_calls']==3 and len(commands)==3
    assert (candidate/'notes.txt').read_text()==content
    messages = (tmp_path/'episode/native/messages.jsonl').read_text()
    assert 'Showing page 2 of 3' in messages and 'SECOND_PAGE_MARKER' in messages


def test_magentic_one_file_surfer_rejects_candidate_escape(tmp_path):
    # Exercise the actual transported program, including symlink resolution.
    import subprocess
    from mac_bench.baselines.common.config import ROOT
    from mac_bench.baselines.common.source import source_modules
    root=ROOT/'.cache/baselines/Magentic-One/python/packages'
    with source_modules(root/'autogen-ext/src', ('autogen_ext','mac_bench.baselines.magentic_one.world_agents')):
        from mac_bench.baselines.magentic_one.world_agents import _BROWSE
    candidate=tmp_path/'candidate';candidate.mkdir()
    outside=tmp_path/'private.txt';outside.write_text('PRIVATE_OWNER_ONLY')
    (candidate/'escape').symlink_to(outside)
    values=json.dumps(dict(path='escape',page=0))
    program=_BROWSE.format(arguments=repr(values)).replace(
        "pathlib.Path('/work/project')",f'pathlib.Path({str(candidate)!r})')
    run=subprocess.run([__import__('sys').executable,'-c',program],capture_output=True,text=True)
    assert run.returncode and 'outside candidate project' in run.stderr
    assert 'PRIVATE_OWNER_ONLY' not in run.stdout+run.stderr


@pytest.mark.parametrize('failure', ['tool_budget','transport'])
def test_magentic_one_native_executor_failure_keeps_submission_and_scores(tmp_path,failure):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    task,owner=handle(tmp_path,'engineering')
    if failure=='transport':
        execute=owner.execute
        def fail_second(command,*,timeout):
            if owner.writes: raise RuntimeError('candidate transport failed')
            return execute(command,timeout=timeout)
        owner.execute=fail_second
    result=run_episode(task,MagenticOne(),Script(magentic_changes('engineering')),
        output=tmp_path/'episode',budget=AgentBudget(max_model_calls=20,
            max_tool_calls=1 if failure=='tool_budget' else 10,
            max_tokens=10000,max_wall_seconds=30))
    assert owner.closed and result['ps']==.5 and len(owner.writes)==1,result
    assert result['agents_active_at_return']==0
    if failure=='tool_budget':
        assert result['stop_reason']=='budget:tool_calls' and result['status']=='completed',result
    else:
        assert result['status']=='error' and result['error_type']=='RuntimeError',result


def test_magentic_one_budget_stop_and_provider_failure_preserve_grading(tmp_path):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    task, owner=handle(tmp_path,'engineering')
    client=Script(['Given facts.'])
    result=run_episode(task,MagenticOne(),client,output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=1,max_tool_calls=20,max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and result['stop_reason']=='budget:model_calls',result
    assert owner.closed and result['model_call_attempts']==1
    assert result['agents_active_at_return']==0


def test_magentic_one_replan_keeps_prior_world_edits(tmp_path):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    task,owner=handle(tmp_path,'engineering');ops=operations('engineering')
    method=MagenticOne();method.spec['limits']['max_stalls']=1
    client=Script(['Facts.','Plan.',magentic_ledger('Coder'),'```sh\nwrite 0\n```',
        magentic_ledger('ComputerTerminal'),magentic_ledger('Coder',progress=False,loop=True),
        'Updated facts: first change exists.', 'Revised plan: make second change.',
        magentic_ledger('Coder'),'```sh\nwrite 1\n```',magentic_ledger('ComputerTerminal'),
        magentic_ledger('Coder',done=True),'Done.'])
    result=run_episode(task,method,client,output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=30,max_tool_calls=20,max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and result['ps']==1 and owner.closed,result
    assert len(owner.writes)==2 and 'write 0' in owner.writes[0] and 'write 1' in owner.writes[1]
    messages=(tmp_path/'episode/native/messages.jsonl').read_text()
    assert 'Revised plan' in messages


@pytest.mark.parametrize('failure', ['budget','provider'])
def test_magentic_one_stops_after_write_without_losing_partial_state(tmp_path,failure):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    task,owner=handle(tmp_path,'engineering')
    values=['Facts.','Plan.',magentic_ledger('Coder'),'```sh\nwrite 0\n```',magentic_ledger('ComputerTerminal')]
    if failure=='provider':values.append(RuntimeError('scripted provider failure'))
    result=run_episode(task,MagenticOne(),Script(values),output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=5 if failure=='budget' else 20,
            max_tool_calls=20,max_tokens=10000,max_wall_seconds=30))
    assert result['ps']==.5 and owner.closed and len(owner.writes)==1,result
    assert result['agents_active_at_return']==0
    if failure=='budget':assert result['stop_reason']=='budget:model_calls'
    else:assert result['status']=='error' and result['error_type']=='RuntimeError'


def test_agentnet_is_not_an_active_experiment_method():
    from mac_bench.baselines.common.registry import create_method
    with pytest.raises(ValueError,match='Unknown collaboration method'):
        create_method('agentnet')


@pytest.mark.parametrize('domain',['engineering','operational'])
def test_magentic_one_bounds_coordinator_context_and_preserves_current_ledger(tmp_path,domain):
    from mac_bench.baselines.magentic_one.tasks import MagenticOne
    from mac_bench.evaluation.episodes.identity import call_identity
    task,owner=handle(tmp_path,domain)
    # Leave room for the current business tool definitions, then exceed it
    # with an observation so this tests compaction of history.
    large='Large observed state with exact references. '*500
    if domain=='engineering':
        original=owner.execute
        def execute(command,*,timeout):
            original(command,timeout=timeout)
            return CommandResult(command,0,large if len(owner.writes)==1 else 'changed',.01)
        owner.execute=execute
    else:
        original=owner.agent_call
        def agent_call(name,args):
            value=original(name,args)
            return {**value,'large_state':large if len(owner.writes)==1 else 'changed'}
        owner.agent_call=agent_call
    first,second=('Coder','ComputerTerminal') if domain=='engineering' else ('Operator','Reviewer')
    ops=operations(domain)
    class SummaryScript(Script):
        def complete(self,messages,**options):
            if call_identity().get('phase')=='context_summary':
                self.calls.append((messages,options))
                return response('CHANGES AND CHECKS: One operation recorded; second remains. '
                                'FINDINGS: Recover exact state through the published live tools.')
            return super().complete(messages,**options)
    client=SummaryScript(magentic_changes(domain, facts='Given facts.', plan='PLAN_MARKER: Make two changes.'))
    method=MagenticOne();method.spec['model_parameters']['max_tokens']=512
    method.spec['context_policy']=dict(request_token_limit=8192 if domain=='engineering' else 16384,
        target_input_tokens=3000 if domain=='engineering' else 8000,
        summary_output_tokens=512,recent_turns=1,old_read_output_chars=1600)
    result=run_episode(task,method,client,output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=40,max_tool_calls=20,max_tokens=10000,max_wall_seconds=30))
    assert result['status']=='completed' and result['ps']==1 and owner.closed,result
    events=[json.loads(x) for x in (tmp_path/'episode/events.jsonl').read_text().splitlines()]
    assert any(x['kind']=='context_compaction' for x in events)
    calls=[json.loads(p.read_text()) for p in sorted((tmp_path/'episode/model_calls').glob('*.json'))]
    summarized=[x for x in calls if x['identity']['phase']=='context_summary']
    assert summarized and all(x['max_tokens']==512 for x in summarized)
    ledgers=[x for x in calls if x['identity']['phase']=='orchestration' and
             'is_request_satisfied' in json.dumps(x['messages'])]
    assert ledgers and all('PLAN_MARKER' in json.dumps(x['messages']) for x in ledgers)
    assert any(x['kind']=='context_archive' and large in json.dumps(x) for x in events)


def test_magentic_one_transient_ledger_request_does_not_restart_compacted_history():
    from autogen_core.models import AssistantMessage,UserMessage
    from mac_bench.baselines.magentic_one.bridge import RecordedModel
    from mac_bench.runtime.agents.context import ContextPolicy, MEMORY_PREFIX
    context=SimpleNamespace(task=SimpleNamespace(instruction='Task.',interaction_instructions='Tools.'),
                            event=lambda value:None)
    model=RecordedModel(context,None,name='Orchestrator',role='orchestrator',
                        policy=ContextPolicy(),failures=[])
    ledger=AssistantMessage(content='We are working to address the following user request: Task. PLAN_MARKER',
                            source='MagenticOneOrchestrator')
    model._sync([ledger,UserMessage(content='Produce ledger JSON for first turn.',source='MagenticOneOrchestrator')])
    model.messages=[*model.messages[:2],ChatMessage('user',MEMORY_PREFIX+'Earlier evidence retained.')]
    model._sync([ledger,AssistantMessage(content='Do next work.',source='MagenticOneOrchestrator'),
                 UserMessage(content='Work observed.',source='Coder'),
                 UserMessage(content='Produce ledger JSON for second turn.',source='MagenticOneOrchestrator')])
    assert model.request.endswith('Produce ledger JSON for second turn.')
    assert any(MEMORY_PREFIX in str(m.content) for m in model.messages)
    assert any('Work observed.' in str(m.content) for m in model.messages)
    assert all('Produce ledger JSON' not in str(m.content) for m in model.messages[2:])
    assert 'PLAN_MARKER' in model.ledger


def test_meta_team_native_worker_truncation_does_not_terminate_chairman(tmp_path):
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    task, owner = handle(tmp_path, 'engineering')
    class BrokenWorker(Script):
        def __init__(self):
            super().__init__([])
            self.turn = 0
            self.failed_calls = 0
        def complete(self, messages, **options):
            with self.lock:
                self.calls.append((messages, options))
                if messages[0].content.startswith('# Developer'):
                    self.failed_calls += 1
                    raise ModelOutputTruncatedError('length', response=response(
                        [('terminal', {'command': 'UNEXECUTED'})]))
                turn = self.turn
                self.turn += 1
            if turn == 0:
                return response([('start_agent', {'name': 'developer'}),
                                 ('send_message', {'to': ['developer'], 'content': 'Implement changes.'})])
            import time
            deadline = time.monotonic() + 10
            while self.failed_calls < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            assert self.failed_calls == 2
            if turn == 1:
                return response(operations('engineering'))
            return response([('set_final_output', {'output': 'Finished after worker failure.'}),
                             ('terminate', {})])
    client = BrokenWorker()
    result = run_episode(task, MetaTeam(), client, output=tmp_path/'episode',
        budget=AgentBudget(max_model_calls=35, max_tool_calls=20,
                          max_tokens=50000, max_wall_seconds=60))
    events = [json.loads(line) for line in (tmp_path/'episode/events.jsonl').read_text().splitlines()]
    assert result['status'] == 'completed', result
    assert client.failed_calls == 2
    assert 'UNEXECUTED' not in owner.writes
    assert any(e.get('event_type') == 'agent.loop.error'
               and e.get('agent_id') == 'developer' for e in events)
    assert not any(e.get('data', {}).get('reason') == 'shared_gateway_stop' for e in events)
    assert result['closed']


@pytest.mark.parametrize('error', [RuntimeError('provider'), ValueError('storage')])
def test_meta_team_preserves_fatal_completion_errors(error):
    from mac_bench.baselines.meta_team.tasks import _propagate_completion_stop
    from mac_bench.baselines.common.gateway import MethodStop
    stop = MethodStop(error)
    with pytest.raises(MethodStop) as caught:
        _propagate_completion_stop(stop)
    assert caught.value is stop
