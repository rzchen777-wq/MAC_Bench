"""Check local installation and task assets."""
import argparse
import importlib.util
import json
import sys
from mac_bench.runtime.paths import repository_root

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id')
    args = parser.parse_args(argv)
    root = repository_root()
    checks = [('Python >= 3.11', sys.version_info >= (3,11)),
              ('Task index', (root/'data/public_tasks.json').is_file())]
    if args.task_id:
        try:
            from mac_bench.data_cli import catalog_rows, verify_task, verify_environment_assets
            _, rows = catalog_rows(root, [args.task_id])
            for row in rows:
                verify_task(root / 'data' / row['bundle'], row)
                verify_environment_assets(root, row, checksums=False)
            checks.append(('Selected task assets', True))
        except (OSError, ValueError) as exc:
            checks.append(('Selected task assets: '+str(exc), False))
        checks.append(('Modal SDK',importlib.util.find_spec('modal') is not None))
    for label,ok in checks:
        print(('OK      ' if ok else 'MISSING ')+label)
    return 0 if all(ok for _,ok in checks) else 1
