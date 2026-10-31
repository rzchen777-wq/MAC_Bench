"""The RPC relay runs from the current installed runtime, over literal stdin."""
import json
import subprocess
from types import SimpleNamespace
from mac_bench.environments.packaging.modal_session import ModalWorldSession


def test_current_runtime_persistent_process(tmp_path):
    client=tmp_path/'src/mac_bench/environments/packaging/rpc_client.py'
    client.parent.mkdir(parents=True)
    client.write_text('import json,sys\nfor line in sys.stdin:\n r=json.loads(line); print(json.dumps({"id":r["id"],"response":{"result":r["arguments"]}}),flush=True)\n')
    processes=[]
    class Input:
        def __init__(self,p):self.p=p
        def write(self,data):self.p.stdin.write(data)
        def drain(self):
            if not self.p.stdin.closed:self.p.stdin.flush()
        def write_eof(self):self.p.stdin.close()
    def execute(*args,**kwargs):
        p=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
        processes.append(p)
        return SimpleNamespace(stdin=Input(p),stdout=p.stdout,stderr=p.stderr)
    session=ModalWorldSession(None,SimpleNamespace(raw=SimpleNamespace(exec=execute)),root=str(tmp_path))
    try:
        for value in ['中文','$(touch /tmp/SHOULD_NOT_RUN) `echo unsafe`\nquote"', 'x'*100000]:
            assert session.request('/owner/interface',{'text':value})=={'text':value}
        assert len(processes)==1
    finally:
        session.close()
        for p in processes:
            p.wait(timeout=5);p.stdout.close();p.stderr.close()
