"""Modal code worlds and independent grading; no native-app/Docker assumptions."""

import base64
import gzip
from hashlib import sha256
import json
import shlex
import time
from pathlib import Path

from .artifacts import fingerprint, write_json
from .verifier import score_product
from .infrastructure import load_override
from ..public_workspace import initialization_commands


def modal_backend(directory, timeout=7200):
    from mac_bench.runtime.sandbox.modal import ModalSandboxBackend
    # A frozen infrastructure image retains its provider application scope.
    # New constructions use the current default application instead.
    directory = Path(directory)
    applications = set()
    if directory.is_dir():
        for path in directory.glob('*.json'):
            receipt = json.loads(path.read_text())
            if receipt.get('image_id') and receipt.get('app_name'):
                applications.add(receipt['app_name'])
    if len(applications) > 1:
        raise ValueError('Snapshot receipts span multiple provider applications')
    options = {'app_name': next(iter(applications))} if applications else {}
    return ModalSandboxBackend(timeout=timeout, cpu=4, memory_mib=8192,
                               snapshot_directory=directory, **options)


class ProductRuntime:
    def __init__(self, environment, directory, *, backend=None):
        self.environment = environment
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.backend = backend or modal_backend(self.directory / "provider")
        self.sandbox = None

    def command(self, name, command, *, timeout=1200):
        if self.sandbox is None:
            raise RuntimeError("Product runtime is not open")
        result = self.backend.exec(self.sandbox, command, timeout=timeout)
        path = self.directory / "commands" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "output.log").write_text(result.stdout)
        write_json(path / "result.json", {"command": command, "exit_code": result.exit_code,
                   "duration_seconds": result.duration_seconds})
        return result

    def open(self, *, snapshot=None, network_blocked=False):
        if self.sandbox is not None:
            raise RuntimeError("Runtime is already open")
        arguments = {"name": "product-world", "network_blocked": network_blocked}
        arguments.update({"snapshot": snapshot} if snapshot else {"image": self.environment["image"]})
        self.sandbox = self.backend.create(**arguments)
        write_json(self.directory / "sandbox.json", {"id": self.sandbox.id,
                   "status": "running", "created_at": time.time(),
                   "network_blocked": network_blocked})
        return self

    def close(self):
        if self.sandbox is not None:
            sid = self.sandbox.id
            self.backend.delete(self.sandbox)
            self.sandbox = None
            write_json(self.directory / "sandbox.json", {"id": sid,
                       "status": "terminated", "finished_at": time.time()})

    def _upload_source_part(self, path, content):
        expected = sha256(content).hexdigest()
        for attempt in range(3):
            self.backend.write_file(self.sandbox, path=path, content=content)
            result = self.command('source-part-integrity-' + Path(path).name + '-' + str(attempt),
                                  'sha256sum ' + shlex.quote(path))
            if (result.exit_code == 0 and result.stdout.split()
                    and result.stdout.split()[0] == expected):
                return
        raise RuntimeError('Source restore/integrity: uploaded part checksum mismatch after three writes')

    def install_source(self, archive):
        content = Path(archive).read_bytes()
        expected = sha256(content).hexdigest()
        transport = {}
        if len(content) > 8 * 1024 * 1024:
            packed = gzip.compress(content, mtime=0)
            parts = []
            for offset in range(0, len(packed), 8 * 1024 * 1024):
                path = f'/work/source-transfer.part-{len(parts):06d}'
                self._upload_source_part(path, packed[offset:offset + 8 * 1024 * 1024])
                parts.append(path)
            script = (
                'import gzip,hashlib,os,shutil\n'
                'with open("/work/source-transfer.gz","wb") as output:\n'
                f' for path in {parts!r}:\n'
                '  with open(path,"rb") as part: shutil.copyfileobj(part,output)\n'
                'digest=hashlib.sha256()\n'
                'with gzip.open("/work/source-transfer.gz","rb") as source, '
                'open("/work/source-transfer.tmp","wb") as output:\n'
                ' for block in iter(lambda:source.read(1048576),b""):\n'
                '  digest.update(block); output.write(block)\n'
                f'assert digest.hexdigest()=={expected!r}, "Source archive checksum mismatch"\n'
                'os.replace("/work/source-transfer.tmp","/work/source.tar")\n')
            restored = self.command('compressed-source-restore',
                                    'python3 -I -c ' + shlex.quote(script), timeout=120)
            if restored.exit_code:
                raise RuntimeError('Compressed source restore/integrity check failed')
            transport = {'transport': 'gzip_atomic', 'transport_bytes': len(packed),
                         'transport_chunks': len(parts), 'transport_sha256': sha256(packed).hexdigest()}
        else:
            self._upload_source_part('/work/source.tar', content)
        proof = self.command('source-upload-integrity', 'sha256sum /work/source.tar')
        if proof.exit_code or not proof.stdout.split() or proof.stdout.split()[0] != expected:
            raise RuntimeError('Source upload failed integrity check')
        write_json(self.directory / 'source_upload.json',
                   {'archive_sha256': expected, 'bytes': len(content), 'verified': True, **transport})
        result = self.command("extract", "set -e; mkdir -p /work/project; tar xf /work/source.tar -C /work/project")
        if result.exit_code:
            raise RuntimeError("Cannot extract source archive")

    def read_archive(self, path):
        """Read frozen binary artifacts through bounded, integrity-checked execs.

        Some provider filesystem streams stall after the export has finished.
        Keep each response small and distinguish a failed transfer from a
        candidate result; never accept truncated or changed archive contents.
        """
        if self.sandbox is None or not path.startswith('/'):
            raise ValueError('An open runtime and absolute archive path are required')

        def execute(script, *arguments):
            command = 'python3 -I -c ' + shlex.quote(script)
            command += ''.join(' ' + shlex.quote(str(a)) for a in arguments)
            result = self.backend.exec(self.sandbox, command, timeout=60)
            if result.exit_code:
                raise RuntimeError('Archive transfer command failed')
            return result.stdout.strip()

        metadata = json.loads(execute(
            'import hashlib,json,os,sys\np=sys.argv[1]; h=hashlib.sha256()\n'
            'with open(p,"rb") as f:\n'
            ' for b in iter(lambda:f.read(1048576),b""): h.update(b)\n'
            'print(json.dumps(dict(size=os.path.getsize(p),sha256=h.hexdigest())))', path))
        content = bytearray()
        size = metadata['size']
        chunk_size = 512 * 1024
        while len(content) < size:
            count = min(chunk_size, size - len(content))
            # Retry only reads of this immutable archive. Never append a partial
            # response; shrink the page if the provider keeps clipping it.
            for attempt in range(3):
                encoded = execute(
                    'import base64,sys; f=open(sys.argv[1],"rb"); '
                    'f.seek(int(sys.argv[2])); '
                    'print(base64.b64encode(f.read(int(sys.argv[3]))).decode("ascii"))',
                    path, len(content), count)
                try:
                    chunk = base64.b64decode(encoded, validate=True)
                except ValueError:
                    chunk = b''
                if len(chunk) == count:
                    break
            else:
                if count > 64 * 1024:
                    chunk_size = max(64 * 1024, count // 2)
                    continue
                raise RuntimeError('Archive transfer returned a truncated chunk')
            content.extend(chunk)
        if sha256(content).hexdigest() != metadata['sha256']:
            raise RuntimeError('Archive transfer checksum mismatch')
        return bytes(content)

    def build(self):
        return self.command("build", "set -e; cd /work/project; " + self.environment["build"],
                            timeout=self.environment.get("build_timeout", 1800))

    def health(self):
        return self.command("health", "set -e; cd /work/project; " + self.environment["health"],
                            timeout=self.environment.get("health_timeout", 1800))

    def verify(self, verifier_directory, contract, *, mode="normal"):
        from modal.exception import SandboxFilesystemNotFoundError
        self.backend.upload_tree(self.sandbox, local_dir=verifier_directory, remote_dir="/work/grader")
        result = self.command("verify-" + mode,
                              "set -e; cd /work/project; " + self.environment["verify"],
                              timeout=self.environment.get("verify_timeout", 900))
        try:
            execution = json.loads(self.backend.read_file(self.sandbox, path="/work/grader/result.json"))
        except (OSError, ValueError, SandboxFilesystemNotFoundError) as error:
            if result.exit_code == 1 and 'RuntimeError: Application startup failed; inspect server.log' in result.stdout:
                server = self.command('candidate-startup-log', 'cat /work/grader/server.log')
                if server.exit_code == 0 and 'Traceback' in server.stdout:
                    score = {'status': 'failed', 'gc': 0, 'task_success': False,
                             'regression_passed': False, 'phase': 'candidate_application_startup',
                             'error': 'Candidate application crashed before verifier checks; traceback retained',
                             'candidate_startup_log': 'commands/candidate-startup-log/output.log'}
                    write_json(self.directory / ('score-' + mode + '.json'), score)
                    return score
            return {"status": "verifier_error", "gc": None, "task_success": None,
                    "error": f"Missing/invalid verifier result: {error}; exit={result.exit_code}"}
        score = score_product(contract, execution)
        if result.exit_code not in {0, 1}:
            score = {"status": "verifier_error", "gc": None, "task_success": None,
                     "error": f"Verifier process exit={result.exit_code}", "execution": execution}
        write_json(self.directory / ("score-" + mode + ".json"), score)
        return score

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.close()


def read_environment(directory):
    """Read the dependency recipe and its package-manager lock files."""
    directory=Path(directory)
    environment=json.loads((directory/'private/environment.json').read_text())
    if environment.get('dependency_files'):
        environment['dependency_contents']={name:(directory/path).read_text()
            for name,path in environment['dependency_files'].items()}
    return environment


def dependency_cache(environment):
    import os
    from mac_bench.runtime.paths import repository_root
    recipe={'image': environment['image'], 'setup': environment['setup']}
    if environment.get('dependency_contents'):
        recipe.update(dependency_setup=environment['dependency_setup'],dependency_contents=environment['dependency_contents'])
    key = fingerprint(recipe)
    root = Path(os.environ.get('MAC_BENCH_ENVIRONMENT_CACHE', repository_root()/'.cache/environments'))
    return root / key, 'mac-bench-base-' + key


def prepare_environment(environment, run):
    """Cache dependency setup once; tasks load their own source and initialization."""
    import fcntl
    import shutil
    cache, name = dependency_cache(environment)
    cache.mkdir(parents=True, exist_ok=True)
    with (cache/'prepare.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        runtime = ProductRuntime(environment, cache / 'environment')
        if not runtime.backend.snapshot_exists(name=name):
            try:
                if environment.get('dependency_contents'):
                    parent_environment={k:v for k,v in environment.items()
                                        if k not in {'dependency_files','dependency_contents','dependency_setup'}}
                    parent=prepare_environment(parent_environment,cache/'base')
                    provider=cache/'environment/provider'
                    provider.mkdir(parents=True,exist_ok=True)
                    for receipt in (cache/'base/environment/provider').glob('*.json'):
                        shutil.copyfile(receipt,provider/receipt.name)
                    runtime.open(snapshot=parent)
                    runtime.backend.write_files(runtime.sandbox,files={
                        '/opt/mac-bench-dependencies/'+name:content.encode()
                        for name,content in environment['dependency_contents'].items()})
                    result=runtime.command('dependencies',environment['dependency_setup'],
                                           timeout=environment.get('setup_timeout',1800))
                else:
                    runtime.open()
                    result = runtime.command('setup', environment['setup'],
                                             timeout=environment.get('setup_timeout', 1800))
                if result.exit_code:
                    raise RuntimeError('Dependency setup failed; inspect ' + str(cache/'environment/commands'))
                runtime.backend.create_snapshot(runtime.sandbox, name=name, timeout=600)
            finally:
                runtime.close()
        provider = Path(run) / 'environment/provider'
        provider.mkdir(parents=True, exist_ok=True)
        for receipt in (cache/'environment/provider').glob('*.json'):
            target = provider/receipt.name
            if target.resolve() != receipt.resolve():
                shutil.copyfile(receipt, target)
    write_json(Path(run)/'environment/environment.json', {'snapshot':name, 'cache':str(cache)})
    return name


class LoadedProductWorld:
    """Persistent agent workspace, fresh starter reset, separately constructed grader."""
    accepted_families = {'product_application'}
    runtime_type = ProductRuntime

    def __init__(self, bundle, directory, *, backend=None):
        from mac_bench.environments.packaging.bundle import WorldBundle
        self.bundle = WorldBundle.load(bundle)
        meta = self.bundle.manifest["metadata"]
        if meta.get("world_family") not in self.accepted_families:
            raise ValueError("Unsupported engineering world family")
        self.meta = meta
        self.directory = Path(directory)
        self.environment = read_environment(self.bundle.root)
        self._sealed_environment = dict(self.environment)
        self.contract = json.loads((self.bundle.root / "private/contract.json").read_text())
        self.runtime = self.runtime_type(self.environment, self.directory / "agent", backend=backend)

    def open(self, *, rebuild_environment=False):
        if self.runtime.sandbox is not None:
            raise RuntimeError("Product world is already open")
        import os
        rebuild_environment = rebuild_environment or os.environ.get('MAC_BENCH_REBUILD_ENVIRONMENT') == '1'
        cached = self.meta.get("initial_snapshot")
        prepared, prepared_name = dependency_cache(self.environment)
        prepared_provider = prepared/'environment/provider'
        prepared_receipt = prepared_provider/(sha256(prepared_name.encode()).hexdigest()+'.json')
        override = load_override(self.bundle, self._sealed_environment)
        if override is not None:
            if rebuild_environment:
                raise ValueError('Cannot combine dependency infrastructure override with rebuilding')
            self.snapshot = override['snapshot']
            self.provider_directory = Path(override['provider_directory'])
            self.environment = {**self.environment, 'build': override['build']}
            self.runtime.environment = self.environment
            write_json(self.directory / 'infrastructure_override.json', override)
        elif prepared_receipt.is_file() and not rebuild_environment:
            self.provider_directory = prepared_provider
            self.snapshot = prepared_name
        elif cached and not rebuild_environment:
            # This sealed image contains infrastructure only; reference code was never uploaded.
            self.provider_directory = self.bundle.root / "private/provider"
            self.snapshot = cached
        else:
            # Explicit portable route when the provider image is unavailable/on another account.
            self.snapshot = prepare_environment(self.environment, self.directory)
            self.provider_directory = self.directory / "environment/provider"
        self.runtime.backend = modal_backend(self.provider_directory)
        try:
            # All dependencies are in the infrastructure snapshot. Candidate
            # code must not retrieve the removed implementation from upstream.
            self.runtime.open(snapshot=self.snapshot, network_blocked=True)
            self.runtime.install_source(self.bundle.root / self.meta["workspace_archive"])
            public_names = self.bundle.manifest['public_files']
            for name in public_names:
                self.runtime.backend.write_file(self.runtime.sandbox, path='/work/' + name,
                                                content=(self.bundle.root / name).read_bytes())
            if fixture := self.meta.get("fixture_archive"):
                self.runtime.backend.write_file(self.runtime.sandbox, path="/work/fixtures.tar",
                                                content=(self.bundle.root / fixture).read_bytes())
            result = self.runtime.command(
                "initialize-public-workspace",
                initialization_commands(self.environment, self.meta),
                timeout=self.environment.get('build_timeout', 1800))
            if result.exit_code:
                raise RuntimeError("Cannot initialize public candidate workspace; inspect initialization log")
            task = (self.bundle.root / 'public/task.txt').read_text()
            self.runtime.backend.write_file(self.runtime.sandbox, path="/work/TASK.md", content=task.encode())
            return self
        except BaseException:
            self.close()
            raise

    def execute(self, command, *, timeout=120):
        # Terminal is the code-task affordance; commands remain in the same sandbox.
        return self.runtime.command("agent-" + str(time.time_ns()), command, timeout=timeout)

    def reset(self):
        # Reuse infrastructure image; discard source, processes and task state atomically.
        self.close()
        return self.open()

    def export_submission(self, output):
        result = self.runtime.command("export", "set -e; tar cf /work/submission.tar "
                                      "--exclude=.git --exclude=node_modules --exclude=ui/build "
                                      "--exclude=navidrome -C /work/project .")
        if result.exit_code:
            raise RuntimeError("Submission export failed")
        Path(output).write_bytes(self.runtime.read_archive('/work/submission.tar'))
        return Path(output)

    def grade(self):
        submission = self.export_submission(self.directory / "submission.tar")
        return self.grade_submission(submission)

    def grade_submission(self, submission):
        """Evaluator API: accepted source archive is checked in an independent world."""
        from .archives import check_archive
        submission = Path(submission)
        check_archive(submission)
        grader = self.runtime_type(self.environment, self.directory / ("grade-" + str(time.time_ns())),
                                backend=modal_backend(self.provider_directory))
        try:
            # Submitted build hooks also run offline, before trusted checks.
            grader.open(snapshot=self.snapshot, network_blocked=True)
            grader.install_source(submission)
            if grader.build().exit_code:
                return {"status": "failed", "task_success": False, "gc": 0,
                        "error": "Submitted implementation does not build", "phase": "submission_build"}
            return grader.verify(self.bundle.root / "private/verifier", self.contract)
        finally:
            grader.close()

    def close(self):
        self.runtime.close()

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.close()
