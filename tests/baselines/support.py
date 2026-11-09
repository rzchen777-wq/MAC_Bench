"""Scripted models and local worlds shared by baseline integration tests."""
import json
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
import numpy as np
from mac_bench.runtime.llm.client import ChatResponse
from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.evaluation import task_record,open_task
from mac_bench.environments.packaging.bundle import seal_bundle
from mac_bench.environments.packaging.interface import business_guide
from mac_bench.runtime.sandbox.base import CommandResult
from mac_bench.baselines.aorchestra.tasks import AOrchestra
from mac_bench.baselines.meta_team.tasks import MetaTeam
import subprocess
import time
from mac_bench.evaluation.episodes.budget import EpisodeBudget
import httpx
from mac_bench.evaluation.episodes.recording import RecordingClient
from mac_bench.runtime.llm.config import LLMConfig

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
    backend = None
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
        (root/'private/agent_instructions.txt').write_text(business_guide(root))
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

class LocalFixtureBackend:
    """Execute the same remote worker as a subprocess, only for fixture tests."""
    def exec(self, sandbox, command, *, cwd=None, timeout):
        started = time.monotonic()
        process = subprocess.run(command, shell=True, cwd=cwd, timeout=timeout,
                                 capture_output=True, text=True)
        return CommandResult(command, process.returncode, process.stdout + process.stderr,
                             time.monotonic() - started)

    def open_channel(self, sandbox, *, argv, cwd):
        return LocalChannel(argv, cwd)

    def read_file(self, sandbox, *, path):
        return Path(path).read_bytes()

class LocalChannel:
    def __init__(self, argv, cwd):
        self.process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    def write(self, data):
        pending = memoryview(data)
        while pending:
            pending = pending[self.process.stdin.write(pending):]
        self.process.stdin.flush()

    def read(self):
        while chunk := self.process.stdout.read(65536):
            yield chunk

    def close(self):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=10)
            assert code == 0, self.process.stderr.read().decode()
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.process.stderr.close()

def sse(*events):
    return b''.join(b'event: ' + event['type'].encode() + b'\r\ndata: '
                    + json.dumps(event).encode() + b'\r\n\r\n' for event in events)

START = {'type': 'message_start', 'message': {'id': 'msg-fixture', 'type': 'message',
    'role': 'assistant', 'model': 'deepseek-v4-flash', 'content': [],
    'stop_reason': None, 'stop_sequence': None,
    'usage': {'input_tokens': 7, 'output_tokens': 0, 'cache_read_input_tokens': 11,
              'cache_creation_input_tokens': 3}}}

END = {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
       'usage': {'output_tokens': 5}}

STREAM = sse(START,
    {'type': 'content_block_start', 'index': 0,
     'content_block': {'type': 'thinking', 'thinking': ''}},
    {'type': 'content_block_delta', 'index': 0,
     'delta': {'type': 'thinking_delta', 'thinking': 'Read the public requirements.'}},
    {'type': 'content_block_delta', 'index': 0,
     'delta': {'type': 'signature_delta', 'signature': 'fixture-signature'}},
    {'type': 'content_block_stop', 'index': 0},
    {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
    {'type': 'content_block_delta', 'index': 1,
     'delta': {'type': 'text_delta', 'text': 'The shared workspace is ready.'}},
    {'type': 'content_block_stop', 'index': 1}, END, {'type': 'message_stop'})

class Fragments(httpx.SyncByteStream):
    def __init__(self, data):
        self.data = data

    def __iter__(self):
        for i in range(0, len(self.data), 7):
            yield self.data[i:i + 7]

def recorder(tmp_path, calls=20):
    config = LLMConfig('https://api.deepseek.com', 'provider-secret', 'deepseek-v4-flash',
                       timeout_seconds=2, max_retries=0)
    budget = EpisodeBudget(AgentBudget(max_model_calls=calls, max_tool_calls=20,
        max_env_actions=20, max_tokens=10000, max_wall_seconds=120))
    return RecordingClient(SimpleNamespace(config=config), tmp_path / 'calls', budget=budget)

def post(relay, session='worker-a', request=None):
    body = json.dumps(request or {'model': relay.config.model, 'stream': True, 'max_tokens': 128,
                                 'messages': [{'role': 'user', 'content': 'hello'}]}).encode()
    return httpx.post(relay.base_url + '/v1/messages', content=body, headers={
        'x-api-key': relay.token, 'x-deepseek-harness-session-id': session,
        'x-deepseek-harness-compact': '1' if session == 'compact' else '0'}, timeout=5, trust_env=False)
