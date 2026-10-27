"""Versioned public-tool presentation, separate from native data and grading."""
from functools import lru_cache
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

FORMAT = 'mac_bench.tool_presentation.v1'


def _inside(root, name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Invalid tool presentation path')
    target = root / path
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Tool presentation path escapes its artifact')
    return target


def presentation(bundle=None):
    selected = bundle.manifest.get('metadata', {}).get('tool_presentation') if bundle else None
    if selected is None:
        raise ValueError('This task has no packaged tool interface')
    resource = selected.get('resource')
    root = _inside(Path(__file__).parents[1] / 'assets', resource) if resource else _inside(bundle.root, selected['path'])
    raw = (root / 'manifest.json').read_bytes()
    manifest = json.loads(raw)
    if manifest.get('format') != FORMAT or manifest.get('version') not in (2, 3, 4, 5, 6):
        raise ValueError('Unsupported tool presentation')
    if not resource and selected and (selected['version'] != manifest['version'] or
                     selected['sha256'] != sha256(raw).hexdigest()):
        raise ValueError('Tool presentation manifest differs from sealed selection')
    for name, checksum in manifest['files'].items():
        if not resource and sha256(_inside(root, name).read_bytes()).hexdigest() != checksum:
            raise ValueError('Tool presentation artifact changed: ' + name)
    for name in [*manifest['remote_files'], manifest['host_api'], manifest['host_resources'], manifest['guide']]:
        if name not in manifest['files']:
            raise ValueError('Undeclared tool presentation component')
    for name in manifest.get('runtime_files', []):
        if name not in manifest['files'] or name != 'packaging/server.py':
            raise ValueError('Unsupported record-form runtime component')
    return root, manifest


@lru_cache(maxsize=16)
def _module(path, checksum, name, tool_package):
    path = Path(path)
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Keep resource/query contracts pinned transitively, not only their entry files.
    source = path.read_bytes().replace(b'mac_bench.construction.realize.tools', tool_package.encode())
    sys.modules[name] = module
    try:
        exec(compile(source, str(path), 'exec'), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def host_components(bundle):
    root, manifest = presentation(bundle)
    fingerprint = sha256(b''.join((root/name).read_bytes() for name in sorted(manifest['files']))).hexdigest()
    package = 'mac_bench.evaluation.tasks._tools_' + fingerprint
    if package not in sys.modules:
        module = ModuleType(package)
        module.__path__ = [str(root/'tools')]
        sys.modules[package] = module
    # These are all transitive dependencies of host API/resource handling.
    # Preloading verified source avoids both reading and writing .pyc files in
    # immutable world artifacts; imports still resolve in the isolated package.
    names = ['contracts', 'navigation', 'query']
    if 'tools/data_workspace.py' in manifest['files']:
        names.append('data_workspace')
    for name in names:
        relative = 'tools/' + name + '.py'
        _module(str(root/relative), manifest['files'][relative], package+'.'+name, package)
    def load(key, package_name):
        name = manifest[key]
        checksum = manifest['files'][name]
        # Identical entry files can import different frozen dependencies. Cache
        # by the entire presentation, not just this entry file's bytes.
        return _module(str(root / name), checksum,
                       package_name + '._presentation_' + fingerprint + '_' + checksum, package)
    return (load('host_api', 'mac_bench.environments.tools'),
            load('host_resources', 'mac_bench.evaluation.tasks'),
            (root / manifest['guide']).read_text(), manifest['version'])

