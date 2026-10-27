"""Reuse complete, compatible evaluations; never replay interrupted agents."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
from hashlib import sha256
import json
import math
from pathlib import Path
import tempfile
from uuid import uuid4


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    temporary.replace(path)


def episode_identity(record, method, connection, budget, repetition, *, namespace='main'):
    """Resolve non-secret provenance before constructing any client or world."""
    from mac_bench.baselines.common.config import ROOT, tree_hash, redact_configuration
    spec = redact_configuration(dict(method.spec))
    spec.pop('_spec_path', None)
    sources = [ROOT/'src/mac_bench/baselines'/method.name,
               ROOT/'src/mac_bench/baselines/common',
               ROOT/'src/mac_bench/evaluation',
               ROOT/'src/mac_bench/environments',
               ROOT/'src/mac_bench/runtime']
    if spec.get('source'):
        sources.append(ROOT / spec['source']['directory'])
    assets = {}
    for name, team in spec.get('teams', {}).items():
        # Meta-Team declares pool artifacts; Magentic-One declares member lists.
        if isinstance(team, dict) and team.get('artifact'):
            assets[name] = tree_hash(ROOT / team['artifact'])
    for name, pool in getattr(method, 'pools', {}).items():
        assets[name] = tree_hash(Path(pool['path']))
    frozen = spec.get('learning', {}).get('frozen_artifact')
    if frozen:
        path = Path(frozen)
        path = path if path.is_absolute() else ROOT / path
        if path.is_dir():
            path = path / 'manifest.json'
        assets['frozen_manifest'] = sha256(path.read_bytes()).hexdigest()
    return {
        'format': 'collaboration.episode.identity.v1', 'namespace': namespace,
        'task_id': record.task_id, 'world_revision': record.world_revision,
        'category': record.category, 'split': record.split, 'phase': 'test',
        'method': method.name, 'method_configuration': spec,
        'model': {name: getattr(connection, name) for name in
                  ('base_url', 'model', 'timeout_seconds', 'max_retries', 'thinking_mode', 'image_input')},
        'budget': asdict(budget), 'repetition': repetition,
        'implementation': {str(path.relative_to(ROOT)): tree_hash(path) for path in sources},
        'assets': assets,
    }


def identity_key(identity):
    return sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _valid_result(result, identity):
    if not isinstance(result, dict):
        return False
    gc = result.get('gc')
    return (result.get('status') == 'completed' and result.get('closed') is True
            and result.get('stop_reason') not in {'context_capacity', 'context_summary_failed', 'model_output_truncated'}
            and result.get('evaluation_status') in {'passed', 'failed'}
            and type(result.get('task_success')) is bool
            and type(gc) in (int, float) and math.isfinite(gc) and 0 <= gc <= 1
            and result.get('initial_state_matches') is True
            and result.get('task_id') == identity['task_id']
            and result.get('world_revision') == identity['world_revision']
            and result.get('baseline') == identity['method']
            and result.get('model') == identity['model']['model']
            and result.get('budget') == identity['budget']
            and not result.get('agents_active_at_return')
            and not any(result.get(key) for key in
                        ('error_type', 'finalization_error_type', 'close_error_type', 'receipt_export_error')))


def _inventory(directory):
    files = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Cache evidence must not contain symlinks')
        if path.is_file():
            files[str(path.relative_to(directory))] = sha256(path.read_bytes()).hexdigest()
    return files


def _valid_evidence(directory, identity):
    from ..reports.metrics import evaluation_fields
    required = ['episode/result.json', 'episode/evaluation.json', 'episode/final.json',
                'episode/initial.json', 'episode/events.jsonl']
    if not all((directory / name).is_file() for name in required):
        return False
    try:
        result = json.loads((directory / 'episode/result.json').read_text())
        verdict = json.loads((directory / 'episode/evaluation.json').read_text())
        if not isinstance(verdict, dict):
            return False
        if (verdict.get('gaps') or verdict.get('ambiguities') or verdict.get('errors')
                or any(not isinstance(check, dict) or check.get('status') == 'error'
                       for check in verdict.get('checks', []))):
            return False
        fields = evaluation_fields(verdict)
        if not _valid_result(result, identity) or any(result.get(k) != v for k, v in fields.items()):
            return False
        for path in (directory / 'runtime').rglob('sandbox.json'):
            receipt = json.loads(path.read_text())
            if not isinstance(receipt, dict) or receipt.get('status') != 'terminated':
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


class CompletedEpisodeCache:
    """Cache owns attempt artifacts, so moving an experiment report is harmless.

    A per-key OS lock covers lookup, execution, cleanup and publication. Process
    death releases the lock; abandoned attempts never become completed entries.
    """
    def __init__(self, root):
        self.root = Path(root).resolve()

    @contextmanager
    def _lock(self, key):
        entry = self.root / key[:2] / key
        entry.mkdir(parents=True, exist_ok=True)
        with (entry / '.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield entry

    def _lookup(self, entry, identity):
        try:
            receipt = json.loads((entry / 'completed.json').read_text())
            if (not isinstance(receipt, dict)
                    or receipt.get('format') != 'collaboration.completed_episode.v1'
                    or receipt['identity'] != identity):
                return None
            directory = (entry / receipt['attempt']).resolve()
            if not directory.is_relative_to(entry / 'attempts'):
                return None
            if _inventory(directory) != receipt['files']:
                return None
            result = json.loads((directory / 'episode/result.json').read_text())
            if not _valid_evidence(directory, identity):
                return None
            return result, directory
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def get_or_run(self, identity, execute):
        """execute(attempt_dir) must stop workers, grade and close its world."""
        key = identity_key(identity)
        with self._lock(key) as entry:
            cached = self._lookup(entry, identity)
            if cached is not None:
                result, directory = cached
                return result, {'key': key, 'reused': True, 'artifact_directory': str(directory)}
            attempt = entry / 'attempts' / uuid4().hex
            attempt.mkdir(parents=True)
            _write(attempt / 'identity.json', identity)
            # Raised exceptions leave the attempt and its partial evidence intact.
            result = execute(attempt)
            result_path = attempt / 'episode/result.json'
            eligible = (_valid_evidence(attempt, identity)
                        and json.loads(result_path.read_text()) == result)
            if eligible:
                # Retain an invalidated previous receipt for provenance.
                old = entry / 'completed.json'
                if old.exists():
                    old.rename(entry / ('invalidated-' + uuid4().hex + '.json'))
                _write(entry / 'completed.json', {
                    'format': 'collaboration.completed_episode.v1', 'identity': identity,
                    'attempt': str(attempt.relative_to(entry)), 'files': _inventory(attempt)})
            return result, {'key': key, 'reused': False, 'cacheable': eligible,
                            'artifact_directory': str(attempt)}
