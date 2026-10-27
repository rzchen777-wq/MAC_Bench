"""Context boundaries, identity preservation, and real loop/helper accounting."""
import json
from types import SimpleNamespace
import pytest
from mac_bench.runtime.agents.context import (ContextPolicy, WorkingContext, ContextCapacityError,
    completed_turns, MEMORY_PREFIX, TokenEstimate)
from mac_bench.runtime.llm.client import ChatMessage, ChatResponse


def interaction(i, content, arguments='{}'):
    return [ChatMessage('assistant', 'Observed work', tool_calls=({'id':str(i),'type':'function',
        'function':{'name':'terminal','arguments':arguments}},)),
        ChatMessage('tool', content, tool_call_id=str(i))]


def test_compaction_preserves_full_turns_and_raw_history():
    manager=WorkingContext(ContextPolicy(request_token_limit=3500,target_input_tokens=1400,
        summary_output_tokens=200,recent_turns=2))
    messages=[ChatMessage('system','Rules'),ChatMessage('user','Complete the task')]
    messages += interaction(1,'source\n'*1800, 'write x.py\n'+'code\n'*1000)
    messages += interaction(2,'test passed')+interaction(3,'unknown receipt request_id=R17 result_ref=H9 record_id=E12')
    original=[m.as_dict() for m in messages];archives=[];requests=[]
    def summarize(batch,limit):
        requests.append(batch)
        assert manager.tokens.count(batch)+limit <= manager.policy.request_token_limit
        assert all(m.role in {'system','user'} and not m.tool_calls for m in batch)
        return ChatResponse('Modified x.py. Exact readback remains necessary.',model='scripted',usage={})
    stats=manager.prepare(messages,[],400,summarize,archives.append)
    assert stats and stats['estimated_input_tokens_after']<=3100
    assert [m.as_dict() for m in messages[-4:]]==original[-4:]
    assert [m.as_dict() for m in messages[:2]]==original[:2]
    assert archives[0]['messages']==[m.trace_dict() for m in [ChatMessage(**x) for x in original[2:]]]
    assert stats['summary_calls']==len(requests)>0
    assert 'code\ncode\ncode' not in json.dumps([m.as_dict() for m in messages])
    completed_turns(messages[3:])


def test_pending_tool_calls_cannot_be_compacted():
    with pytest.raises(ContextCapacityError,match='pending'):
        completed_turns([interaction(1,'')[0]])


def test_failed_summary_never_discards_history():
    manager=WorkingContext(ContextPolicy(request_token_limit=2000,target_input_tokens=800,summary_output_tokens=100))
    messages=[ChatMessage('system','Rules'),ChatMessage('user','Task')]+interaction(1,'x'*9000)
    original=list(messages)
    def fail(*args):raise RuntimeError('provider failed')
    with pytest.raises(RuntimeError,match='provider failed'):
        manager.prepare(messages,[],200,fail)
    assert messages==original


def test_large_fixed_task_is_not_truncated():
    manager=WorkingContext(ContextPolicy(request_token_limit=1000,target_input_tokens=500,summary_output_tokens=100))
    messages=[ChatMessage('system','Rules'),ChatMessage('user','x'*8000)]
    with pytest.raises(ContextCapacityError,match='Task/tool'):
        manager.prepare(messages,[],100,lambda *a:None)
    assert len(messages[1].content)==8000


def test_estimator_calibrates_from_entire_request_including_arguments_and_schema():
    estimate=TokenEstimate();messages=interaction(1,'ok','x'*1000)
    tools=[{'large_schema':'enum'*1000}]
    assert estimate.count(messages,tools)>estimate.count(messages)
    before=estimate.count(messages,tools)
    estimate.observe(messages,tools,{'prompt_tokens':before*2})
    assert estimate.count(messages,tools)>=before*2


