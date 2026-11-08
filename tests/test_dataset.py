import io
import json
import tarfile
from pathlib import Path

import pytest
from mac_bench.cli import main
from mac_bench.data_cli import import_task, export_task, install_archive
from mac_bench.evaluation.tasks.catalog import load_catalog
from mac_bench.environments.packaging.bundle import seal_bundle
from mac_bench.runtime.paths import repository_root


@pytest.fixture
def task_data(tmp_path):
    base=tmp_path/'data';world=base/'tasks/sample';(world/'public').mkdir(parents=True)
    (world/'public/task.txt').write_text('Complete the task.')
    bundle=seal_bundle(world,world_id='sample',public_files=['public/task.txt'],
                       metadata={'world_family':'developer_release'})
    row={'task_id':'sample','world_revision':bundle.manifest['revision'],
         'category':'developer_release','split':'dev','bundle':'tasks/sample'}
    (base/'tasks.json').write_text(json.dumps({'format':'collaboration.tasks.v1','tasks':[row]}))
    return base,world,row


def test_selected_task_does_not_require_other_packages(task_data):
    base,world,row=task_data
    missing={**row,'task_id':'missing','bundle':'tasks/missing','world_revision':'other'}
    (base/'tasks.json').write_text(json.dumps({'format':'collaboration.tasks.v1','tasks':[row,missing]}))
    assert load_catalog(base/'tasks.json',task_ids=['sample'])[0].bundle==world
    with pytest.raises(FileNotFoundError):load_catalog(base/'tasks.json')


def test_import_checks_identity_and_does_not_copy_extra_files(task_data,tmp_path):
    base,world,row=task_data
    (world/'extra.txt').write_text('not in the task package')
    target=tmp_path/'imported'
    assert import_task(world,target,row)
    assert not (target/'extra.txt').exists()
    assert import_task(world,target,row) is False
    (target/'public/task.txt').write_text('tampered')
    with pytest.raises(ValueError,match='changed'):import_task(world,target,row)


def test_archive_roundtrip(task_data,tmp_path):
    _,world,row=task_data
    archive=tmp_path/'task.tar.gz';target=tmp_path/'installed'
    result=export_task(world,archive,row)
    assert result['bytes']>0 and len(result['sha256'])==64
    assert install_archive(archive,target,row)
    assert (target/'manifest.json').read_bytes()==(world/'manifest.json').read_bytes()


@pytest.mark.parametrize('name,kind',[('../escape','file'),('/escape','file'),('link','symlink')])
def test_archive_rejects_escaping_members(task_data,tmp_path,name,kind):
    _,_,row=task_data
    archive=tmp_path/'bad.tar'
    with tarfile.open(archive,'w') as out:
        member=tarfile.TarInfo(name)
        if kind=='symlink':member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd'
        else:member.size=1
        out.addfile(member,io.BytesIO(b'x'))
    with pytest.raises(ValueError):install_archive(archive,tmp_path/'target',row)
    assert not (tmp_path/'target/manifest.json').exists()


def test_unknown_task_does_not_load_assets(task_data):
    base,_,_=task_data
    with pytest.raises(ValueError,match='Unknown'):load_catalog(base/'tasks.json',task_ids=['no-such-task'])


def test_public_index_matches_catalogue():
    root=repository_root()
    public=json.loads((root/'data/public_tasks.json').read_text())['tasks']
    rows=json.loads((root/'data/tasks.json').read_text())['tasks']
    assert len(public)==len(rows)==54
    assert {r['task_id'] for r in public}=={r['task_id'] for r in rows}
    assert sum(r['split']=='test' for r in rows)==48
    assert sum(r['split']=='dev' for r in rows)==6
    for row in rows:
        entry=next(r for r in public if r['task_id']==row['task_id'])
        assert 'collection' not in entry
        assert entry['world_revision']==row['world_revision']
        assert row['bundle']=='tasks/'+row['task_id']
        assert 'acceptance' not in row


