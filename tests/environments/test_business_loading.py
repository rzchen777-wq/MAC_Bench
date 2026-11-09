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
    key=lambda: runtime.PortableBusinessWorld(bundle,backend=object()).image_recipe
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
    assert 'mac_bench/environments/business/services.py' in names
    assert not any('/container_snapshot/' in n for n in names)
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
    from mac_bench.runtime.sandbox.base import PreparedImage, SandboxBackend
    from mac_bench.runtime.sandbox.images import image_directory, write_record
    monkeypatch.setenv('MAC_BENCH_ENVIRONMENT_CACHE',str(bundle.root.parent/'cache'))
    class Backend:
        provider='modal'
        image_scope={'provider':'modal','test':'business'}
        validate_image=SandboxBackend.validate_image
        def __init__(self):self.writes=[];self.commands=[];self.snapshots=0;self.batches=[]
        def image_exists(self,image):return cached
        def business_image(self,*args):return object()
        def create(self,**kwargs):return SimpleNamespace(id='sandbox')
        def write_file(self,sandbox,*,path,content):self.writes.append(path)
        def write_files(self,sandbox,*,files):
            self.writes.extend(files);self.batches.append(dict(files))
        def exec(self,sandbox,command,**kwargs):
            self.commands.append(command);return SimpleNamespace(exit_code=0,stdout='')
        def capture_business_image(self,sandbox,*,recipe,timeout):
            self.snapshots+=1
            return PreparedImage(self.provider,'prepared',recipe,self.image_scope)
        def delete(self,*args):pass
    class Session:
        def __init__(self,*args):pass
        def observe(self):return {}
        def observe_digest(self):return runtime.digest({})
        def close(self):pass
        def stop_episode(self):pass
    monkeypatch.setattr(runtime,'WorldSession',Session)
    monkeypatch.setattr(runtime,'start_service',lambda *args:None)
    monkeypatch.setattr(runtime,'public_resource_files',lambda bundle:{'/work/world/record_forms.json':b'forms'})
    monkeypatch.setattr(runtime,'private_verifier_files',lambda bundle:{'/work/world/verifier.json':b'verifier', '/work/world/profile.py':b'profile'})
    backend=Backend()
    world=runtime.PortableBusinessWorld(bundle,backend=backend)
    if cached:
        write_record(image_directory('business',backend.image_scope,world.image_recipe)/'image.json',
            PreparedImage(backend.provider,'cached',world.image_recipe,backend.image_scope))
    with world:
        assert any('rm -rf /work/world &&' in c for c in backend.commands)
        assert backend.writes.count('/work/runtime.tar.gz')==1
        assert backend.writes.count('/work/world/tool_bindings.json')==1
        runtime_batches = [b for b in backend.batches if '/work/world/tool_bindings.json' in b]
        assert len(runtime_batches)==1
        assert runtime_batches[0]['/work/world/record_forms.json']==b'forms'
        assert runtime_batches[0]['/work/world/verifier.json']==b'verifier'
        assert runtime_batches[0]['/work/world/profile.py']==b'profile'
        batch_count=len(backend.batches)
        restores=lambda: sum(c.endswith('worker /work/environment') for c in backend.commands)
        assert restores()==1
        writes=list(backend.writes)
        world.reset()
        assert restores()==2
        assert backend.writes==writes
        assert len(backend.batches)==batch_count
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
    first = runtime.PortableBusinessWorld(bundle, backend=object()).image_recipe
    monkeypatch.setattr(runtime, 'PREPARED_FORMAT', runtime.PREPARED_FORMAT + 1)
    second = runtime.PortableBusinessWorld(bundle, backend=object()).image_recipe
    assert first != second


def test_hrms_restore_disables_remote_update_check_without_changing_snapshot(tmp_path, monkeypatch):
    configuration = {'Env': ['KEEP=value', 'FRAPPE_DOCKER_BUILD=0'], 'HostConfig': {}}
    config = {'services': [{'name': 'runtime-hrms', 'configuration': configuration}],
              'dependencies': {'runtime-hrms': []}, 'networks': {}, 'volumes': {},
              'mounts': {}, 'readiness': {}}
    path = tmp_path / 'environment.json'
    original = json.dumps(config)
    path.write_text(original)
    created = []
    class Engine:
        def call(self, method, path, **kwargs):
            if method == 'GET': return []
            if path == '/containers/create': created.append(kwargs['body'])
        def inspect(self, name): return {'Id': name}
    monkeypatch.setattr(worker, 'load_images', lambda *args: None)
    monkeypatch.setattr(worker, 'Engine', lambda *args: Engine())
    monkeypatch.setattr(worker, 'create_body', lambda config, tag: dict(config))
    monkeypatch.setattr(worker.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, 'BusinessServices', lambda *args, **kwargs: SimpleNamespace(start=lambda current: None))
    for _ in range(2):
        worker.restore(tmp_path)
    assert all(body['Env'] == ['KEEP=value', 'FRAPPE_DOCKER_BUILD=1'] for body in created)
    assert path.read_text() == original


