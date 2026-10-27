"""Strict local catalogue intake: classification does not mutate sealed worlds."""
import json
from pathlib import Path
from .protocol import TaskRecord, ENGINEERING


def task_record(bundle_path, *, category=None, split='test', task_id=None, revision=None):
    from mac_bench.environments.packaging.bundle import WorldBundle
    bundle = WorldBundle.load(bundle_path)
    meta = bundle.manifest.get('metadata', {})
    family = meta.get('world_family')
    declared = family if family in ENGINEERING else meta.get('scenario_family')
    if category is not None and declared is not None and category != declared:
        raise ValueError('Catalogue category conflicts with sealed world')
    if revision is not None and revision != bundle.manifest['revision']:
        raise ValueError('Catalogue names a different sealed revision')
    return TaskRecord(task_id or bundle.manifest['world_id'], bundle.manifest['revision'],
                      category or declared, split, bundle.root)


def load_catalog(path, *, task_ids=None):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text())
    if manifest.get('format') != 'collaboration.tasks.v1':
        raise ValueError('Unsupported task catalogue')
    records, ids, revisions = [], set(), set()
    items = manifest['tasks']
    declared = [row['task_id'] for row in items]
    if len(set(declared)) != len(declared):
        raise ValueError('Duplicate task ID in catalogue')
    if task_ids is not None:
        wanted = set(task_ids)
        if wanted - set(declared):
            raise ValueError('Unknown task IDs: ' + ', '.join(sorted(wanted - set(declared))))
        items = [row for row in items if row['task_id'] in wanted]
    for item in items:
        relative = Path(item['bundle'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Task bundle must be inside the dataset directory')
        record = task_record(path.parent / item['bundle'], category=item['category'],
                             split=item['split'], task_id=item['task_id'], revision=item['world_revision'])
        if record.task_id in ids or record.world_revision in revisions:
            raise ValueError('Duplicate task ID or world revision')
        ids.add(record.task_id); revisions.add(record.world_revision); records.append(record)
    if not records:
        raise ValueError('Task catalogue is empty')
    return tuple(records)


def open_task(record, directory, **kwargs):
    # Revalidate even when callers constructed a TaskRecord directly.
    task_record(record.bundle, category=record.category, split=record.split,
                task_id=record.task_id, revision=record.world_revision)
    if record.category in ENGINEERING:
        from .adapters.engineering import EngineeringTask
        return EngineeringTask(record, directory, **kwargs)
    from .adapters.operational import OperationalTask
    return OperationalTask(record, directory, **kwargs)
