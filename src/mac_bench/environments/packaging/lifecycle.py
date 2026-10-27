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
module=next(name for name in ('mac_bench.environments','mac_bench.construction.realize','evoteam.collaboration.realize')
            if os.path.isfile('/work/world/src/'+name.replace('.','/')+'/packaging/server.py'))+'.packaging.server'
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
    _exec(
        backend,
        sandbox,
        "pkill -f '^python3 -m (mac_bench.construction|evoteam.collaboration).realize.packaging.server' || true",
        30,
    )


def install_query_tools(backend, sandbox, *, namespace='mac_bench.environments', bundle=None):
    """Install the selected frozen presentation, never the current checkout."""
    from .interface import presentation
    source, manifest = presentation(bundle)
    if namespace not in {'mac_bench.environments', 'mac_bench.construction.realize', 'evoteam.collaboration.realize'}:
        raise ValueError('Unsupported sealed runtime namespace')
    hashes, files = {}, {}
    for relative in manifest['remote_files']:
        name = str(Path(relative).relative_to('tools'))
        content = (source / relative).read_bytes()
        if namespace == 'mac_bench.environments':
            content = content.replace(b'mac_bench.construction.realize', b'mac_bench.environments')
        if namespace == 'evoteam.collaboration.realize':
            # Only the additive interface deployed inside legacy sealed images
            # uses their pinned imports. The host exposes no evoteam package.
            content = content.replace(b'mac_bench.construction.realize', b'evoteam.collaboration.realize')
            content = content.replace(b'mac_bench.runtime.common', b'evoteam.collaboration.common')
        files['/work/world/src/' + namespace.replace('.', '/') + '/tools/' + name] = content
        hashes[name] = sha256(content).hexdigest()
    for relative in manifest.get('runtime_files', []):
        content = (source / relative).read_bytes()
        if namespace == 'mac_bench.environments':
            content = content.replace(b'mac_bench.construction.realize', b'mac_bench.environments')
        if namespace == 'evoteam.collaboration.realize':
            content = content.replace(b'mac_bench.construction.realize', b'evoteam.collaboration.realize')
        files['/work/world/src/' + namespace.replace('.', '/') + '/' + relative] = content
        hashes[relative] = sha256(content).hexdigest()
    if manifest.get('version') == 4 or (manifest.get('version') in (5, 6) and bundle and bundle.manifest['metadata'].get('record_forms')):
        names = bundle.manifest['public_files'] if bundle else []
        if 'public/record_forms.json' not in names or 'public/records.json' not in names:
            raise ValueError('Record-form presentation needs public schemas and a record directory')
        for public, remote in [('public/record_forms.json', 'record_forms.json'),
                               ('public/records.json', 'record_directory.json')]:
            files['/work/world/' + remote] = (bundle.root/public).read_bytes()
    if manifest.get('version') == 6:
        names = bundle.manifest['public_files']
        selected = bundle.manifest['metadata']['data_workspace']['resources']
        if not {'public/facts.json', 'public/registers.json'} <= set(selected):
            raise ValueError('Data workspace requires public facts and register contracts')
        if any(name not in names or not name.startswith('public/') or not name.endswith('.json') for name in selected):
            raise ValueError('Data workspace cannot install non-public resources')
        resources = {name: json.loads((bundle.root/name).read_text()) for name in selected}
        files['/work/world/public_data_resources.json'] = json.dumps(resources, ensure_ascii=False).encode()
    if callable(getattr(backend, 'write_files', None)):
        backend.write_files(sandbox, files=files)
    else:
        for path, content in files.items():
            backend.write_file(sandbox, path=path, content=content)
    return hashes


def install_private_verifier(backend, sandbox, bundle):
    """Install a sealed grader amendment while reusing unchanged app images."""
    import gzip
    from mac_bench.environments.verification.contracts import verify_package
    from mac_bench.environments.verification.runtime import install_sql_runtime, install_observer_profile

    amendment = bundle.manifest['metadata'].get('private_verifier_artifact')
    if amendment is None:
        return None
    name = amendment['path']
    if not name.startswith('private/') or name not in bundle.manifest['files']:
        raise ValueError('Grader amendment must be a declared private artifact')
    content = (bundle.root / name).read_bytes()
    checksum = sha256(content).hexdigest()
    editable = bundle.manifest['format'] == 'mac_bench.task.v1'
    if not editable and checksum != bundle.manifest['files'][name]:
        raise ValueError('Sealed grader amendment changed')
    contract = json.loads(content)
    if editable:
        from mac_bench.environments.verification.contracts import digest as contract_digest
        contract['revision'] = contract_digest({k:v for k,v in contract.items() if k != 'revision'})
        content = json.dumps(contract,ensure_ascii=False).encode()
        checksum = sha256(content).hexdigest()
    verify_package(contract)
    if not editable and contract['revision'] != amendment['revision']:
        raise ValueError('Grader amendment revision differs from metadata')
    install_sql_runtime(backend, sandbox, bundle)
    install_observer_profile(backend, sandbox, bundle)
    packed = gzip.compress(content, compresslevel=6, mtime=0)
    script = ('import gzip,hashlib,os,sys\n'
              'p="/work/world/verifier.amendment.tmp"\n'
              'data=gzip.decompress(open("/work/grader-amendment.gz","rb").read())\n'
              'assert hashlib.sha256(data).hexdigest()==sys.argv[1]\n'
              'open(p,"wb").write(data)\n'
              'os.replace(p,"/work/world/verifier.json")\n')
    command = shlex.join(['python3', '-I', '-c', script, checksum])
    for attempt in range(3):
        backend.write_file(sandbox, path='/work/grader-amendment.gz', content=packed)
        result = backend.exec(sandbox, command, timeout=120)
        if result.exit_code == 0:
            return {'path': name, 'revision': contract['revision'], 'sha256': checksum}
    raise RuntimeError('Private grader amendment failed transfer integrity check')


