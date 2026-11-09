"""Isolated code workspaces and independent grading on Modal or Daytona."""

import base64
import gzip
from hashlib import sha256
import json
import shlex
import time
from pathlib import Path

from .artifacts import write_json
from .verifier import score_product
from ..public_workspace import initialization_commands


from mac_bench.runtime.sandbox.providers import create_backend, resolve_config
from ..preparation import read_environment, prepare_environment


class ProductRuntime:
    def __init__(self, environment, directory, *, backend=None):
        self.environment = environment
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.backend = backend or create_backend(resolve_config('software'))
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

    def open(self, *, prepared=None, network_blocked=False):
        if self.sandbox is not None:
            raise RuntimeError("Runtime is already open")
        arguments = {"name": "product-world", "network_blocked": network_blocked}
        arguments.update({"prepared": prepared} if prepared else {"image": self.environment["image"]})
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
        self.backend.upload_tree(self.sandbox, local_dir=verifier_directory, remote_dir="/work/grader")
        result = self.command("verify-" + mode,
                              "set -e; cd /work/project; " + self.environment["verify"],
                              timeout=self.environment.get("verify_timeout", 900))
        try:
            report = self.command("verifier-result-" + mode, "cat /work/grader/result.json", timeout=60)
            if report.exit_code:
                raise OSError("Verifier did not write result.json")
            execution = json.loads(report.stdout)
        except (OSError, ValueError) as error:
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
        self.contract = json.loads((self.bundle.root / "private/contract.json").read_text())
        self.backend = backend or create_backend(resolve_config('software'))
        self.prepared_image = None
        self.runtime = self.runtime_type(self.environment, self.directory / "agent", backend=self.backend)

    def open(self, *, rebuild_environment=False):
        if self.runtime.sandbox is not None:
            raise RuntimeError("Product world is already open")
        if self.prepared_image is None or rebuild_environment:
            self.prepared_image = prepare_environment(self.environment, self.directory,
                backend=self.backend, rebuild=rebuild_environment)
        try:
            # All dependencies are in the infrastructure snapshot. Candidate
            # code must not retrieve the removed implementation from upstream.
            self.runtime.open(prepared=self.prepared_image, network_blocked=True)
            self.runtime.install_source(self.bundle.root / self.meta["workspace_archive"])
            files = {'/work/' + name: (self.bundle.root / name).read_bytes()
                     for name in self.bundle.manifest['public_files']}
            files['/work/TASK.md'] = (self.bundle.root / 'public/task.txt').read_bytes()
            if fixture := self.meta.get("fixture_archive"):
                files['/work/fixtures.tar'] = (self.bundle.root / fixture).read_bytes()
            self.runtime.backend.write_files(self.runtime.sandbox, files=files)
            result = self.runtime.command(
                "initialize-public-workspace",
                initialization_commands(self.environment, self.meta),
                timeout=self.environment.get('build_timeout', 1800))
            if result.exit_code:
                raise RuntimeError("Cannot initialize public candidate workspace; inspect initialization log")
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
                                backend=self.backend)
        try:
            # Submitted build hooks also run offline, before trusted checks.
            grader.open(prepared=self.prepared_image, network_blocked=True)
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
