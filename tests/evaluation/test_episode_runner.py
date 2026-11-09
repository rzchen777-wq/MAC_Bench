import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.evaluation import run_episode, EpisodeBudget, BudgetExceeded
from mac_bench.evaluation.tasks import task_record,open_task
from mac_bench.evaluation.tasks.adapters.operational import OperationalTask
from mac_bench.environments.packaging.bundle import seal_bundle
from mac_bench.environments.packaging.interface import business_guide
from mac_bench.runtime.llm.client import ChatMessage,ChatResponse
from mac_bench.runtime.sandbox.base import CommandResult


class CodeWorld:
    def __init__(self):self.changed=False;self.closed=False;self.gradings=0
    def open(self):self.closed=False
    def close(self):self.closed=True
    def reset(self):self.changed=False
    def execute(self,command,*,timeout):self.changed=True;return CommandResult(command,0,'done',.01)
    def export_submission(self,path):path.write_text('changed' if self.changed else 'starter');return path
    def grade_submission(self,path):
        self.gradings+=1;ok=path.read_text()=='changed'
        return {'status':'passed' if ok else 'failed','task_success':ok,'gc':float(ok)}


def task(tmp_path,world):
    root=tmp_path/'bundle';(root/'public').mkdir(parents=True);(root/'private').mkdir()
    (root/'public/task.txt').write_text('Implement the actual source behavior.')
    (root/'private/secret.txt').write_text('SECRET')
    b=seal_bundle(root,world_id='code',public_files=['public/task.txt'],metadata={'world_family':'platform_data'})
    return open_task(task_record(b.root),tmp_path/'runtime',world=world)


class Client:
    config=SimpleNamespace(model='scripted')
    def complete(self,*args,**kwargs):return ChatResponse(content='ok',model='scripted',usage={'prompt_tokens':10,'completion_tokens':2},finish_reason='stop')


