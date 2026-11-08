"""Download task archives and their shared environment files."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import urlopen


def download_file(url, destination, checksum):
    destination = Path(destination)
    if destination.exists():
        with destination.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == checksum:
                return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            digest = hashlib.sha256()
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != checksum:
            raise ValueError("Download checksum differs: " + destination.name)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def download_tasks(root, url, rows, *, overwrite=False):
    from mac_bench.data_cli import install_archive, verify_task

    root = Path(root)
    with urlopen(url, timeout=30) as response:
        manifest = json.load(response)
    available = {row["task_id"]: row for row in manifest["tasks"]}
    assets = manifest.get("assets", {})
    needed_archives, needed_assets = [], set()
    for row in rows:
        item = available[row["task_id"]]
        if any(item[key] != row[key] for key in ('world_revision', 'category', 'split')):
            raise ValueError(f"Dataset revision differs for {row['task_id']}; use the matching code and dataset release")
        destination = root / 'data' / row['bundle']
        if destination.exists() and not overwrite:
            verify_task(destination, row)
        else:
            needed_archives.append((item, row, _relative_file(item['archive'])))
        needed_assets.update(_relative_file(name) for name in item.get('environment_assets', []))
    for name in sorted(needed_assets):
        asset = assets[name]
        download_file(urljoin(url, asset['url']), root / 'data' / name, asset['sha256'])
    for item, row, name in needed_archives:
        archive = root / '.cache/dataset' / name
        download_file(urljoin(url, name), archive, item['sha256'])
        install_archive(archive, root / 'data' / row['bundle'], row, overwrite=overwrite)
    _record_environment_install(root, assets, needed_assets)
    for row in rows:
        print('Installed', row['task_id'])


def _relative_file(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Invalid release file path')
    return path.as_posix()


def download_hub_tasks(root, rows, *, repo_id, revision, token=None, max_workers=4, overwrite=False):
    """Install selected tasks from one pinned Hub snapshot, with parallel downloads and local reuse."""
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download
    from mac_bench.data_cli import install_archive, verify_task

    root = Path(root)
    cache = root / '.cache/dataset'
    commit = HfApi(token=token).dataset_info(repo_id, revision=revision).sha
    options = dict(repo_id=repo_id, repo_type='dataset', revision=commit, token=token)
    manifest_path = hf_hub_download(filename='downloads.json', local_dir=cache, **options)
    manifest = json.loads(Path(manifest_path).read_text())
    available = {item['task_id']: item for item in manifest['tasks']}
    needed_archives, needed_assets = {}, set()
    for row in rows:
        item = available[row['task_id']]
        if any(item[key] != row[key] for key in ('world_revision', 'category', 'split')):
            raise ValueError(f"Dataset revision differs for {row['task_id']}; use the matching code and dataset release")
        destination = root / 'data' / row['bundle']
        if destination.exists() and not overwrite:
            verify_task(destination, row)
        else:
            needed_archives[_relative_file(item['archive'])] = (item, row)
        needed_assets.update(item.get('environment_assets', []))
    assets = manifest.get('assets', {})
    pending = []
    for name in sorted(needed_assets):
        name = _relative_file(name)
        path = root / 'data' / name
        if not _matches(path, assets[name]['sha256']):
            # HF metadata can still point to this revision after a local edit.
            # Remove a known-bad file so its cached metadata cannot bypass repair.
            path.unlink(missing_ok=True)
            pending.append(name)
    print(f'Dataset: {repo_id}@{commit[:12]} | {len(rows)} tasks | '
          f'{len(needed_archives)} task packages, {len(pending)} environment files to download', flush=True)
    if pending:
        snapshot_download(local_dir=root / 'data', allow_patterns=pending,
                          max_workers=max_workers, **options)
        for name in pending:
            if not _matches(root / 'data' / name, assets[name]['sha256']):
                raise ValueError('Environment checksum differs: ' + name)
    if needed_archives:
        for name, (item, _) in needed_archives.items():
            if not _matches(cache / name, item['sha256']):
                (cache / name).unlink(missing_ok=True)
        snapshot_download(local_dir=cache, allow_patterns=list(needed_archives),
                          max_workers=max_workers, **options)
        for name, (item, row) in needed_archives.items():
            archive = cache / name
            if not _matches(archive, item['sha256']):
                raise ValueError('Task package checksum differs: ' + name)
            install_archive(archive, root / 'data' / row['bundle'], row, overwrite=overwrite)
    _record_environment_install(root, assets, needed_assets)
    for row in rows:
        print('Installed', row['task_id'])
    receipt = dict(repo_id=repo_id, revision=revision, commit=commit)
    (cache / 'release.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def _matches(path, checksum):
    if not path.is_file():
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == checksum


def _record_environment_install(root, assets, selected):
    """Record installed assets and remove superseded, unmodified managed blobs."""
    from mac_bench.runtime.common.artifacts import write_json

    index = 'environments/images/index.json'
    if index not in selected:
        return
    receipt = root / '.cache/dataset/installed_assets.json'
    previous = json.loads(receipt.read_text()) if receipt.exists() else {}
    prefix = 'environments/images/blobs/sha256/'
    for name, checksum in previous.items():
        relative = _relative_file(name)
        filename = Path(relative).name
        if (relative.startswith(prefix) and relative == prefix + filename
                and len(filename) == 64 and filename == checksum
                and all(c in '0123456789abcdef' for c in filename)
                and relative not in assets):
            path = root / 'data' / relative
            if _matches(path, checksum):
                path.unlink()
    installed = {name: checksum for name, checksum in previous.items()
                 if name in assets and checksum == assets[name]['sha256']}
    installed.update({name: assets[name]['sha256'] for name in selected})
    write_json(installed, receipt)
