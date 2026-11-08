"""Scoped upstream imports: upstream generic module names cannot leak to other methods."""
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
import os

_lock = threading.RLock()


@contextmanager
def source_modules(root, prefixes, *, workdir=None):
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    def owned(name):
        return any(name == p or name.startswith(p + '.') for p in prefixes)
    # In-process original runtimes patch globals. Serialize episodes; keep their
    # own async workers concurrent. A future suite can parallelize processes.
    with _lock:
        previous = {k: v for k, v in sys.modules.items() if owned(k)}
        original_path = list(sys.path)
        original_cwd = Path.cwd()
        for k in previous:
            del sys.modules[k]
        sys.path.insert(0, str(root))
        if workdir is not None:
            os.chdir(workdir)
        try:
            yield
        finally:
            for k in list(sys.modules):
                if owned(k):
                    del sys.modules[k]
            sys.modules.update(previous)
            sys.path[:] = original_path
            os.chdir(original_cwd)
