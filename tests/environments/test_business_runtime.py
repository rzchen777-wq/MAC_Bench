"""Portable startup preserves dependencies and native process budgets."""
from types import SimpleNamespace
import pytest
from mac_bench.environments.packaging.modal_session import ModalWorldSession
from mac_bench.environments.business.services import BusinessServices
from mac_bench.environments.business.containers import BusinessEnvironmentError


def test_independent_services_start_before_readiness(monkeypatch):
    c=BusinessServices(None, names=['db','web','other'],
        dependencies={'db':[],'web':['db'],'other':[]}, readiness={}, timeout=10)
    started=[]; probes=[]

    c.engine=SimpleNamespace(call=lambda method,path:started.append(path.split('/')[-2]),inspect=lambda name:{'Id':name,'State':{'Running':True}})
    def probe(name,info):
        probes.append(name)
        assert 'other' in started
        if name=='web':assert 'db' in probes
        return True
    c.probe=probe
    monkeypatch.setattr('mac_bench.environments.business.services.time.sleep',lambda _:None)
    assert set(c.start({n:{'Id':n} for n in c.names}))==set(c.names)
    assert started.index('other')<started.index('web')


def test_failed_dependency_does_not_start_child():
    c=BusinessServices(None, names=['db','web'],
        dependencies={'db':[],'web':['db']}, readiness={}, timeout=10)
    started=[];
    c.engine=SimpleNamespace(call=lambda method,path:started.append(path),inspect=lambda name:{'State':{'Running':False}})
    with pytest.raises(BusinessEnvironmentError):c.start({'db':{'Id':'db'},'web':{'Id':'web'}})
    assert len(started)==1 and '/db/' in started[0]


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
