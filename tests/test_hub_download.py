"""Hub downloads pin revisions and install only the selected task dependencies."""
import hashlib
import json
import shutil
from types import SimpleNamespace

import huggingface_hub
import pytest

from mac_bench.data_cli import export_task
from mac_bench.download import download_hub_tasks
from mac_bench.environments.packaging.bundle import seal_bundle


@pytest.fixture
def hub(tmp_path, monkeypatch):
    task = tmp_path / 'source'
    (task / 'public').mkdir(parents=True)
    (task / 'public/task.txt').write_text('Complete the task.')
    bundle = seal_bundle(task, world_id='sample', public_files=['public/task.txt'],
                         metadata={'world_family': 'developer_release'})
    row = dict(task_id='sample', world_revision=bundle.manifest['revision'],
               category='developer_release', split='dev', bundle='tasks/sample')
    release = tmp_path / 'remote'
    entry = export_task(task, release / 'packages/sample.tar.gz', row)
    entry.update({k: row[k] for k in ('world_revision', 'category', 'split')})
    entry['archive'] = 'packages/sample.tar.gz'
    asset = 'environments/images/example'
    (release / asset).parent.mkdir(parents=True)
    (release / asset).write_bytes(b'shared image')
    entry['environment_assets'] = [asset]
    manifest = dict(tasks=[entry], assets={asset: {'sha256': hashlib.sha256(b'shared image').hexdigest()}})
    (release / 'downloads.json').write_text(json.dumps(manifest))
    calls = []
    class Api:
        def __init__(self, **kwargs): pass
        def dataset_info(self, repo_id, revision):
            assert (repo_id, revision) == ('owner/MAC-Bench', 'v1.0')
            return SimpleNamespace(sha='a' * 40)
    def fetch(*, filename, local_dir, **kwargs):
        assert kwargs['revision'] == 'a' * 40
        destination = local_dir / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or filename == 'downloads.json':
            shutil.copyfile(release / filename, destination)
        return str(destination)
    def snapshot(*, allow_patterns, local_dir, **kwargs):
        assert kwargs['revision'] == 'a' * 40 and kwargs['repo_type'] == 'dataset'
        calls.append(set(allow_patterns))
        for name in allow_patterns:
            fetch(filename=name, local_dir=local_dir, **kwargs)
    monkeypatch.setattr(huggingface_hub, 'HfApi', Api)
    monkeypatch.setattr(huggingface_hub, 'hf_hub_download', fetch)
    monkeypatch.setattr(huggingface_hub, 'snapshot_download', snapshot)
    return SimpleNamespace(root=tmp_path / 'installed', release=release, row=row,
                           manifest=manifest, asset=asset, calls=calls)


def run(hub):
    return download_hub_tasks(hub.root, [hub.row], repo_id='owner/MAC-Bench', revision='v1.0')


def test_download_pins_snapshot_selects_files_and_reuses_verified_install(hub):
    receipt = run(hub)
    assert receipt['commit'] == 'a' * 40
    assert hub.calls == [{hub.asset}, {'packages/sample.tar.gz'}]
    assert (hub.root / 'data/tasks/sample/public/task.txt').read_text() == 'Complete the task.'
    assert (hub.root / 'data' / hub.asset).read_bytes() == b'shared image'
    hub.calls.clear()
    run(hub)
    assert hub.calls == []
    (hub.root / 'data' / hub.asset).write_bytes(b'damaged')
    run(hub)
    assert hub.calls == [{hub.asset}]


def test_mismatched_task_revision_stops_before_downloading(hub):
    hub.manifest['tasks'][0]['world_revision'] = 'different'
    (hub.release / 'downloads.json').write_text(json.dumps(hub.manifest))
    with pytest.raises(ValueError, match='Dataset revision differs'):
        run(hub)
    assert hub.calls == []


def test_bad_environment_hash_does_not_install_task(hub):
    (hub.release / hub.asset).write_bytes(b'bad image')
    with pytest.raises(ValueError, match='Environment checksum differs'):
        run(hub)
    assert not (hub.root / 'data/tasks/sample').exists()


def test_installed_task_changes_are_not_overwritten(hub):
    run(hub)
    (hub.root / 'data/tasks/sample/public/task.txt').write_text('Local edit')
    hub.calls.clear()
    with pytest.raises(ValueError):
        run(hub)
    assert hub.calls == []


def test_corrupt_cached_package_is_downloaded_again(hub):
    cached = hub.root / '.cache/dataset/packages/sample.tar.gz'
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b'corrupt cached package')
    run(hub)
    assert (hub.root / 'data/tasks/sample/public/task.txt').read_text() == 'Complete the task.'


def test_overwrite_restores_installed_task_without_redownloading_images(hub):
    run(hub)
    (hub.root / 'data/tasks/sample/public/task.txt').write_text('Local edit')
    hub.calls.clear()
    download_hub_tasks(hub.root, [hub.row], repo_id='owner/MAC-Bench', revision='v1.0', overwrite=True)
    assert (hub.root / 'data/tasks/sample/public/task.txt').read_text() == 'Complete the task.'
    assert hub.calls == [{'packages/sample.tar.gz'}]


@pytest.mark.parametrize('transport', ['hub', 'mirror'])
@pytest.mark.parametrize('corrupted', [False, True])
def test_update_cleans_only_previously_installed_unmodified_blobs(hub, transport, corrupted):
    from mac_bench.download import download_tasks

    prefix = 'environments/images/blobs/sha256/'
    index = 'environments/images/index.json'
    def asset(content):
        name = prefix + hashlib.sha256(content).hexdigest()
        path = hub.release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return name, dict(url=name, sha256=hashlib.sha256(content).hexdigest())
    old, old_metadata = asset(b'old image')
    edited, edited_metadata = asset(b'locally editable old image')
    other, other_metadata = asset(b'another task current image')
    current, current_metadata = asset(b'current image')
    def publish(entries, selected, generation):
        payload = json.dumps({'schemaVersion': 2, 'generation': generation}).encode()
        (hub.release / index).write_bytes(payload)
        hub.manifest['assets'] = {**entries, index: dict(url=index, sha256=hashlib.sha256(payload).hexdigest())}
        hub.manifest['tasks'][0]['environment_assets'] = [index, *selected]
        (hub.release / 'downloads.json').write_text(json.dumps(hub.manifest))
    def install():
        if transport == 'hub':
            return run(hub)
        return download_tasks(hub.root, (hub.release / 'downloads.json').as_uri(), [hub.row])
    publish({old: old_metadata, edited: edited_metadata, other: other_metadata}, [old, edited, other], 1)
    install()
    (hub.root / 'data' / edited).write_bytes(b'user edits')
    unmanaged = hub.root / 'data' / prefix / ('f' * 64)
    unmanaged.write_bytes(b'unmanaged data')
    publish({current: current_metadata, other: other_metadata}, [current], 2)
    if corrupted:
        (hub.release / current).write_bytes(b'damaged download')
        with pytest.raises(ValueError, match='checksum'):
            install()
        assert (hub.root / 'data' / old).exists()
    else:
        install()
        assert not (hub.root / 'data' / old).exists()
        assert (hub.root / 'data' / current).read_bytes() == b'current image'
    assert (hub.root / 'data' / edited).read_bytes() == b'user edits'
    assert (hub.root / 'data' / other).read_bytes() == b'another task current image'
    assert unmanaged.read_bytes() == b'unmanaged data'
