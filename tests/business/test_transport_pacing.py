"""Native request pacing must not introduce implicit mutation retries."""
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from mac_bench.environments.errors import CompileFailure
from mac_bench.environments.runtime.infrastructure import transport as module


def clock(monkeypatch):
    now=[0.0]
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(module.time,'sleep',lambda delay:now.__setitem__(0,now[0]+delay))
    return now


def test_pacing_covers_distinct_write_endpoints_but_does_not_retry(monkeypatch):
    now=clock(monkeypatch)
    transport=module.RestTransport('http://localhost:6068',headers={},min_request_interval=6.2)
    observed=[]
    class Response:
        status=201
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def read(self):return b'{"id":1}'
    def send(request,**kwargs):
        observed.append((request.method,now[0]))
        if request.full_url.endswith('/fail'):
            raise HTTPError(request.full_url,429,'rate limited',{},None)
        return Response()
    transport.opener=Mock(open=send)
    transport.request('POST','/accounts',{})
    transport.request('GET','/accounts/1')
    transport.request('POST','/manual-journals',{})
    with pytest.raises(CompileFailure,match='429'):
        transport.request('POST','/fail',{})
    assert observed==[('POST',0),('GET',0),('POST',6.2),('POST',12.4)]
    assert now[0]==12.4  # No hidden retries or backoff after the failed mutation.


@pytest.mark.parametrize('interval',[-1,True,float('nan'),float('inf'),'6'])
def test_pacing_rejects_invalid_configuration(interval):
    with pytest.raises(ValueError):
        module.RestTransport('http://localhost',headers={},min_request_interval=interval)


def test_declared_routes_keep_record_ids_and_query_variants_in_one_bucket(monkeypatch):
    now = clock(monkeypatch)
    transport = module.RestTransport('http://localhost', headers={}, min_request_interval=6.2)
    sent = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"id":1}'
    def send(request, **kwargs):
        sent.append((request.full_url, now[0]))
        return Response()
    transport.opener = Mock(open=send)
    transport.request('GET', '/invoices/1', rate_limit_key='/invoices/:id')
    transport.request('GET', '/bills/1', rate_limit_key='/bills/:id')
    transport.request('GET', '/invoices/2?expand=true', rate_limit_key='/invoices/:id')
    transport.request('GET', '/bills/2', rate_limit_key='/bills/:id')
    assert [t for _, t in sent] == [0, 0, 6.2, 6.2]


def test_sleeping_bucket_does_not_block_unrelated_endpoint(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    asleep, release = Event(), Event()
    monkeypatch.setattr(module.time, 'monotonic', lambda: 0.0)
    def sleep(delay):
        asleep.set()
        assert release.wait(2)
    monkeypatch.setattr(module.time, 'sleep', sleep)
    transport = module.RestTransport('http://localhost', headers={}, min_request_interval=6.2)
    transport._pace('GET', '/invoices/:id')
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(transport._pace, 'GET', '/invoices/:id')
        try:
            assert asleep.wait(1)
            pool.submit(transport._pace, 'GET', '/bills/:id').result(timeout=1)
        finally:
            release.set()
        waiting.result()
