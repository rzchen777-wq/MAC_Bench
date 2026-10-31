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
