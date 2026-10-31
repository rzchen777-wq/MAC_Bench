"""Prepare the task packages, viewer tables and shared images for a Hub release."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from mac_bench.data_cli import catalog_rows, export_task
from mac_bench.environments.business.runtime import selected_images
from mac_bench.runtime.paths import repository_root


def checksum(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare(output):
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = repository_root()
    output = Path(output).resolve()
    if output.exists() and any(p.name != '.cache' for p in output.iterdir()):
        raise ValueError('Choose an empty output directory')
    output.mkdir(parents=True, exist_ok=True)
    base, rows = catalog_rows(root)
    public = {r['task_id']: r for r in json.loads((base / 'public_tasks.json').read_text())['tasks']}
    image_root = base / 'environments/images'
    oci = json.loads((image_root / 'index.json').read_text())
    # The loader uses the shared manifest index to identify reusable layers.
    metadata, layers = {'index.json', 'oci-layout'}, set()
    def walk(descriptor):
        name = 'blobs/sha256/' + descriptor['digest'].removeprefix('sha256:')
        if 'layer' in descriptor['mediaType']:
            layers.add(name)
        else:
            metadata.add(name)
        if 'manifest' in descriptor['mediaType'] or 'index' in descriptor['mediaType']:
            body = json.loads((image_root / name).read_text())
            for child in [*body.get('manifests', []), *body.get('layers', []),
                          *([body['config']] if 'config' in body else [])]:
                walk(child)
    for descriptor in oci['manifests']:
        walk(descriptor)
    manifest = {'format': 'mac_bench.downloads.v1', 'tasks': [], 'assets': {}}
    views = {'dev': [], 'test': []}
    needed = set()
    for row in rows:
        path = base / row['bundle']
        instruction = (path / 'public/task.txt').read_text()
        if instruction != public[row['task_id']]['instruction']:
            raise ValueError('Public task description differs: ' + row['task_id'])
        if public[row['task_id']]['world_revision'] != row['world_revision']:
            raise ValueError('Public task revision differs: ' + row['task_id'])
        entry = export_task(path, output / 'packages' / (row['task_id'] + '.tar.gz'), row)
        entry.update({k: row[k] for k in ('world_revision', 'category', 'split')})
        entry['archive'] = 'packages/' + entry['archive']
        environment = path / 'environment/environment.json'
        business = environment.is_file()
        if business:
            tags = {s['image'] for s in json.loads(environment.read_text())['services']}
            _, files = selected_images(image_root, tags)
            entry['environment_assets'] = ['environments/images/' + n for n in sorted(metadata | set(files))]
            needed.update(entry['environment_assets'])
        manifest['tasks'].append(entry)
        views[row['split']].append({**{k: row[k] for k in ('task_id', 'category', 'split', 'world_revision')},
            'domain': 'business_operations' if business else 'software_engineering',
            'instruction': instruction, 'package': entry['archive']})
        print('Packaged', row['task_id'], flush=True)
    for name in sorted(needed):
        source, target = base / name, output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)  # Immutable OCI blobs: no second 5 GiB copy.
        manifest['assets'][name] = {'url': name, 'bytes': source.stat().st_size, 'sha256': checksum(source)}
    (output / 'data').mkdir(exist_ok=True)
    for split, records in views.items():
        pq.write_table(pa.Table.from_pylist(records), output / 'data' / (split + '.parquet'), compression='zstd')
    for name in ('tasks.json', 'public_tasks.json'):
        shutil.copyfile(base / name, output / name)
    for name in ('LICENSE', 'NOTICE'):
        shutil.copyfile(root / name, output / name)
    card = (base / 'README.md').read_text().replace('../docs/assets/overview.png', 'assets/overview.png')
    (output / 'README.md').write_text(card)
    (output / 'assets').mkdir(exist_ok=True)
    shutil.copyfile(root / 'docs/assets/overview.png', output / 'assets/overview.png')
    (output / 'downloads.json').write_text(json.dumps(manifest, indent=2) + '\n')
    attributes = ['*.tar.gz filter=lfs diff=lfs merge=lfs -text', '*.parquet filter=lfs diff=lfs merge=lfs -text',
                  '*.png filter=lfs diff=lfs merge=lfs -text']
    attributes += [f'environments/images/{name} filter=lfs diff=lfs merge=lfs -text' for name in sorted(layers)]
    (output / '.gitattributes').write_text('\n'.join(attributes) + '\n')
    print(f'Ready: {len(rows)} tasks; {len(needed)} environment files; '
          f'{sum(v["bytes"] for v in manifest["assets"].values()) / 1024**3:.2f} GiB of OCI assets.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    prepare(parser.parse_args().out)