def test_build_context_path_reuses_shared_uploads_across_tasks(tmp_path):
    source=tmp_path/'images'; source.mkdir()
    (source/'blob').write_bytes(b'immutable')
    cache=tmp_path/'contexts'
    first=runtime.cached_image_context(source,cache,['blob'])
    second=runtime.cached_image_context(source,cache,['blob'])
    assert first == second and (first/'blob').read_bytes()==b'immutable'
    assert (first/'blob').stat().st_ino == (source/'blob').stat().st_ino
    different=runtime.cached_image_context(source,cache,['blob'],index={'manifests':[]})
    assert different != first


def test_image_import_keeps_shared_blobs_until_last_consumer_and_reset_needs_no_archive(tmp_path, monkeypatch):
    import hashlib
    images = tmp_path/'images'
    blobs = images/'blobs/sha256'
    blobs.mkdir(parents=True)
    def blob(data, media='application/vnd.oci.image.layer.v1.tar+gzip'):
        value = hashlib.sha256(data).hexdigest()
        (blobs/value).write_bytes(data)
        return {'digest':'sha256:'+value,'mediaType':media}
    shared = blob(b'shared')
    manifests = []
    required = {}
    for name, size in [('small',10),('large',2000)]:
        own = blob(name.encode()*size)
        manifest = blob(json.dumps({'layers':[shared,own]}).encode(), 'application/vnd.oci.image.manifest.v1+json')
        manifests.append({**manifest,'annotations':{'org.opencontainers.image.ref.name':name}})
        required[name] = {d['digest'].split(':')[1] for d in [shared, own, manifest]}
    (images/'index.json').write_text(json.dumps({'manifests':manifests}))
    config = {'services':[{'name':n,'image':n} for n in ('small','large')]}
    imported = []
    def run(command, **kwargs):
        if command[:3] == ['docker','image','inspect']:
            return SimpleNamespace(returncode=0 if command[-1].split('/')[1].split(':')[0] in imported else 1)
        raise AssertionError(command)
    def import_image(images, name, tag):
        assert required[name] <= {p.name for p in blobs.iterdir()}
        if name == 'small':
            assert not (required['large'] - required['small']) & {p.name for p in blobs.iterdir()}
        imported.append(name)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker.subprocess,'run',run)
    monkeypatch.setattr(worker,'import_image',import_image)
    worker.load_images(tmp_path,config)
    assert imported == ['large','small']
    assert not list(blobs.iterdir())
    (images/'index.json').unlink()
    worker.load_images(tmp_path,config)
    assert imported == ['large','small']


@pytest.mark.parametrize('producer_code,consumer_code,expected', [
    (0, 0, None), (7, 0, 7), (0, 9, 9),
])
def test_oci_import_streams_bytes_and_propagates_both_failures(tmp_path, monkeypatch,
                                                            producer_code, consumer_code, expected):
    import os
    import subprocess
    import sys
    from mac_bench.environments.business import worker
    binary = tmp_path / 'bin'
    binary.mkdir()
    received = tmp_path / 'received'
    for name, body in {
        'skopeo': 'import sys\nsys.stdout.buffer.write(b"layer-data" * 300000)\nsys.exit(' + str(producer_code) + ')\n',
        'docker': 'import sys,pathlib\ndata=sys.stdin.buffer.read()\npathlib.Path(' + repr(str(received)) + ').write_bytes(data)\nsys.exit(' + str(consumer_code) + ')\n',
    }.items():
        script = binary / name
        script.write_text('#!' + sys.executable + '\n' + body)
        script.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ['PATH'])
    if expected is None:
        worker.import_image(tmp_path / 'images', 'source', 'mac-bench/test:initial')
    else:
        with pytest.raises(subprocess.CalledProcessError) as error:
            worker.import_image(tmp_path / 'images', 'source', 'mac-bench/test:initial')
        assert error.value.returncode == expected
    assert received.read_bytes() == b'layer-data' * 300000
    assert not list(tmp_path.glob('*.tar'))
