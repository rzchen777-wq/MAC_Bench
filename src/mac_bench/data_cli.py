"""Verify, import and package the current task data."""
import argparse
import json
import gzip
import hashlib
import shutil
import tarfile
import tempfile
from hashlib import sha256
from pathlib import Path

from mac_bench.runtime.paths import repository_root
from mac_bench.evaluation.tasks.catalog import task_record


def catalog_rows(root, task_ids=None):
    path = Path(root) / 'data' / 'tasks.json'
    rows = json.loads(path.read_text())['tasks']
    ids = [row['task_id'] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate task ID')
    if task_ids:
        if set(task_ids) - set(ids):
            raise ValueError('Unknown task ID')
        rows = [row for row in rows if row['task_id'] in task_ids]
    for row in rows:
        relative = Path(row['bundle'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid task directory')
        if not (path.parent / relative).resolve().is_relative_to(path.parent.resolve()):
            raise ValueError('Task directory escapes the dataset')
    return path.parent, rows


def verify_task(path, row):
    from mac_bench.environments.packaging.bundle import WorldBundle
    WorldBundle.load(path, verify=True)
    return task_record(path, task_id=row['task_id'], revision=row['world_revision'],
                       category=row['category'], split=row['split'])


def import_task(source, destination, row, *, overwrite=False):
    source, destination = Path(source), Path(destination)
    verify_task(source, row)
    if destination.exists() and not overwrite:
        verify_task(destination, row)
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.task-', dir=destination.parent) as directory:
        target = Path(directory) / 'task'
        target.mkdir()
        manifest = json.loads((source / 'manifest.json').read_text())
        for name in ['manifest.json', *manifest['files']]:
            item = target / name
            item.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, item)
        verify_task(target, row)
        previous = Path(directory) / 'previous'
        if destination.exists():
            destination.rename(previous)
        try:
            target.rename(destination)
        except BaseException:
            if previous.exists():
                previous.rename(destination)
            raise
    return True


def install_archive(archive_path, destination, row, *, overwrite=False):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.unpack-', dir=destination.parent) as directory:
        root = Path(directory)
        with tarfile.open(archive_path) as archive:
            names = set()
            for member in archive:
                name = Path(member.name)
                if name.is_absolute() or '..' in name.parts or str(name) in names:
                    raise ValueError('Invalid archive member')
                names.add(str(name))
                if not member.isfile() and not member.isdir():
                    raise ValueError('Task archives contain only files and directories')
                target = root / name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as reader, target.open('wb') as writer:
                        shutil.copyfileobj(reader, writer)
        return import_task(root, destination, row, overwrite=overwrite)


def export_task(source, destination, row):
    verify_task(source, row)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((Path(source) / 'manifest.json').read_text())
    with destination.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as packed:
        with tarfile.open(fileobj=packed, mode='w') as archive:
            for name in ['manifest.json', *sorted(manifest['files'])]:
                path = Path(source) / name
                info = tarfile.TarInfo(name)
                info.size = path.stat().st_size
                info.mode = 0o644
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
    digest = sha256()
    with destination.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'task_id': row['task_id'], 'archive': destination.name,
            'sha256': digest.hexdigest(), 'bytes': destination.stat().st_size}


def verify_environment_assets(root, row, *, checksums=True, checked=None):
    """Check the selected task's OCI files; hash shared blobs once per invocation."""
    from mac_bench.environments.business.runtime import selected_images
    root = Path(root)
    config = root / 'data' / row['bundle'] / 'environment/environment.json'
    if not config.is_file():
        return 0
    image_root = root / 'data/environments/images'
    layout = json.loads((image_root / 'oci-layout').read_text())
    if layout.get('imageLayoutVersion') != '1.0.0':
        raise ValueError('Unsupported OCI image layout')
    tags = {s['image'] for s in json.loads(config.read_text())['services']}
    _, names = selected_images(image_root, tags)
    checked = checked if checked is not None else set()
    for name in names:
        path = image_root / name
        if not path.is_file():
            raise FileNotFoundError('Missing environment file: ' + name)
        if checksums and path not in checked:
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != path.name:
                raise ValueError('Environment checksum differs: ' + name)
            checked.add(path)
    return len(names)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('download','verify','import','install','export'))
    parser.add_argument('--task-id', action='append')
    parser.add_argument('--split', choices=('dev','test'))
    parser.add_argument('--url', help='Use an HTTP mirror downloads.json instead of Hugging Face')
    parser.add_argument('--repo-id', help='Hugging Face dataset repository')
    parser.add_argument('--revision', help='Dataset branch, tag or commit')
    parser.add_argument('--overwrite', action='store_true', help='Replace selected installed tasks with the release contents')
    parser.add_argument('--max-workers', type=int, default=4, help='Concurrent Hugging Face downloads (default: 4)')
    parser.add_argument('--source', type=Path, help='A dataset checkout containing data/tasks.json')
    parser.add_argument('--archive', type=Path, help='One task archive')
    parser.add_argument('--out', type=Path, help='Export directory')
    args = parser.parse_args(argv)
    if args.overwrite and args.command not in {'download', 'import', 'install'}:
        parser.error('--overwrite is used with download, import or install')
    base, rows = catalog_rows(repository_root(), args.task_id)
    if args.split:
        rows = [row for row in rows if row['split'] == args.split]
    if not rows:
        parser.error('No tasks match the selected IDs and split')
    if args.command == 'download':
        if args.max_workers < 1:
            parser.error('--max-workers must be positive')
        if args.url:
            if args.repo_id or args.revision:
                parser.error('--url cannot be combined with --repo-id or --revision')
            from mac_bench.download import download_tasks
            download_tasks(repository_root(), args.url, rows, overwrite=args.overwrite)
        else:
            from mac_bench.download import download_hub_tasks
            release = json.loads((base / 'distribution.json').read_text())
            download_hub_tasks(repository_root(), rows, repo_id=args.repo_id or release['repo_id'],
                               revision=args.revision or release['revision'], max_workers=args.max_workers, overwrite=args.overwrite)
        return 0
    if args.command == 'import' and args.source is None:
        parser.error('import requires --source')
    if args.command == 'install' and (args.archive is None or len(rows) != 1):
        parser.error('install requires --archive and exactly one --task-id')
    if args.command == 'export' and args.out is None:
        parser.error('export requires --out')
    source_rows = {}
    if args.command == 'import':
        source_base, available = catalog_rows(args.source, args.task_id)
        source_rows = {row['task_id']: row for row in available}
    exports = []
    checked_assets = set()
    for row in rows:
        destination = base / row['bundle']
        if args.command == 'verify':
            verify_task(destination, row)
            verify_environment_assets(repository_root(), row, checked=checked_assets)
        elif args.command == 'import':
            origin = source_rows[row['task_id']]
            if any(origin[key] != row[key] for key in ('world_revision','category','split')):
                raise ValueError('Source task does not match the dataset index')
            import_task(source_base / origin['bundle'], destination, row, overwrite=args.overwrite)
        elif args.command == 'install':
            install_archive(args.archive, destination, row, overwrite=args.overwrite)
        else:
            exports.append(export_task(destination, args.out / (row['task_id']+'.tar.gz'), row))
        print('OK', row['task_id'])
    if exports:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out/'archives.json').write_text(json.dumps({'tasks':exports},indent=2)+'\n')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
