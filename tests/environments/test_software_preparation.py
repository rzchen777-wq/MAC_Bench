import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from mac_bench.environments.engineering import preparation
from mac_bench.environments.engineering.product import runtime
from mac_bench.runtime.sandbox.base import PreparedImage, SandboxHandle, SandboxBackend
from mac_bench.runtime.sandbox.images import image_directory


@pytest.fixture(params=['modal','daytona'])
def backend(request):
    class Backend:
        provider=request.param
        image_scope={'provider':request.param,'test':'scope'}
        validate_image=SandboxBackend.validate_image
        def __init__(self): self.images=set(); self.calls=[]; self.fail=None; self.number=0
        def create(self,**kw):
            assert kw.get('image')=='python:3.12'
            assert 'prepared' not in kw
            self.calls.append('create')
            return SandboxHandle('preparer',self.provider,None)
        def exec(self,sandbox,command,**kw):
            self.calls.append(command)
            return SimpleNamespace(exit_code=int(command==self.fail),stdout='test output',duration_seconds=0)
        def write_files(self,sandbox,*,files): self.calls.append(('files',files))
        def image_exists(self,image): return image.reference in self.images
        def capture_image(self,sandbox,*,recipe,timeout):
            self.number+=1;ref='image-'+str(self.number);self.images.add(ref);self.calls.append('capture')
            return PreparedImage(self.provider,ref,recipe,self.image_scope)
        def delete(self,sandbox): self.calls.append('delete')
    return Backend()


def test_prepare_reuse_rebuild_and_failure(tmp_path,monkeypatch,backend):
    monkeypatch.setenv('MAC_BENCH_ENVIRONMENT_CACHE',str(tmp_path/'cache'))
    env={'image':'python:3.12','setup':'base install','dependency_setup':'locked install',
         'dependency_contents':{'requirements.txt':'example==1'}}
    def prepare(run,**kwargs): return preparation.prepare_environment(env,tmp_path/run,backend=backend,**kwargs)
    first=prepare('first')
    assert backend.calls==['create','base install',('files',{'/opt/mac-bench-dependencies/requirements.txt':b'example==1'}),'locked install','capture','delete']
    backend.calls.clear();assert prepare('reuse')==first;assert backend.calls==[]
    second=prepare('rebuild',rebuild=True);assert second.reference!=first.reference
    assert json.loads((tmp_path/'first/environment/image.json').read_text())['reference']==first.reference
    assert json.loads((tmp_path/'rebuild/environment/image.json').read_text())['reference']==second.reference
    backend.fail='locked install'
    with pytest.raises(RuntimeError,match='Locked dependency setup failed'):prepare('failed',rebuild=True)
    assert backend.calls[-1]=='delete'
    backend.calls.clear();assert prepare('after-failure')==second;assert backend.calls==[]
    backend.fail=None;backend.images.remove(second.reference)
    third=prepare('missing');assert third.reference!=second.reference
    backend.calls.clear()
    def denied(image): raise PermissionError('denied')
    backend.image_exists=denied
    with pytest.raises(PermissionError):prepare('denied')
    assert backend.calls==[]


def test_resource_scope_and_providers_have_distinct_cache_keys():
    assert image_directory('software',{'provider':'modal'},'recipe') != image_directory('software',{'provider':'daytona'},'recipe')
    assert image_directory('software',{'provider':'daytona','disk':10},'recipe') != image_directory('software',{'provider':'daytona','disk':20},'recipe')


def test_invalid_verifier_output_is_not_a_candidate_failure(tmp_path):
    backend = SimpleNamespace(upload_tree=lambda *a, **kw: None)
    world = runtime.ProductRuntime({'verify': 'verify'}, tmp_path, backend=backend)
    world.sandbox = 'sandbox'
    world.command = lambda name, *a, **kw: SimpleNamespace(
        exit_code=0 if name == 'verify-normal' else 1, stdout='')
    assert world.verify(tmp_path, {})['status'] == 'verifier_error'
