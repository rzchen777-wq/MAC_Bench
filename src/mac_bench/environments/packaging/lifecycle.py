"""Install task tools and verifiers and start the business runtime service."""

import json
import shlex
from hashlib import sha256
from pathlib import Path

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


def stop_service(backend, sandbox):
    _exec(backend, sandbox, "pkill -f '^python3 -m mac_bench.environments.packaging.server' || true", 30)


def install_public_resources(backend, sandbox, *, bundle):
    """Install this task's public resource bindings; code is in the runtime archive."""
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
    if callable(getattr(backend, 'write_files', None)):
        backend.write_files(sandbox, files=files)
    else:
        for path, content in files.items():
            backend.write_file(sandbox, path=path, content=content)
    return {name: sha256(content).hexdigest() for name, content in files.items()}


def install_private_verifier(backend, sandbox, bundle):
    """Install a task grader while reusing application images."""
    import gzip
    from mac_bench.environments.verification.contracts import verify_package
    from mac_bench.environments.verification.runtime import install_observer_profile

    amendment = bundle.manifest['metadata'].get('private_verifier_artifact')
    if amendment is None:
        return None
    name = amendment['path']
    if not name.startswith('private/') or name not in bundle.manifest['files']:
        raise ValueError('Task grader must be a declared private artifact')
    content = (bundle.root / name).read_bytes()
    checksum = sha256(content).hexdigest()
    contract = json.loads(content)
    verify_package(contract)
    install_observer_profile(backend, sandbox, bundle)
    packed = gzip.compress(content, compresslevel=6, mtime=0)
    script = ('import gzip,hashlib,os,sys\n'
              'p="/work/world/verifier.tmp"\n'
              'data=gzip.decompress(open("/work/grader.gz","rb").read())\n'
              'assert hashlib.sha256(data).hexdigest()==sys.argv[1]\n'
              'open(p,"wb").write(data)\n'
              'os.replace(p,"/work/world/verifier.json")\n')
    command = shlex.join(['python3', '-I', '-c', script, checksum])
    for attempt in range(3):
        backend.write_file(sandbox, path='/work/grader.gz', content=packed)
        result = backend.exec(sandbox, command, timeout=120)
        if result.exit_code == 0:
            return {'path': name, 'revision': contract['revision'], 'sha256': checksum}
    raise RuntimeError('Task grader failed transfer integrity check')


