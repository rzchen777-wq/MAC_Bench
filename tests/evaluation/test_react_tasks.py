from dataclasses import asdict
from types import SimpleNamespace
import pytest
from mac_bench.baselines.react_single.tasks import ReactSingle
from mac_bench.evaluation.episodes.protocol import EpisodeContext
from mac_bench.evaluation.episodes.budget import EpisodeBudget
from mac_bench.evaluation.tasks import TaskView,ToolDefinition,AgentEnvironment
from mac_bench.runtime.llm.client import ChatResponse


def test_react_uses_task_tools_and_no_business_prompt_for_code():
    calls=[]
    definition=ToolDefinition('terminal','Edit actual code',{'type':'object','properties':{'command':{'type':'string'}},'required':['command'],'additionalProperties':False})
    env=AgentEnvironment([definition],lambda n,a:calls.append(a) or {'exit_code':0,'output':'done'})
    budget=EpisodeBudget();env.begin(admit_call=budget.admit_tool,event=lambda e:None)
    class Client:
        count=0
        def complete(self,messages,**kwargs):
            assert not any('records are already live' in (m.content or '') for m in messages)
            self.count+=1
            name,args=('terminal','{"command":"edit"}') if self.count==1 else ('finish','{"summary":"done"}')
            return ChatResponse(content='',model='scripted',usage={'prompt_tokens':2,'completion_tokens':1},finish_reason='tool_calls',
                tool_calls=({'id':str(self.count),'type':'function','function':{'name':name,'arguments':args}},))
    view=TaskView('code','revision','Complete source code.',(definition,),'Edit /work/project through terminal.')
    result=ReactSingle().run(EpisodeContext(view,env,Client(),budget,lambda e:None));env.end()
    assert calls==[{'command':'edit'}] and asdict(result)['model_calls']==2


@pytest.mark.parametrize('tool_name', ['terminal', 'call_tool'])
def test_recovery_uses_public_tools_and_shared_budget_once(tmp_path, tool_name):
    from mac_bench.evaluation.episodes.recording import RecordingClient
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    import json
    calls = []
    definition = ToolDefinition(tool_name, 'Perform an action', {
        'type': 'object', 'properties': {'command': {'type': 'string'}},
        'required': ['command'], 'additionalProperties': False})
    env = AgentEnvironment([definition], lambda n, a: calls.append(a) or {'output': 'done'})
    budget = EpisodeBudget()
    env.begin(admit_call=budget.admit_tool, event=lambda e: None)
    class Client:
        count = 0
        def complete(self, messages, **kwargs):
            self.count += 1
            name, args = (tool_name, '{"command":"edit"}') if self.count < 3 else ('finish', '{"summary":"done"}')
            response = ChatResponse('', model='scripted', usage={'prompt_tokens': 2, 'completion_tokens': 1},
                finish_reason='length' if self.count == 1 else 'tool_calls',
                tool_calls=({'id': str(self.count), 'type': 'function', 'function': {'name': name, 'arguments': args}},))
            if self.count == 1:
                raise ModelOutputTruncatedError('length', response=response)
            return response
    client = RecordingClient(Client(), tmp_path / 'calls', budget=budget)
    view = TaskView('task', 'revision', 'Complete the task.', (definition,), 'Use the provided tools.')
    result = ReactSingle().run(EpisodeContext(view, env, client, budget, lambda e: None))
    env.end()
    assert next(e['stop_reason'] for e in result.trace if e['kind'] == 'agent_stop') == 'finished'
    assert calls == [{'command': 'edit'}]
    assert result.input_tokens == client.input_tokens == budget.snapshot()['input_tokens'] == 6
    assert result.output_tokens == client.output_tokens == budget.snapshot()['output_tokens'] == 3
    assert budget.snapshot()['model_call_attempts'] == 3
    records = [json.loads(p.read_text()) for p in sorted((tmp_path / 'calls').glob('*.json'))]
    assert [r['max_tokens'] for r in records] == [8192, 16384, 8192]
    assert records[0]['response_incomplete']
    assert not any(m.get('tool_calls') for m in records[1]['messages'])
