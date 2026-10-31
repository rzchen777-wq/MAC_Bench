"""The shared business tool interface used by construction and evaluation."""
from pathlib import Path
from hashlib import sha256
import json


def host_components(bundle):
    from mac_bench.environments.tools import api
    from mac_bench.evaluation.tasks import resources
    guide = bundle.root / 'private/agent_instructions.txt'
    if not guide.exists():
        guide = Path(__file__).with_name('guide.txt')
    return api, resources, guide.read_text(), 6


def presentation(bundle=None):
    root = Path(__file__).resolve().parents[1]
    names = [str(p.relative_to(root)) for p in sorted((root/'tools').rglob('*.py'))]
    names += ['packaging/server.py', 'packaging/rpc_client.py', 'catalog/factories/apps/drf.py']
    return root, {'version': 6, 'remote_files': names, 'runtime_files': [],
                  'files': {name: sha256((root/name).read_bytes()).hexdigest() for name in names}}