@pytest.mark.parametrize('domain',['engineering','operational'])
def test_real_task_loop_counts_summaries_and_retains_business_uncertainty(tmp_path,domain):
    from mac_bench.baselines.react_single.tasks import ReactSingle
    from mac_bench.runtime.agents.workflow import AgentBudget
    from mac_bench.evaluation.episodes.protocol import EpisodeContext
    from mac_bench.evaluation.episodes.budget import EpisodeBudget
    from mac_bench.evaluation.episodes.recording import RecordingClient
    from mac_bench.evaluation.tasks import TaskView,ToolDefinition,AgentEnvironment
    name='terminal' if domain=='engineering' else 'call_tool'
    field='command' if domain=='engineering' else 'operation'
    tool=ToolDefinition(name,'Inspect actual state',{'type':'object','properties':{field:{'type':'string'}},
        'required':[field],'additionalProperties':False})
    observation={'record_id':'E12','request_id':'R17','status':'unknown','result_ref':'H9',
        'details':'long source or native observation\n'*1000}
    env=AgentEnvironment([tool],lambda n,a:observation)
    budget=EpisodeBudget(AgentBudget(max_model_calls=10,max_tool_calls=10,max_env_actions=10,max_tokens=100000))
    env.begin(admit_call=budget.admit_tool,event=lambda e:None)
    class Client:
        config=SimpleNamespace(model='scripted')
        normal_calls=summary_calls=0
        summary_inputs=''
        def complete(self,messages,**kwargs):
            if messages[0].content.startswith('You maintain working memory'):
                self.summary_calls+=1
                assert 'unknown receipts are not proof' in messages[0].content
                self.summary_inputs += messages[1].content
                return ChatResponse('E12: write outcome unknown, request_id R17, saved observation H9. '
                    'Read current state before retrying. Code locations remain in the workspace.',
                    model='scripted',usage={'prompt_tokens':10,'completion_tokens':5})
            self.normal_calls+=1
            if self.normal_calls==1:
                n,args=name,{field:'inspect'}
            else:
                assert messages[2].content.startswith(MEMORY_PREFIX)
                assert all(x in messages[2].content for x in ['E12','R17','H9','unknown'])
                n,args='finish',{'summary':'Reported partial work'}
            return ChatResponse('',model='scripted',usage={'prompt_tokens':10,'completion_tokens':5},
                tool_calls=({'id':str(self.normal_calls),'type':'function','function':{'name':n,'arguments':json.dumps(args)}},))
    client=Client();recorded=RecordingClient(client,tmp_path/'calls',budget=budget)
    view=TaskView('task','revision','Complete the task.',(tool,),'Use live tools.')
    events=[]
    result=ReactSingle(context_policy=ContextPolicy(request_token_limit=11000,target_input_tokens=1800,
        summary_output_tokens=300,recent_turns=1)).run(EpisodeContext(view,env,recorded,budget,events.append))
    env.end()
    assert client.normal_calls==2 and client.summary_calls>0
    assert all(x in client.summary_inputs for x in ['E12','R17','H9'])
    assert result.model_calls==budget.model_calls==2+client.summary_calls
    assert result.input_tokens==budget.input_tokens==result.model_calls*10
    assert sum(e['kind']=='context_archive' for e in events)==1
    summaries=[json.loads(p.read_text()) for p in (tmp_path/'calls').glob('*.json')
        if json.loads(p.read_text())['identity'].get('phase')=='context_summary']
    assert len(summaries)==client.summary_calls
    assert any(t['kind']=='context_compaction' for t in result.trace)


def test_incremental_compaction_carries_prior_memory_and_does_not_edit_recent_reasoning():
    manager=WorkingContext(ContextPolicy(request_token_limit=3000,target_input_tokens=1000,
        summary_output_tokens=200,recent_turns=1))
    first=interaction(1,'old log\n'*1500)
    signed=ChatMessage('assistant','Inspect next',tool_calls=interaction(2,'')[0].tool_calls,
        reasoning_details=({'signature':'opaque','text':'keep exactly'},))
    recent=[signed,ChatMessage('tool','request_id=R17 status=unknown',tool_call_id='2')]
    messages=[ChatMessage('system','Rules'),ChatMessage('user','Task')]+first+recent
    def summary(batch,limit):return ChatResponse('R17 remains unknown. Modified x.py.',model='scripted',usage={})
    manager.prepare(messages,[],300,summary)
    assert messages[-2] is signed
    messages+=interaction(3,'new log\n'*1500)
    requests=[]
    def again(batch,limit):
        requests.append(batch)
        assert 'R17 remains unknown' in batch[1].content
        return ChatResponse('R17 remains unknown. New log inspected; x.py changed.',model='scripted',usage={})
    manager.prepare(messages,[],300,again)
    assert requests and sum(str(m.content).startswith(MEMORY_PREFIX) for m in messages)==1


@pytest.mark.parametrize('truncated', [False, True])
def test_summary_consumes_last_local_model_call_without_free_solver_request(truncated):
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    from mac_bench.runtime.agents.workflow import ReactAgent,AgentBudget
    from mac_bench.runtime.types import PublicTask
    from mac_bench.runtime.types import SolverWorkspace
    from mac_bench.runtime.tools import ToolSpec,ToolResult
    class Client:
        count=0
        def complete(self,messages,**kwargs):
            self.count+=1
            if self.count==1:
                return ChatResponse('',model='scripted',usage={'prompt_tokens':10,'completion_tokens':5},
                    tool_calls=({'id':'a','type':'function','function':{'name':'inspect','arguments':'{}'}},))
            assert self.count==2 and messages[0].content.startswith('You maintain working memory')
            if truncated:
                raise ModelOutputTruncatedError('length')
            return ChatResponse('Inspected state; task unfinished.',model='scripted',usage={'prompt_tokens':10,'completion_tokens':5})
    client=Client()
    tool=ToolSpec('inspect','Inspect state',{},(),lambda c,a:ToolResult.completed({'data':'x'*6000}))
    agent=ReactAgent(client,None,workspace_tools=False,extra_tools=[tool],
        budget=AgentBudget(max_model_calls=2,max_tool_calls=2,max_tokens=10000),
        model_max_tokens=500,output_limit=10000,
        context_policy=ContextPolicy(request_token_limit=2500,target_input_tokens=1200,summary_output_tokens=200),
        system_prompt='Rules',user_prompt='Task')
    result=agent.run(task=PublicTask('id','type','Task'),workspace=SolverWorkspace(None,cwd='.',kind='artifact'))
    assert result.stop_reason=='model_call_budget' and result.model_calls==2==client.count
    assert result.input_tokens==(10 if truncated else 20) and result.output_tokens==(5 if truncated else 10)