def test_offline_listing(capsys):
    assert main(['tasks','list','--split','test','--json'])==0
    assert len(json.loads(capsys.readouterr().out))==48


def test_dry_run_only_loads_selected_task(task_data,monkeypatch,capsys):
    base,_,row=task_data
    from mac_bench.evaluation import __main__ as entry
    def forbidden(*a,**k):raise AssertionError('No provider allocation on dry run')
    monkeypatch.setattr(entry,'open_task',forbidden)
    assert entry.main(['--catalog',str(base/'tasks.json'),'--task-id','sample','--dry-run'])==0
    assert json.loads(capsys.readouterr().out)['task_id']=='sample'


def test_default_catalogue_selects_task(task_data,monkeypatch,capsys):
    base,_,_=task_data
    from mac_bench.runtime import paths
    monkeypatch.setattr(paths,'repository_root',lambda:base.parent)
    assert main(['evaluate','--task-id','sample','--dry-run'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['task_id']=='sample'
    assert result['world']==str(base/'tasks/sample')


def test_export_is_reproducible_across_names_and_file_timestamps(task_data, tmp_path):
    import os
    _, world, row = task_data
    first = export_task(world, tmp_path / 'first.tar.gz', row)
    os.utime(world / 'public/task.txt', (1234567890, 1234567890))
    second = export_task(world, tmp_path / 'second.tar.gz', row)
    assert first['sha256'] == second['sha256']


def test_explicit_overwrite_replaces_task_and_removes_extra_files(task_data, tmp_path):
    _, world, row = task_data
    target = tmp_path / 'installed'
    import_task(world, target, row)
    (target / 'public/task.txt').write_text('Local edits')
    (target / 'extra.txt').write_text('Extra data')
    archive = tmp_path / 'task.tar.gz'
    export_task(world, archive, row)
    assert install_archive(archive, target, row, overwrite=True)
    assert (target / 'public/task.txt').read_text() == 'Complete the task.'
    assert not (target / 'extra.txt').exists()
    assert not list(tmp_path.glob('.task-*'))


def test_invalid_replacement_preserves_installed_task(task_data, tmp_path):
    _, world, row = task_data
    target = tmp_path / 'installed'
    import_task(world, target, row)
    (world / 'public/task.txt').write_text('Corrupt package')
    with pytest.raises(ValueError):
        import_task(world, target, row, overwrite=True)
    assert (target / 'public/task.txt').read_text() == 'Complete the task.'


def test_environment_verification_checks_only_selected_blobs(task_data, tmp_path):
    import hashlib
    from mac_bench.data_cli import verify_environment_assets
    base, world, row = task_data
    (world / 'environment').mkdir()
    (world / 'environment/environment.json').write_text(json.dumps({'services':[{'image':'app'}]}))
    images = base / 'environments/images'
    blobs = images / 'blobs/sha256'
    blobs.mkdir(parents=True)
    (images / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
    content=b'layer';digest=hashlib.sha256(content).hexdigest();path=blobs/digest;path.write_bytes(content)
    (images/'index.json').write_text(json.dumps({'manifests':[
        {'digest':'sha256:'+digest, 'mediaType':'application/vnd.oci.image.layer.v1.tar',
         'annotations':{'org.opencontainers.image.ref.name':'app'}},
        {'digest':'sha256:'+'0'*64, 'mediaType':'application/vnd.oci.image.layer.v1.tar',
         'annotations':{'org.opencontainers.image.ref.name':'unselected'}}]}))
    checked=set()
    assert verify_environment_assets(base.parent,row,checked=checked)==1
    assert checked=={path}
    path.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='Environment checksum differs'):
        verify_environment_assets(base.parent,row)
    path.unlink()
    with pytest.raises(FileNotFoundError,match='Missing environment file'):
        verify_environment_assets(base.parent,row,checksums=False)
