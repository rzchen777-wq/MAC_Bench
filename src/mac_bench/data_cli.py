"""Verify, import and package the current task data."""
import argparse
import json
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


def import_task(source, destination, row):
    source, destination = Path(source), Path(destination)
    verify_task(source, row)
    if destination.exists():
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
        target.rename(destination)
    return True


def install_archive(archive_path, destination, row):
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
        return import_task(root, destination, row)


def export_task(source, destination, row):
    verify_task(source, row)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((Path(source) / 'manifest.json').read_text())
    with tarfile.open(destination, 'w:gz') as archive:
        for name in ['manifest.json', *sorted(manifest['files'])]:
            archive.add(Path(source) / name, arcname=name, recursive=False)
    digest = sha256()
    with destination.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'task_id': row['task_id'], 'archive': destination.name,
            'sha256': digest.hexdigest(), 'bytes': destination.stat().st_size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('download','verify','import','install','export'))
    parser.add_argument('--task-id', action='append')
    parser.add_argument('--split', choices=('dev','test'))
    parser.add_argument('--url', help='URL of the release downloads.json')
    parser.add_argument('--source', type=Path, help='A dataset checkout containing data/tasks.json')
    parser.add_argument('--archive', type=Path, help='One task archive')
    parser.add_argument('--out', type=Path, help='Export directory')
    args = parser.parse_args(argv)
    base, rows = catalog_rows(repository_root(), args.task_id)
    if args.split:
        rows = [row for row in rows if row['split'] == args.split]
    if args.command == 'download':
        if not args.url:parser.error('download requires --url pointing to downloads.json')
        from mac_bench.download import download_tasks
        download_tasks(repository_root(),args.url,rows)
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
    for row in rows:
        destination = base / row['bundle']
        if args.command == 'verify':
            verify_task(destination, row)
        elif args.command == 'import':
            origin = source_rows[row['task_id']]
            if any(origin[key] != row[key] for key in ('world_revision','category','split')):
                raise ValueError('Source task does not match the dataset index')
            import_task(source_base / origin['bundle'], destination, row)
        elif args.command == 'install':
            install_archive(args.archive, destination, row)
        else:
            exports.append(export_task(destination, args.out / (row['task_id']+'.tar.gz'), row))
        print('OK', row['task_id'])
    if exports:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out/'archives.json').write_text(json.dumps({'tasks':exports},indent=2)+'\n')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
