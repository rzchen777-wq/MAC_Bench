"""Explicit, sealed SQL-module overrides for newly revised grader contracts."""
from hashlib import sha256
from pathlib import Path


def selected_observer_profile(bundle):
    selected = bundle.manifest['metadata'].get('private_verifier_observer_profile')
    if selected is None:
        return None
    name = selected['path']
    relative = Path(name)
    path = bundle.root / relative
    if (relative.is_absolute() or '..' in relative.parts or not name.startswith('private/')
            or name not in bundle.manifest['files'] or path.is_symlink()
            or not path.resolve().is_relative_to(bundle.root.resolve())):
        raise ValueError('Observer profile must be a declared private bundle asset')
    content = path.read_bytes()
    digest = sha256(content).hexdigest()
    if bundle.manifest['format'] != 'mac_bench.task.v1' and (digest != selected['sha256'] or digest != bundle.manifest['files'][name]):
        raise ValueError('Sealed observer profile changed')
    compile(content, name, 'exec')
    return content


def install_observer_profile(backend, sandbox, bundle):
    content = selected_observer_profile(bundle)
    if content is None:
        return None
    if (bundle.root/'environment/environment.json').exists():
        content = content.replace(b'evoteam.collaboration.realize',b'mac_bench.environments').replace(b'mac_bench.construction.realize',b'mac_bench.environments')
    backend.write_file(sandbox, path='/work/world/profile.py', content=content)
    return {'module': 'profile.py', 'sha256': sha256(content).hexdigest()}


def selected_sql_runtime(bundle):
    selected = bundle.manifest['metadata'].get('private_verifier_sql_runtime')
    if selected is None:
        return None
    name = selected['path']
    relative = Path(name)
    path = bundle.root / relative
    if (relative.is_absolute() or '..' in relative.parts or not name.startswith('private/')
            or name not in bundle.manifest['files'] or path.is_symlink()
            or not path.resolve().is_relative_to(bundle.root.resolve())):
        raise ValueError('SQL runtime must be a declared private bundle asset')
    content = path.read_bytes()
    digest = sha256(content).hexdigest()
    if bundle.manifest['format'] != 'mac_bench.task.v1' and (digest != selected['sha256'] or digest != bundle.manifest['files'][name]):
        raise ValueError('Sealed verifier SQL runtime changed')
    namespace = ('mac_bench.environments' if (bundle.root/'environment/environment.json').exists() else bundle.manifest['metadata'].get('runtime_namespace', 'evoteam.collaboration.realize'))
    if namespace == 'mac_bench.environments':
        content = content.replace(b'evoteam.collaboration.realize',b'mac_bench.environments').replace(b'mac_bench.construction.realize',b'mac_bench.environments')
    if namespace not in ('evoteam.collaboration.realize', 'mac_bench.construction.realize', 'mac_bench.environments'):
        raise ValueError('Unsupported sealed grading runtime namespace')
    compile(content, name, 'exec')
    remote = namespace.replace('.', '/') + '/verification/sql.py'
    return remote, content


def install_sql_runtime(backend, sandbox, bundle):
    selected = selected_sql_runtime(bundle)
    if selected is None:
        return None
    remote, content = selected
    backend.write_file(sandbox, path='/work/world/src/' + remote, content=content)
    return {'module': remote, 'sha256': sha256(content).hexdigest()}