def test_cli_sigterm_finishes_episode_and_closes_its_world(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    program = '''
import os,signal,sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from test_episode_runner import CodeWorld,Client,task
from mac_bench.evaluation import __main__ as cli
from mac_bench.evaluation.episodes.runner import run_episode
class Interrupted:
    name='signal-fixture'
    def run(self, context):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError('SIGTERM did not stop execution')
root=Path(sys.argv[1]); world=CodeWorld()
cli._main=lambda argv: run_episode(task(root,world),Interrupted(),Client(),output=root/'episode',close=True)
try:
    cli.main([])
finally:
    (root/'closed.txt').write_text(str(world.closed))
'''
    process = subprocess.run([sys.executable, '-c', program, str(tmp_path), str(Path(__file__).parent)],
                             capture_output=True, text=True, timeout=15)
    assert process.returncode == 143, process.stdout + process.stderr
    assert (tmp_path / 'closed.txt').read_text() == 'True'
    result = json.loads((tmp_path / 'episode/result.json').read_text())
    assert result['closed'] and result['status'] == 'error'
    assert result['error_type'] == 'SystemExit'


def test_truncated_model_response_retains_body_and_counts_usage(tmp_path):
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    from mac_bench.evaluation.episodes.recording import RecordingClient
    class Truncated(Client):
        def complete(self, *args, **kwargs):
            response = ChatResponse('Incomplete working notes', model='scripted',
                usage={'prompt_tokens': 30, 'completion_tokens': 20}, finish_reason='length')
            raise ModelOutputTruncatedError('length', response=response)
    budget = EpisodeBudget(AgentBudget(max_tokens=40))
    client = RecordingClient(Truncated(), tmp_path / 'calls', budget=budget)
    with pytest.raises(ModelOutputTruncatedError):
        client.complete([ChatMessage('user', 'Task')])
    record = json.loads((tmp_path / 'calls/0001.json').read_text())
    assert record['response_incomplete'] and record['response']['content'] == 'Incomplete working notes'
    assert record['usage'] == {'prompt_tokens': 30, 'completion_tokens': 20}
    assert budget.snapshot()['input_tokens'] == client.input_tokens == 30
    assert budget.snapshot()['output_tokens'] == client.output_tokens == 20
    with pytest.raises(BudgetExceeded, match='tokens'):
        client.complete([ChatMessage('user', 'Retry')])


def test_transport_retries_record_each_attempt_and_preserve_usage(tmp_path, monkeypatch):
    from mac_bench.evaluation.episodes import recording
    monkeypatch.setattr(recording.time, 'sleep', lambda seconds: None)

    class Transient(Client):
        attempts = 0

        def complete(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts < 3:
                error = RuntimeError('temporary upstream failure')
                error.status_code = 503
                raise error
            return super().complete(*args, **kwargs)

    budget = EpisodeBudget(AgentBudget(max_tokens=40, max_model_calls=3))
    client = recording.RecordingClient(Transient(), tmp_path / 'calls', budget=budget)
    response = client.complete([ChatMessage('user', 'Task')])
    records = [json.loads(p.read_text()) for p in sorted((tmp_path / 'calls').glob('*.json'))]
    assert [r['status'] for r in records] == ['error', 'error', 'completed']
    assert len({r['identity']['logical_call_id'] for r in records}) == 1
    assert len({r['identity']['provider_attempt_id'] for r in records}) == 3
    assert response.usage == {'prompt_tokens': 10, 'completion_tokens': 2}
    assert client.completed_calls == 1 and client.count == 3
    assert client.input_tokens == 10 and client.output_tokens == 2
    with pytest.raises(BudgetExceeded, match='model'):
        client.complete([ChatMessage('user', 'Again')])


def test_http_error_response_does_not_mask_provider_exception(tmp_path):
    import httpx
    from mac_bench.evaluation.episodes.recording import RecordingClient

    error = RuntimeError('provider connection failed')
    error.response = httpx.Response(502)

    class Failed(Client):
        def complete(self, *args, **kwargs):
            raise error

    budget = EpisodeBudget(AgentBudget(max_tokens=40))
    client = RecordingClient(Failed(), tmp_path / 'calls', budget=budget)
    with pytest.raises(RuntimeError, match='provider connection failed') as captured:
        client.complete([ChatMessage('user', 'Task')])
    assert captured.value is error
    record = json.loads((tmp_path / 'calls/0001.json').read_text())
    assert record['error_type'] == 'RuntimeError' and 'usage' not in record
    assert client.input_tokens == client.output_tokens == 0


class Method:
    name='custom'
    def run(self,ctx):
        assert not hasattr(ctx,'evaluate') and not hasattr(ctx.task,'bundle')
        ctx.client.complete([ChatMessage('user',ctx.task.instruction)])
        ctx.environment.call('terminal',{'command':'edit source'})
        return {'stop_reason':'finished'}


def test_same_runner_accepts_engineering_and_preserves_record(tmp_path):
    world=CodeWorld();r=run_episode(task(tmp_path,world),Method(),Client(),output=tmp_path/'episode')
    assert r['status']=='completed' and r['ps']==r['rr']==1 and r['closed']
    assert r['model_calls']==1 and r['tool_calls']==1 and r['input_tokens']==10
    assert world.gradings==1 and world.closed
    assert (tmp_path/'episode/model_calls/0001.json').exists()
    assert 'SECRET' not in (tmp_path/'episode/model_calls/0001.json').read_text()
    events=[json.loads(l) for l in (tmp_path/'episode/events.jsonl').read_text().splitlines()]
    assert [e['kind'] for e in events]==['task_tool_start','task_tool_end']


@pytest.mark.parametrize('reason', ['context_capacity', 'context_summary_failed', 'model_output_truncated'])
def test_context_stop_is_an_execution_failure_even_with_valid_grading(tmp_path, reason):
    class Blocked(Method):
        def run(self, ctx):
            return {'stop_reason': reason}
    world = CodeWorld()
    result = run_episode(task(tmp_path, world), Blocked(), Client(), output=tmp_path / 'episode')
    assert result['status'] == 'execution_error' and result['closed']
    assert result['gc'] == 0 and result['model_calls'] == result['tool_calls'] == 0


def test_provider_error_after_write_is_graded_once_and_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_API_KEY', 'private-example-credential')
    class Broken(Method):
        def run(self,ctx):
            super().run(ctx);raise RuntimeError('provider failed private-example-credential')
    world=CodeWorld();r=run_episode(task(tmp_path,world),Broken(),Client(),output=tmp_path/'episode')
    assert r['status']=='error' and r['ps']==r['rr']==1 and r['error_type']=='RuntimeError'
    assert world.gradings==1 and world.closed
    evidence = (tmp_path/'episode/owner_errors.jsonl').read_text()
    assert 'provider failed [REDACTED]' in evidence
    assert 'private-example-credential' not in evidence
    assert 'execution_or_startup' in evidence and 'RuntimeError' in evidence


def test_budget_exhaustion_stops_all_workers_and_still_grades(tmp_path):
    class TooMany(Method):
        def run(self,ctx):
            ctx.environment.call('terminal',{'command':'one'})
            ctx.environment.call('terminal',{'command':'two'})
    world=CodeWorld();r=run_episode(task(tmp_path,world),TooMany(),Client(),output=tmp_path/'episode',budget=AgentBudget(max_tool_calls=1))
    assert r['status']=='completed' and r['stop_reason']=='budget:tool_calls' and r['tool_calls']==1 and world.closed


def test_invalid_evaluator_score_is_error_not_zero(tmp_path):
    world=CodeWorld();world.grade_submission=lambda p:{'status':'passed','task_success':True,'gc':.4}
    r=run_episode(task(tmp_path,world),Method(),Client(),output=tmp_path/'episode')
    assert r['status']=='evaluation_error' and 'ps' not in r and world.closed


def test_owner_can_reuse_infrastructure_with_explicit_reset(tmp_path):
    world=CodeWorld();handle=task(tmp_path,world)
    first=run_episode(handle,Method(),Client(),output=tmp_path/'first',close=False)
    assert not first['closed'] and handle.is_open
    handle.reset()
    second=run_episode(handle,Method(),Client(),output=tmp_path/'second',close=False)
    assert second['status']=='completed' and first['instance_id']!=second['instance_id']
    handle.close();assert world.closed


def test_shared_concurrent_budget_does_not_multiply_by_worker_count():
    budget=EpisodeBudget(AgentBudget(max_model_calls=3))
    def admit(_):
        try:budget.admit_model();return True
        except BudgetExceeded:return False
    with ThreadPoolExecutor(8) as pool:assert sum(pool.map(admit,range(20)))==3
    assert budget.snapshot()['model_call_attempts']==3


def test_same_runner_accepts_operational_adapter(tmp_path):
    root=tmp_path/'bundle';(root/'public').mkdir(parents=True);(root/'private').mkdir()
    (root/'public/task.txt').write_text('Adjust live records.');(root/'private/initial.json').write_text('{"value":0}')
    (root/'private/agent_instructions.txt').write_text(business_guide(root))
    b=seal_bundle(root,world_id='ops',public_files=['public/task.txt'],metadata={'scenario_family':'business_finance','tool_api_version':2})
    class Session:
        value=0;active=False
        def observe(self):return {'value':self.value}
        def start_episode(self,i):self.active=True
        def stop_episode(self):self.active=False
        def agent_call(self,n,a):assert self.active;self.value=1;return {'data':{'value':1}}
        def evaluate(self):assert not self.active;return {'status':'passed','complete':True,'task_success':True,'gc':1}
        def observe_and_evaluate(self):return {'observation':self.observe(),'evaluation':self.evaluate()}
        def export_receipts(self,p):p.write_text('receipt')
    class Owner:
        backend = None
        def __init__(self):self.session=Session();self.closed=False
        def __enter__(self):return self
        def close(self):self.closed=True
    class Native(Method):
        def run(self,ctx):
            ctx.environment.call('call_tool',{'tool_name':'write','arguments':{},'request_id':'unique'})
            return {'stop_reason':'finished'}
    owner=Owner();handle=OperationalTask(task_record(b.root),tmp_path/'runtime',owner=owner)
    r=run_episode(handle,Native(),Client(),output=tmp_path/'episode')
    assert r['ps']==r['rr']==1 and owner.closed and (tmp_path/'episode/native_calls.sqlite').exists()


def test_recording_failure_releases_model_admission(tmp_path,monkeypatch):
    from mac_bench.evaluation.episodes import recording
    budget=EpisodeBudget()
    gateway=recording.RecordingClient(Client(),tmp_path/'model',budget=budget)
    def broken(*a,**k):raise OSError('storage failed')
    monkeypatch.setattr(recording,'write_json',broken)
    with pytest.raises(OSError):gateway.complete([ChatMessage('user','task')])
    budget.finish()
    assert budget.snapshot()['model_call_attempts']==1
    with pytest.raises(BudgetExceeded,match='episode_closed'):budget.admit_model()


def test_method_agent_events_are_measured_without_guessing(tmp_path):
    class Team(Method):
        def run(self,ctx):
            for agent in ['a','b']:ctx.event({'kind':'agent_start','agent_id':agent})
            result=super().run(ctx)
            for agent in ['a','b']:ctx.event({'kind':'agent_end','agent_id':agent})
            return result
    r=run_episode(task(tmp_path,CodeWorld()),Team(),Client(),output=tmp_path/'episode')
    assert r['active_agents_peak']==2 and r['agents_used']==['a','b'] and r['agents_active_at_return']==0
