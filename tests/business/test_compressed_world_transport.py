import base64,gzip,json,shlex
from types import SimpleNamespace
import pytest
from mac_bench.environments.packaging.session import WorldSession


@pytest.mark.parametrize('size', [2, 10000])
def test_streaming_client_compresses_large_responses(monkeypatch, capsys, size):
    import io
    from mac_bench.environments.packaging import rpc_client
    raw=json.dumps({'result':{'text':'中文','rows':['payload']*size}},ensure_ascii=False).encode()
    requests=[{'id':i,'path':'/owner/observe','arguments':{}} for i in [1,2]]
    monkeypatch.setattr(rpc_client.sys,'stdin',io.StringIO('\n'.join(map(json.dumps,requests))+'\n'))
    monkeypatch.setattr(rpc_client,'urlopen',lambda *a,**kw:io.BytesIO(raw))
    rpc_client.main()
    lines=capsys.readouterr().out.splitlines()
    assert len(lines)==2
    assert {json.loads(line)['id'] for line in lines} == {1,2}
    for line in lines:
        envelope=json.loads(line)
        response=envelope['response']
        if size==10000:
            assert response['transport']=='gzip-base64/v1'
            response=json.loads(gzip.decompress(base64.b64decode(response['data'])))
        assert response==json.loads(raw)




def test_joint_owner_capture_uses_one_observation():
    class Session(WorldSession):
        def __init__(self):self.calls=[]
        def request(self,path,arguments):
            self.calls.append(path)
            return {'observation':{'value':1},'evaluation':{'gc':0}}
    session=Session()
    assert session.observe_and_evaluate()=={'observation':{'value':1},'evaluation':{'gc':0}}
    assert session.calls==['/owner/observe_and_evaluate']