def test_old_file_outputs_masked_without_summary_and_recent_evidence_kept():
    manager=WorkingContext(ContextPolicy(request_token_limit=6000,target_input_tokens=1800,
        summary_output_tokens=300,recent_turns=2,recent_tokens=1000,
        prune_old_reads=True,old_read_output_chars=600))
    old=interaction(1,json.dumps({'exit_code':0,'output':'source\n'*3000,'output_truncated':False}),
        json.dumps({'command':'cd /work/project && cat x.py'}))
    recent=interaction(2,'test failed at x.py:42')+interaction(3,'current source\n'*250)
    messages=[ChatMessage('system','Rules'),ChatMessage('user','Task')]+old+recent
    archives=[]
    def unexpected(*args):raise AssertionError('No summary needed')
    stats=manager.prepare(messages,[],500,unexpected,archives.append)
    assert stats['strategy']=='observation_masking' and stats['summary_calls']==0
    assert messages[-4:]==recent and messages[2] is old[0]
    observation=json.loads(messages[3].content)
    assert observation['exit_code']==0 and observation['context_output_abbreviated']
    assert archives[0]['messages'][1]['content']==old[1].content
    completed_turns(messages[2:])


@pytest.mark.parametrize('name,command,exit_code',[
    ('call_tool','cat x.py',0),('terminal','python mutate.py',0),
    ('terminal','cat x.py > y.py',0),('terminal','cat $(python mutate.py)',0),
    ('terminal','cat missing.py',1)])
def test_masking_never_elides_business_receipts_mutations_or_failed_reads(name,command,exit_code):
    from mac_bench.runtime.agents.context import mask_old_reads
    old=interaction(1,json.dumps({'exit_code':exit_code,'output':'unknown R17 E12\n'*1000}),
        json.dumps({'command':command}))
    call=old[0].tool_calls[0]
    old[0]=ChatMessage('assistant','Inspect',tool_calls=({**call,'function':{**call['function'],'name':name}},))
    turns=[old,interaction(2,'recent')]
    masked,count=mask_old_reads(turns,1,600)
    assert count==0 and masked==turns


def test_recent_turn_survives_even_when_above_soft_target():
    manager=WorkingContext(ContextPolicy(request_token_limit=6000,target_input_tokens=1400,
        summary_output_tokens=200,recent_turns=1))
    old=interaction(1,'old output\n'*2000)
    recent=interaction(2,'current focused failure details\n'*200)
    messages=[ChatMessage('system','Rules'),ChatMessage('user','Task')]+old+recent
    def summarize(batch,limit):
        assert 'Do not invent a plan' in batch[0].content
        assert 'Event metadata:' in batch[1].content
        return ChatResponse('CURRENT WORK: fixing x.py; no check has been run.',model='scripted',usage={})
    stats=manager.prepare(messages,[],500,summarize)
    assert stats['retained_recent_turns']==1 and messages[-2:]==recent
    assert 'workspace and application state have not been reset' in messages[2].content


def test_revisiting_different_commands_does_not_trigger_repeated_action_stop():
    from mac_bench.runtime.agents.workflow import ReactAgent,AgentBudget
    from mac_bench.runtime.types import PublicTask
    from mac_bench.runtime.types import SolverWorkspace
    from mac_bench.runtime.tools import ToolSpec,ToolResult
    commands=['read a','read b','read c','read d']*2
    class Client:
        count=0
        def complete(self,messages,**kwargs):
            i=self.count;self.count+=1
            name,args=('inspect',{'command':commands[i]}) if i<len(commands) else ('finish',{'summary':'Done'})
            return ChatResponse('',model='scripted',usage={},tool_calls=({'id':str(i),
                'type':'function','function':{'name':name,'arguments':json.dumps(args)}},))
    tool=ToolSpec('inspect','Read file',{'command':{'type':'string'}},('command',),
        lambda c,a:ToolResult.completed({'output':'source'}))
    client=Client()
    agent=ReactAgent(client,None,workspace_tools=False,extra_tools=[tool],system_prompt='Rules',
        user_prompt='Task',budget=AgentBudget(max_model_calls=12,max_tool_calls=12,max_tokens=10000))
    result=agent.run(task=PublicTask('id','type','Task'),workspace=SolverWorkspace(None,cwd='.',kind='artifact'))
    assert result.stop_reason=='finished' and result.tool_calls==8


