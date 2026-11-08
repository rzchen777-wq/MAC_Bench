"""Real stream relay + loopback HTTP + tool service, with deterministic app doubles."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
import json
import subprocess
import time

import pytest

from mac_bench.environments.packaging.modal_session import ModalWorldSession
from mac_bench.environments.packaging import rpc_client
from mac_bench.environments.packaging.server import create_server
from mac_bench.environments.tools import CallJournal, NativeResult, Role, Tool, WorldTools
from mac_bench.environments.tools.contracts import object_schema
from mac_bench.environments.tools.registry import ToolRegistry


@pytest.fixture
def running(tmp_path):
    lock = Lock()
    state = {'active': 0, 'peak': 0, 'by_app': {}, 'app_peak': {}, 'writes': [], 'completed': []}
    held, release = Event(), Event()
    def handler(app):
        def execute(_, args):
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
                state['by_app'][app] = state['by_app'].get(app,0)+1
                state['app_peak'][app] = max(state['app_peak'].get(app,0), state['by_app'][app])
            try:
                if args['value'] == -1:
                    held.set(); assert release.wait(5)
                else:
                    time.sleep(.05)
                with lock:
                    state['writes'].append((app,args['value']))
                    state['completed'].append(args['value'])
                return NativeResult({'value': args['value'], 'app': app})
            finally:
                with lock:
                    state['active'] -= 1; state['by_app'][app] -= 1
        return execute
    apps = ['hrms','pretix','twenty','bigcapital']
    tools = [Tool(a+'.write',a,a+'.record','write',a,{},'Test write',
        object_schema({'value':{'type':'integer'}},['value']),'Native record',False,handler(a)) for a in apps]
    journal = CallJournal(tmp_path/'calls.sqlite')
    service = WorldTools('world',ToolRegistry(tools,catalog_revision='test'),
        [Role('operator',tuple(SimpleNamespace(application=a,scope={}) for a in apps),('*',))],journal)
    world = SimpleNamespace(agent=service.agent('team','operator'))
    server = create_server(world,tmp_path,0)
    thread = Thread(target=server.serve_forever,daemon=True); thread.start()
    source = tmp_path/'src/mac_bench/environments/packaging/rpc_client.py'
    source.parent.mkdir(parents=True)
    source.write_text(Path(rpc_client.__file__).read_text().replace('127.0.0.1:6099',f'127.0.0.1:{server.server_port}'))
    processes=[]
    class Input:
        def __init__(self,p): self.p=p
        def write(self,data): self.p.stdin.write(data)
        def drain(self):
            if not self.p.stdin.closed:self.p.stdin.flush()
        def write_eof(self): self.p.stdin.close()
    def execute(*args,**kwargs):
        p=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
        processes.append(p)
        return SimpleNamespace(stdin=Input(p),stdout=p.stdout,stderr=p.stderr)
    session=ModalWorldSession(None,SimpleNamespace(raw=SimpleNamespace(exec=execute)),root=str(tmp_path))
    try:
        yield session,state,held,release,processes
    finally:
        release.set();session.close()
        for p in processes:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
            p.stdout.close();p.stderr.close()
        server.shutdown();server.server_close();thread.join(timeout=5)
        service.close();journal.close()


def call(session,app,value,request):
    return session.agent_call('call_tool',{'tool_name':app+'.write','arguments':{'value':value},'request_id':request})


def test_real_relay_matches_out_of_order_results_and_overlaps_apps(running):
    session,state,held,release,processes=running
    with ThreadPoolExecutor(max_workers=4) as pool:
        slow=pool.submit(call,session,'hrms',-1,'held')
        try:
            assert held.wait(2)
            futures=[pool.submit(call,session,a,i,a) for i,a in enumerate(['pretix','twenty','bigcapital'])]
            results=[f.result(timeout=2) for f in futures]
            assert [r['data']['value'] for r in results]==[0,1,2]
            assert not slow.done()
        finally:release.set()
        assert slow.result(timeout=2)['data']['value']==-1
    assert state['peak']>=3 and len(processes)==1
    assert all(v==1 for v in state['app_peak'].values())


def test_same_app_duplicates_and_different_arguments_are_ordered(running):
    session,state,_,_,_=running
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(call,session,'twenty',1,'same') for _ in range(3)]
        futures += [pool.submit(call,session,'twenty',i,f'new-{i}') for i in range(2,5)]
        results=[f.result(timeout=3) for f in futures]
    assert {r['call_id'] for r in results[:3]}=={results[0]['call_id']}
    assert len(state['writes'])==4 and state['app_peak']['twenty']==1
    conflict=call(session,'twenty',8,'same')
    assert conflict['error']['code']=='request_id_conflict' and len(state['writes'])==4


def test_lost_reply_reconnects_without_repeating_native_write(running):
    session,state,_,_,processes=running
    original=session._decode_response
    once=True
    def lose(envelope):
        nonlocal once
        result=original(envelope)
        if once:
            once=False
            raise EOFError('Injected lost response')
        return result
    session._decode_response=lose
    failed=call(session,'pretix',5,'lost')
    assert failed['error']['outcome']=='unknown'
    recovered=session.agent_call('read_result',{'request_id':'lost','path':'/data/value'})
    assert recovered['value']==5 and recovered['status']=='succeeded'
    assert len(state['writes'])==1 and len(processes)==2
    assert call(session,'pretix',6,'continue')['data']['value']==6
