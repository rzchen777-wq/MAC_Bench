"""Checks against installed task packages, separate from synthetic unit fixtures."""
import json
import os
from pathlib import Path
import pytest
from mac_bench.runtime.paths import repository_root

ROOT = Path(os.environ.get('MAC_BENCH_TASK_ROOT', repository_root() / 'data/tasks'))
CATALOG = json.loads((repository_root() / 'data/tasks.json').read_text())['tasks']


@pytest.fixture(scope='session', autouse=True)
def installed_task_data():
    if not ROOT.is_dir():
        pytest.skip('Install the task data to run packaged-task checks (docs/getting-started.md)')
    missing = [row['task_id'] for row in CATALOG if not (ROOT / row['task_id'] / 'manifest.json').is_file()]
    if missing:
        pytest.fail('Packaged-task checks require all 54 tasks; missing: ' + ', '.join(missing))