def test_truncated_summary_retries_once_without_accepting_partial_memory():
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    manager=WorkingContext(ContextPolicy(request_token_limit=6000,target_input_tokens=2000,
        summary_output_tokens=500))
    messages=[ChatMessage('system','Rules'),ChatMessage('user','Task')]+interaction(1,'x'*15000)
    original=list(messages);requests=[];events=[]
    def summarize(batch,limit):
        requests.append(batch)
        assert messages==original
        if len(requests)==1:raise ModelOutputTruncatedError('length')
        if len(requests)==2:
            assert 'previous summary exceeded' in batch[0].content
        return ChatResponse('CURRENT WORK: x.py unchanged. Checks not run.',model='scripted',usage={})
    stats=manager.prepare(messages,[],500,summarize,events.append)
    assert stats['summary_retries']==1 and stats['summary_attempts']==len(requests)
    assert any(e['kind']=='context_summary_retry' for e in events)
    assert 'Checks not run' in messages[2].content


def test_second_truncation_preserves_nonrecoverable_history_and_stops_cleanly():
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    manager=WorkingContext(ContextPolicy(request_token_limit=6000,target_input_tokens=2000,
        summary_output_tokens=500))
    messages=[ChatMessage('system','Rules'),ChatMessage('user','Task')]+interaction(1,'x'*15000)
    original=list(messages);attempts=[]
    def fail(batch,limit):attempts.append(batch);raise ModelOutputTruncatedError('length')
    with pytest.raises(ContextCapacityError, match='non-recoverable evidence'):
        manager.prepare(messages,[],500,fail)
    assert len(attempts)==2 and messages==original


def test_summary_failure_recovers_file_reads_without_losing_mutation_receipts():
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    manager = WorkingContext(ContextPolicy(request_token_limit=6000, target_input_tokens=2000,
        summary_output_tokens=500, recent_turns=1, recent_tokens=500))
    mutation = interaction('write', 'status=unknown request_id=R17 record_id=E12',
                           '{"command":"python edit.py"}')
    messages = [ChatMessage('system', 'Rules'), ChatMessage('user', 'Task')]
    messages += interaction('read', 'source\n' * 4000, '{"command":"cat src.py"}')
    messages += mutation + interaction('last', 'Latest finding', '{"command":"cat tests.py"}')
    events = []; limits = []
    def fail(batch, limit):
        limits.append(limit)
        raise ModelOutputTruncatedError('length')
    stats = manager.prepare(messages, [], 500, fail, events.append)
    assert limits == [500, 1000]
    assert stats['strategy'] == 'recoverable_read_fallback'
    assert all(m in messages for m in mutation)
    assert manager.tokens.count(messages) <= 5500
    assert any(e['kind'] == 'context_summary_fallback' for e in events)
    completed_turns(messages[3:])


def test_recent_evidence_is_retained_by_tokens_without_default_read_masking():
    manager = WorkingContext(ContextPolicy(request_token_limit=18000, target_input_tokens=10000,
        summary_output_tokens=1000, recent_turns=2, recent_tokens=7000))
    recent = [m for i in range(8) for m in interaction(i, 'Important source\n' * 50,
                                                     '{"command":"cat src.py"}')]
    messages = [ChatMessage('system', 'Rules'), ChatMessage('user', 'Task')]
    messages += interaction('old', 'older evidence\n' * 5000) + recent
    stats = manager.prepare(messages, [], 2000,
        lambda *args: ChatResponse('Old work inspected; no changes.', model='scripted', usage={}))
    assert stats['retained_recent_turns'] >= 8
    assert messages[-len(recent):] == recent
    assert stats['masked_read_outputs'] == 0


def test_old_read_masking_preserves_saved_full_output_path():
    import json
    from mac_bench.runtime.agents.context import mask_old_reads
    observation = json.dumps({'output': 'x' * 20000, 'exit_code': 0,
        'full_output_path': '/work/tool-output/episode/000001.log', 'output_bytes': 100000})
    turns = [interaction('read', observation, '{"command":"cat src.py"}')]
    masked, count = mask_old_reads(turns, 0, 1000)
    assert count == 1
    result = json.loads(masked[0][-1].content)
    assert result['full_output_path'] == '/work/tool-output/episode/000001.log'
    assert result['output_bytes'] == 100000 and len(result['output']) < 1500
