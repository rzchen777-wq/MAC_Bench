"""Coordinated native checkpoints with explicit backend coverage.

A reset restores application storage through registered infrastructure
adapters. Replacing a compiler JSON snapshot is never an environment reset.
All backends are quiesced before capture/restore, and full symbolic projection
readback is checked independently by the native execution orchestrator.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from mac_bench.environments.catalog.contracts import Source
from mac_bench.environments.errors import CompileFailure


@dataclass(frozen=True)
class SnapshotAdapter:
    name: str
    backends: frozenset[str]
    resources: tuple[str, ...]
    source: Source
    quiesce: Callable[[], None]
    capture: Callable[[], dict]
    verify: Callable[[dict], None]
    restore: Callable[[dict], None]
    resume: Callable[[], None]
    preflight: Callable[[], dict]


class NativeCheckpoint:
    def __init__(self, adapters: list[SnapshotAdapter], journal: Path):
        self.adapters = tuple(adapters)
        self.journal = Path(journal)
        if self.journal.exists():
            raise CompileFailure('checkpoint_journal_already_exists')
        self.tokens = {}
        self.phase = 'new'
        self.pending = None
        self.history = []

    def require_coverage(self, catalog):
        required = {spec['backend'] for spec in catalog.values()}
        owners = {}
        names, resources = set(), set()
        for adapter in self.adapters:
            if not adapter.name or adapter.name in names or not adapter.resources or not adapter.backends:
                raise CompileFailure('invalid_snapshot_adapter_registration')
            names.add(adapter.name)
            if len(set(adapter.resources)) != len(adapter.resources) or resources.intersection(adapter.resources):
                raise CompileFailure('snapshot_resource_has_multiple_owners')
            resources.update(adapter.resources)
            if not adapter.source.uri or not adapter.source.digest:
                raise CompileFailure('snapshot_adapter_source_missing')
            if not all(callable(getattr(adapter, role)) for role in ('preflight', 'quiesce', 'capture', 'verify', 'restore', 'resume')):
                raise CompileFailure('snapshot_adapter_operation_missing')
            for backend in adapter.backends:
                if backend in owners:
                    raise CompileFailure(f'snapshot_backend_has_multiple_owners:{backend}')
                owners[backend] = adapter.name
        if required - owners.keys():
            raise CompileFailure(f'snapshot_backend_coverage_missing:{sorted(required - owners.keys())}')
        return {'backends': sorted(required),
                'adapters': [{'name': adapter.name, 'backends': sorted(adapter.backends),
                              'resources': list(adapter.resources), 'source': asdict(adapter.source)}
                             for adapter in self.adapters]}

    def _save(self):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        content = {'phase': self.phase, 'pending': self.pending, 'tokens': self.tokens, 'history': self.history}
        temporary = self.journal.with_name('.' + self.journal.name + '.' + uuid.uuid4().hex)
        data = (json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.journal)
            directory = os.open(self.journal.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _call(self, adapter, role, *args):
        self.pending = {'adapter': adapter.name, 'operation': role}
        self._save()
        result = getattr(adapter, role)(*args)
        if role == 'preflight' and (not isinstance(result, dict) or not result):
            raise CompileFailure('snapshot_preflight_requires_inspection_receipt')
        if role == 'capture':
            if not isinstance(result, dict) or not result:
                raise CompileFailure('snapshot_capture_requires_persistent_locator')
            json.dumps(result, allow_nan=False)
            self.tokens[adapter.name] = result
        # Capture locator and clearing pending must be one durable transition.
        self.history.append({**self.pending, 'returned': True})
        if role in {'preflight', 'restore', 'resume'} and isinstance(result, dict):
            # Infrastructure receipts contain resource identities and hashes,
            # not private container configuration or credentials.
            json.dumps(result, allow_nan=False)
            self.history[-1]['receipt'] = result
        self.pending = None
        self._save()
        return result

    def preflight(self, catalog):
        """Inspect infrastructure before baseline materialization changes apps."""
        if self.phase != 'new' or self.pending is not None:
            raise CompileFailure('checkpoint_preflight_requires_new_session')
        coverage = self.require_coverage(catalog)
        self.phase = 'preflight'
        inspections = {}
        for adapter in self.adapters:
            inspections[adapter.name] = self._call(adapter, 'preflight')
        self.phase = 'prepared'
        self._save()
        return {**coverage, 'preflight': inspections}

    def capture(self, catalog):
        if self.phase == 'new' and self.pending is None:
            self.preflight(catalog)
        if self.phase != 'prepared' or self.pending is not None:
            raise CompileFailure('checkpoint_capture_requires_new_session')
        coverage = self.require_coverage(catalog)
        self.phase = 'quiescing_for_capture'
        # Failures deliberately leave the native state and journal unresolved.
        # Resuming only some applications can expose inconsistent storage.
        for adapter in self.adapters:
            self._call(adapter, 'quiesce')
        self.phase = 'capturing'
        for adapter in self.adapters:
            self._call(adapter, 'capture')
        self.phase = 'verifying_captured_set'
        for adapter in self.adapters:
            self._call(adapter, 'verify', self.tokens[adapter.name])
        self.phase = 'resuming_after_capture'
        for adapter in reversed(self.adapters):
            self._call(adapter, 'resume')
        self.phase = 'captured'
        self._save()
        return coverage

    def restore(self):
        if self.phase not in {'captured', 'restored_verified'} or self.pending is not None:
            raise CompileFailure('checkpoint_restore_requires_complete_capture')
        if set(self.tokens) != {adapter.name for adapter in self.adapters}:
            raise CompileFailure('checkpoint_snapshot_set_incomplete')
        self.phase = 'quiescing_for_restore'
        for adapter in self.adapters:
            self._call(adapter, 'quiesce')
        self.phase = 'verifying_restore_set'
        for adapter in self.adapters:
            self._call(adapter, 'verify', self.tokens[adapter.name])
        self.phase = 'restoring'
        for adapter in self.adapters:
            self._call(adapter, 'restore', self.tokens[adapter.name])
        self.phase = 'resuming_after_restore'
        for adapter in reversed(self.adapters):
            self._call(adapter, 'resume')
        self.phase = 'restored_unverified'
        self._save()

    def confirm_readback(self):
        if self.phase != 'restored_unverified' or self.pending is not None:
            raise CompileFailure('checkpoint_confirmation_out_of_order')
        self.phase = 'restored_verified'
        self._save()
