"""Runtime acceleration must preserve dependencies and at-most-once dispatch."""
import json
import queue
from types import SimpleNamespace
import pytest
from mac_bench.environments.packaging.modal_session import ModalWorldSession
from mac_bench.environments.runtime.infrastructure.container_snapshot.coordinator import ContainerSnapshots
from mac_bench.environments.runtime.infrastructure.container_snapshot.engine import SnapshotError


def test_independent_services_start_before_readiness(monkeypatch):
    c=ContainerSnapshots.__new__(ContainerSnapshots)
    c.names=['db','web','other']
    c.spec={'dependencies':{'db':[],'web':['db'],'other':[]},'readiness_timeout_seconds':10}
    started=[]; probes=[]
    c.save=lambda **kw:None
    c.engine=SimpleNamespace(call=lambda method,path:started.append(path.split('/')[-2]),inspect=lambda name:{'Id':name,'State':{'Running':True}})
    def probe(name,info):
        probes.append(name)
        assert 'other' in started
        if name=='web':assert 'db' in probes
        return True
    c.probe=probe
    monkeypatch.setattr('mac_bench.environments.runtime.infrastructure.container_snapshot.coordinator.time.sleep',lambda _:None)
    assert set(c.start_services({n:{'Id':n} for n in c.names}))==set(c.names)
    assert started.index('other')<started.index('web')


def test_failed_dependency_does_not_start_child():
    c=ContainerSnapshots.__new__(ContainerSnapshots)
    c.names=['db','web'];c.spec={'dependencies':{'db':[],'web':['db']},'readiness_timeout_seconds':10}
    started=[];c.save=lambda **kw:None
    c.engine=SimpleNamespace(call=lambda method,path:started.append(path),inspect=lambda name:{'State':{'Running':False}})
    with pytest.raises(SnapshotError):c.start_services({'db':{'Id':'db'},'web':{'Id':'web'}})
    assert len(started)==1 and '/db/' in started[0]


class Stream:
    def __init__(self):self.items=queue.Queue()
    def __iter__(self):
        while True:
            x=self.items.get()
            if x is None:return
            yield x

class Process:
    def __init__(self, mismatch=False):
        self.stdout=Stream();self.stderr=Stream();self.stdin=self;self.calls=[];self.mismatch=mismatch
    def write(self,line):
        req=json.loads(line);self.calls.append(req)
        response={'error':{'type':'BadInput'}} if req['path']=='/agent/bad' else {'result':req['arguments']}
        self.stdout.items.put(json.dumps({'id':req['id']+int(self.mismatch),'response':response}))
    def drain(self):pass
    def write_eof(self):self.stdout.items.put(None);self.stderr.items.put(None)


def test_one_remote_process_for_multiple_calls():
    proc=Process();starts=[]
    def execute(*a,**k):starts.append(a);return proc
    s=ModalWorldSession(None,SimpleNamespace(raw=SimpleNamespace(exec=execute)))
    assert s.request('/owner/interface',{'x':1})=={'x':1}
    assert s.request('/owner/interface',{'x':2})=={'x':2}
    assert s.request('/agent/bad',{})=={'error':{'type':'BadInput'}}
    assert len(starts)==1 and len(proc.calls)==3
    s.close()
    with pytest.raises(RuntimeError,match='closed'):s.request('/owner/interface',{})


def test_transport_failure_never_replays_write():
    proc=Process(mismatch=True)
    s=ModalWorldSession(None,SimpleNamespace(raw=SimpleNamespace(exec=lambda *a,**k:proc)))
    with pytest.raises(RuntimeError,match='not replayed'):s.request('/agent/operate',{'write':True})
    with pytest.raises(RuntimeError,match='closed'):s.request('/agent/operate',{'write':True})
    assert len(proc.calls)==1
    s.close()


def test_pretix_worker_budget_keeps_native_job_worker():
    from mac_bench.environments.packaging.background import _bounded_pretix
    source=b'''AUTOMIGRATE=${AUTOMIGRATE:-yes}
export NUM_WORKERS=${NUM_WORKERS:-$NUM_WORKERS_DEFAULT}
exec celery -A pretix.celery_app worker -l info "$@"
'''
    result=_bounded_pretix(source).decode()
    assert 'NUM_WORKERS=2' in result and 'worker -l info --concurrency=2' in result
    assert 'AUTOMIGRATE=skip' in result
    with pytest.raises(RuntimeError):_bounded_pretix(b'unknown startup')


def test_reset_verifies_checkpoint_before_discarding(tmp_path):
    c=ContainerSnapshots.__new__(ContainerSnapshots)
    c.names=['app'];c.state={'phase':'resumed','current_ids':{'app':'old'}}
    calls=[]
    c.engine=SimpleNamespace(inspect=lambda n:{'Id':'old'},call=lambda *a,**k:calls.append(a))
    def invalid(token):raise SnapshotError('container_snapshot_manifest_changed')
    c.verified=invalid
    with pytest.raises(SnapshotError,match='manifest_changed'):c.restore({})
    assert not calls


def test_reset_discards_old_state_without_capturing_it(tmp_path,monkeypatch):
    from copy import deepcopy
    c=ContainerSnapshots.__new__(ContainerSnapshots)
    c.names=['app'];c.state={'phase':'resumed','current_ids':{'app':'old'}}
    old={'Id':'old','Config':{'Image':'initial'},'HostConfig':{},'Mounts':[]}
    new={**deepcopy(old),'Id':'new'}
    calls=[]
    def call(method,path,**kw):
        calls.append((method,path,kw))
        if method=='POST':return {'Id':'new'}
    c.engine=SimpleNamespace(inspect=lambda n:old,stopped=lambda n:new,call=call)
    c.verified=lambda token:(tmp_path,{'containers':{'app':old},'mounts':{},'images':{'app':{'id':'initial'}}})
    c.topology=lambda current:{}
    def save(phase=None,**updates):
        if phase:c.state['phase']=phase
        c.state.update(updates)
    c.save=save
    monkeypatch.setattr('mac_bench.environments.runtime.infrastructure.container_snapshot.coordinator.create_body',lambda *a:{'Image':'initial','HostConfig':{}})
    result=c.restore({})
    assert result['storage_restored'] and c.state['phase']=='restored'
    assert calls[0]==('DELETE','/containers/old',{'query':{'v':'false','force':'true'}})
    assert all('/commit' not in path for _,path,_ in calls)
