"""Cache invalidation and transfer counts for portable business environments."""
import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from mac_bench.environments.business import runtime, worker


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'repository_root', lambda: tmp_path)
    root = tmp_path / 'task'
    for name, content in {
        'environment/environment.json': json.dumps({'services':[{'name':'app','image':'app'}]}),
        'environment/state/initial.tar.gz': 'initial volume',
        'environment/host/auth.json': 'test host configuration',
        'environment/runtime/tool_bindings.json': 'runtime configuration',
        'private/profile.py': 'profile',
        'private/initial.json': '{}',
        'private/verifier.json': '{}',
        'public/task.txt': 'task wording',
    }.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
    images=tmp_path/'data/environments/images'
    (images/'blobs/sha256').mkdir(parents=True)
    (images/'blobs/sha256'/('a'*64)).write_bytes(b'image')
    (images/'index.json').write_text(json.dumps({'schemaVersion':2,'manifests':[{
        'digest':'sha256:'+'a'*64, 'mediaType':'application/vnd.oci.image.layer.v1.tar',
        'annotations':{'org.opencontainers.image.ref.name':'app'}}]}))
    return SimpleNamespace(root=root,manifest={'world_id':'sample','revision':'task-1','metadata':{}})


def test_cache_tracks_application_data_not_prompt_verifier_or_runtime(bundle):
    key=lambda: runtime.PortableBusinessWorld(bundle,backend=object()).snapshot_name
    initial=key()
    bundle.manifest['revision']='task-2'
    for name in ('public/task.txt','private/verifier.json','environment/runtime/tool_bindings.json'):
        (bundle.root/name).write_text('updated')
        assert key()==initial
    for name in ('environment/state/initial.tar.gz','environment/host/auth.json','environment/environment.json'):
        path=bundle.root/name
        if path.name=='environment.json':
            path.write_text(json.dumps({'services':[{'name':'app','image':'app'}],'new_setting':True}))
        else:path.write_text('updated')
        assert key()!=initial
        initial=key()


def test_archive_excludes_solvers_generation_and_software_builds():
    with tarfile.open(fileobj=io.BytesIO(runtime.runtime_source()),mode='r:gz') as archive:
        names=archive.getnames()
    assert 'mac_bench/environments/business/worker.py' in names
    assert 'mac_bench/environments/tools/api.py' in names
    assert not any('/baselines/' in n or '/evaluation/' in n or '/engineering/' in n for n in names)


@pytest.mark.parametrize('cached',[False,True])
def test_one_code_upload_and_one_state_restore_per_open_no_upload_on_reset(bundle,monkeypatch,cached):
    # Other tasks can share a layer that was not needed/downloaded for this task.
    images = runtime.repository_root() / 'data/environments/images'
    index = json.loads((images / 'index.json').read_text())
    for tag in ('other-task-a', 'other-task-b'):
        index['manifests'].append({'digest': 'sha256:' + 'b' * 64,
            'mediaType': 'application/vnd.oci.image.layer.v1.tar',
            'annotations': {'org.opencontainers.image.ref.name': tag}})
    (images / 'index.json').write_text(json.dumps(index))
    class Backend:
        def __init__(self):self.writes=[];self.commands=[];self.snapshots=0
        def snapshot_exists(self,**kwargs):return cached
        def create(self,**kwargs):return SimpleNamespace(id='sandbox')
        def write_file(self,sandbox,*,path,content):self.writes.append(path)
        def write_files(self,sandbox,*,files):self.writes.extend(files)
        def exec(self,sandbox,command,**kwargs):
            self.commands.append(command);return SimpleNamespace(exit_code=0,stdout='')
        def create_snapshot(self,*args,**kwargs):self.snapshots+=1
        def delete(self,*args):pass
    class Image:
        def apt_install(self,*args):return self
        def pip_install(self,*args):return self
        def add_local_dir(self,*args,**kwargs):return self
    class Session:
        def __init__(self,*args):pass
        def observe(self):return {}
        def observe_digest(self):return runtime.digest({})
        def close(self):pass
        def stop_episode(self):pass
    installs=[]
    monkeypatch.setattr(runtime,'native_host_image',Image)
    monkeypatch.setattr(runtime,'ModalWorldSession',Session)
    monkeypatch.setattr(runtime,'start_service',lambda *args:None)
    monkeypatch.setattr(runtime,'install_public_resources',lambda *args,**kw:installs.append('resources'))
    monkeypatch.setattr(runtime,'install_private_verifier',lambda *args,**kw:installs.append('verifier'))
    backend=Backend()
    with runtime.PortableBusinessWorld(bundle,backend=backend) as world:
        assert any('rm -rf /work/world &&' in c for c in backend.commands)
        assert backend.writes.count('/work/runtime.tar.gz')==1
        assert backend.writes.count('/work/world/tool_bindings.json')==1
        restores=lambda: sum(c.endswith('worker /work/environment') for c in backend.commands)
        assert restores()==1
        writes=list(backend.writes)
        world.reset()
        assert restores()==2
        assert backend.writes==writes
        assert installs==['resources','verifier']
        assert backend.snapshots==(0 if cached else 1)


def test_prepare_imports_images_without_restoring_volumes_or_creating_containers(tmp_path,monkeypatch):
    (tmp_path/'environment.json').write_text(json.dumps({'services':[{'name':'app'}]}))
    loaded=[]
    monkeypatch.setattr(worker,'load_images',lambda *args:loaded.append(True))
    def forbidden(*args,**kwargs):raise AssertionError('preparation must not restore application state')
    monkeypatch.setattr(worker,'Engine',forbidden)
    worker.restore(tmp_path,start=False)
    assert loaded==[True]


def test_shared_blobs_are_separate_from_task_specific_layers(tmp_path):
    common = "a" * 64
    first = "b" * 64
    second = "c" * 64
    manifests = []
    blobs = tmp_path / "blobs/sha256"
    blobs.mkdir(parents=True)
    for tag, value, specific in [("one", "d" * 64, first), ("two", "e" * 64, second)]:
        layers = [{"digest": "sha256:" + x, "mediaType": "application/vnd.oci.image.layer.v1.tar"}
                  for x in (common, specific)]
        (blobs / value).write_text(json.dumps({"layers": layers}))
        manifests.append({"digest": "sha256:" + value,
                          "mediaType": "application/vnd.oci.image.manifest.v1+json",
                          "annotations": {"org.opencontainers.image.ref.name": tag}})
    (tmp_path / "index.json").write_text(json.dumps({"manifests": manifests}))
    shared = set(runtime.shared_image_blobs(tmp_path))
    assert shared == {"blobs/sha256/" + common}
    _, one = runtime.selected_images(tmp_path, {"one"})
    assert set(one) - shared == {"blobs/sha256/" + first, "blobs/sha256/" + "d" * 64}


def test_prepared_cache_uses_explicit_format_version(bundle, monkeypatch):
    first = runtime.PortableBusinessWorld(bundle, backend=object()).snapshot_name
    monkeypatch.setattr(runtime, 'PREPARED_FORMAT', runtime.PREPARED_FORMAT + 1)
    second = runtime.PortableBusinessWorld(bundle, backend=object()).snapshot_name
    assert first != second
