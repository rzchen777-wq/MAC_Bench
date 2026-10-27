"""Standalone, standard-library worker package uploaded to the Modal VM."""
import fcntl
import json
import os
import sys
from pathlib import Path

from .configuration import (
    validate,
)
from .coordinator import (
    ContainerSnapshots,
)
from .engine import (
    SnapshotError,
)


def main():
    os.umask(0o077)
    request = json.loads(sys.argv[1])
    if not isinstance(request, dict) or set(request) != {'operation', 'spec', 'store', 'token'}:
        raise SnapshotError('container_snapshot_worker_request_invalid')
    operation = request['operation']
    if operation not in {'start', 'diagnose', 'preflight', 'quiesce', 'capture', 'verify', 'restore', 'resume'}:
        raise SnapshotError('container_snapshot_worker_operation_invalid')
    validate(request['spec'], request['store'])
    store = Path(request['store'])
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (store / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = ContainerSnapshots(request['spec'], store)
        method = getattr(worker, operation)
        try:
            result = method(request['token']) if operation in {'verify', 'restore'} else method()
        except Exception:
            try:
                worker.diagnose()
            except Exception:
                pass  # Preserve the original failure even if evidence collection fails.
            raise
    print(json.dumps({'result': result}, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': str(error) if isinstance(error, SnapshotError) else type(error).__name__}))
        raise SystemExit(1)
