"""NativeCheckpoint adapter for Docker services running inside Modal."""
import json
import shlex
from hashlib import sha256
from pathlib import Path, PurePosixPath

from mac_bench.environments.catalog.contracts import Source
from mac_bench.environments.errors import CompileFailure
from mac_bench.environments.runtime.infrastructure.container_snapshot.configuration import (
    validate,
)
from mac_bench.environments.runtime.infrastructure.container_snapshot.engine import (
    SnapshotError,
)
from mac_bench.environments.runtime.reset import SnapshotAdapter


class RemoteContainerSnapshots:
    def __init__(self, backend, sandbox, *, spec, store, worker_files=None):
        if sandbox.provider != 'modal':
            raise CompileFailure('container_snapshot_requires_modal_sandbox')
        try:
            validate(spec, store)
        except SnapshotError as error:
            raise CompileFailure(str(error)) from None
        self.backend, self.sandbox = backend, sandbox
        self.spec, self.store = dict(spec), store
        root = Path(__file__).parent
        self.files = {name: (root / name).read_bytes() for name in (
            '__init__.py', '__main__.py', 'engine.py', 'storage.py', 'coordinator.py', 'configuration.py')}
        if worker_files is not None:
            if (set(worker_files) != set(self.files)
                    or any(not isinstance(raw, bytes) or not raw for raw in worker_files.values())):
                raise CompileFailure('container_snapshot_frozen_worker_invalid')
            self.files = dict(worker_files)
        self.hashes = {name: sha256(raw).hexdigest() for name, raw in self.files.items()}
        self.digest = sha256(json.dumps(self.hashes, sort_keys=True).encode()).hexdigest()
        self.uploaded = False

    def _call(self, operation, token=None):
        control = str(PurePosixPath(self.store) / '.control')
        package = 'snapshot_' + self.digest
        if not self.uploaded:
            result = self.backend.exec(self.sandbox, shlex.join(['mkdir', '-p', '-m', '700', '--', control]), timeout=30)
            if result.exit_code:
                raise CompileFailure('container_snapshot_worker_directory_failed')
            files = {str(PurePosixPath(control) / package / name):content for name,content in self.files.items()}
            if callable(getattr(self.backend, 'write_files', None)):
                self.backend.write_files(self.sandbox, files=files)
            else:
                for path,content in files.items():
                    self.backend.write_file(self.sandbox, path=path, content=content)
            self.uploaded = True
        request = {'operation': operation, 'spec': self.spec, 'store': self.store, 'token': token}
        launcher = (
            'import hashlib,json,pathlib,runpy,sys; package,hashes,payload=sys.argv[1:]; '
            'expected=json.loads(hashes); '
            'valid=all(hashlib.sha256((pathlib.Path(package)/name).read_bytes()).hexdigest()==value '
            'for name,value in expected.items()); '
            'valid or sys.exit(2); sys.argv=[package,payload]; runpy.run_module(package,run_name="__main__")')
        result = self.backend.exec(self.sandbox, shlex.join(['python3', '-c', launcher, package,
            json.dumps(self.hashes), json.dumps(request, allow_nan=False)]), cwd=control,
            timeout=self.spec['timeout_seconds'])
        try:
            response = json.loads(result.stdout)
        except (TypeError, ValueError):
            raise CompileFailure('container_snapshot_outcome_unresolved') from None
        if result.exit_code or not isinstance(response, dict) or set(response) != {'result'}:
            reason = response.get('error') if isinstance(response, dict) else None
            if isinstance(reason, str) and reason.startswith('container_snapshot_'):
                raise CompileFailure(reason)
            raise CompileFailure('container_snapshot_outcome_unresolved')
        return response['result']

    def _token(self, token):
        if (not isinstance(token, dict) or set(token) != {'sandbox_id', 'worker_digest', 'storage'}
                or token['sandbox_id'] != self.sandbox.id or token['worker_digest'] != self.digest):
            raise CompileFailure('container_snapshot_sandbox_or_implementation_changed')
        return token['storage']

    def collect_diagnostics(self, directory):
        """Export private, bounded diagnostics without forwarding them to an agent."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        remote = str(PurePosixPath(self.store) / 'diagnostics')
        for name in ['summary.json', *(name + '.log' for name in self.spec['containers'])]:
            try:
                raw = self.backend.read_file(self.sandbox, path=remote + '/' + name)
            except Exception:
                continue
            target = directory / name
            target.write_bytes(raw)
            target.chmod(0o600)

    def capture(self):
        return {'sandbox_id': self.sandbox.id, 'worker_digest': self.digest, 'storage': self._call('capture')}

    def adapter(self, *, name, backends):
        evidence = {'sandbox_id': self.sandbox.id, 'worker_digest': self.digest,
                    'spec': self.spec, 'store': self.store}
        source = Source.capture(Path(__file__).resolve().as_uri(), {'adapter': evidence}, '/adapter')
        resources = tuple([f'modal:{self.sandbox.id}:docker:{container}' for container in self.spec['containers']]
                          + [f'modal:{self.sandbox.id}:{path}' for path in self.spec['owned_bind_roots']])
        return SnapshotAdapter(name, frozenset(backends), resources, source,
            lambda: self._call('quiesce'), self.capture,
            lambda token: self._call('verify', self._token(token)),
            lambda token: self._call('restore', self._token(token)),
            lambda: self._call('resume'), preflight=lambda: self._call('preflight'))
