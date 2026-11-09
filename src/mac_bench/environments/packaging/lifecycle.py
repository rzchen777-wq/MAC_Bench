"""Install task tools and verifiers and start the business runtime service."""

import json
import shlex

def _exec(backend, sandbox, command, timeout=210):
    result = backend.exec(sandbox, command, timeout=timeout)
    if result.exit_code:
        raise RuntimeError("Native world lifecycle operation failed")
    return result.stdout


def start_service(backend, sandbox):
    # A TCP listener can precede authentication/database readiness. Profile
    # initialization must only bind clients/read metadata, never populate data.
    # Retry a failed initializer, not an agent operation or business write.
    launcher = """import os,socket,subprocess,time
module='mac_bench.environments.packaging.server'
end=time.monotonic()+300
process=None
with open('/work/world-server.log','ab',buffering=0) as log:
    while time.monotonic()<end:
        if process is None or process.poll() is not None:
            if process is not None:
                log.write(('Initializer exited '+str(process.returncode)+'; retrying\\n').encode())
                time.sleep(3)
            process=subprocess.Popen(
                ['python3','-m',module,
                 '--root','/work/world'],cwd='/work/world',
                env={**os.environ,'PYTHONPATH':'src'},stdin=subprocess.DEVNULL,
                stdout=log,stderr=log,start_new_session=True)
        try:
            connection=socket.create_connection(('127.0.0.1',6099),2)
            connection.close()
            break
        except OSError:
            time.sleep(2)
    else:
        if process is not None and process.poll() is None:
            process.terminate()
        raise RuntimeError('World service did not become ready; inspect private log')
"""
    _exec(backend, sandbox, shlex.join(["python3", "-c", launcher]), 320)


def public_resource_files(bundle):
    """Collect public resource bindings for a verified provisioning batch."""
    files = {}
    if bundle.manifest['metadata'].get('record_forms'):
        for public, remote in [('public/record_forms.json', 'record_forms.json'),
                               ('public/records.json', 'record_directory.json')]:
            files['/work/world/' + remote] = (bundle.root/public).read_bytes()
    workspace = bundle.manifest['metadata'].get('data_workspace')
    if workspace:
        selected = workspace['resources']
        if any(not name.startswith('public/') or not name.endswith('.json') or name not in bundle.manifest['public_files'] for name in selected):
            raise ValueError('Data workspace resources must be public')
        resources = {name: json.loads((bundle.root/name).read_text()) for name in selected}
        files['/work/world/public_data_resources.json'] = json.dumps(resources, ensure_ascii=False).encode()
    return files


def private_verifier_files(bundle):
    """Collect the task's grader and observer for one verified upload."""
    from mac_bench.environments.packaging.bundle import _file
    from mac_bench.environments.verification.contracts import verify_package

    artifact = bundle.manifest['metadata'].get('private_verifier_artifact')
    if artifact is None:
        return {}
    name = artifact['path']
    profile_name = 'private/profile.py'
    if not name.startswith('private/') or name not in bundle.manifest['files']:
        raise ValueError('Task grader must be a declared private artifact')
    if profile_name not in bundle.manifest['files']:
        raise ValueError('Task observer must be a declared private artifact')
    content = _file(bundle.root, name).read_bytes()
    verify_package(json.loads(content))
    profile = _file(bundle.root, profile_name).read_bytes()
    compile(profile, profile_name, 'exec')
    return {'/work/world/verifier.json': content, '/work/world/profile.py': profile}
