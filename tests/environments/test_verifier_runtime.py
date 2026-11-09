"""Verifier installation and independent SQL execution limits."""
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from mac_bench.environments.packaging.lifecycle import private_verifier_files
from mac_bench.environments.verification.contracts import package
from mac_bench.environments.verification.errors import VerificationError
from mac_bench.environments.verification.snapshot import make_layout, source_cards
from mac_bench.environments.verification.sql import QueryEngine


@pytest.fixture
def contract():
    text = 'Complete the order.'
    initial = {'orders': [{'id': 1, 'done': False}]}
    sources = source_cards(initial, {'orders': 'Customer orders'})
    requirements = {'clauses': [{'id': 'completed', 'kind': 'result', 'requirement': text,
        'evidence': [{'section': 'task', 'quote': text}], 'allowed_choices': ''}],
        'goals': [{'id': 'delivery', 'description': text, 'clause_ids': ['completed']}],
        'global_clause_ids': [], 'context': [], 'ambiguities': []}
    checks = [{'id': 'order_done', 'clause': 'completed', 'description': text,
        'sql': 'SELECT id FROM current.orders WHERE NOT done', 'assertion': {'mode': 'no_rows'},
        'binding': {'status': 'bound', 'collections': ['orders'],
                    'interpretation': 'Read completed orders.', 'reason': ''}}]
    return package([{'id': 'task', 'text': text}], initial, {}, sources, requirements, checks, [])


@pytest.mark.parametrize('valid', [True, False])
def test_batch_preserves_verified_bytes_and_rejects_invalid_revision(tmp_path, contract, valid):
    if not valid:
        contract['revision'] = '0' * 64
    content = json.dumps(contract, indent=3).encode() + b'\n\n'
    profile = b'def build(root):\n    return root\n'
    (tmp_path / 'private').mkdir()
    (tmp_path / 'private/verifier.json').write_bytes(content)
    (tmp_path / 'private/profile.py').write_bytes(profile)
    bundle = SimpleNamespace(root=tmp_path, manifest={
        'files': {'private/verifier.json': sha256(content).hexdigest(),
                  'private/profile.py': sha256(profile).hexdigest()},
        'metadata': {'private_verifier_artifact': {'path': 'private/verifier.json'}}})
    if not valid:
        with pytest.raises(VerificationError, match='integrity mismatch'):
            private_verifier_files(bundle)
        return
    assert private_verifier_files(bundle) == {
        '/work/world/verifier.json': content, '/work/world/profile.py': profile}


def engine(max_steps):
    initial = {'orders': [{'id': i} for i in range(100)]}
    return QueryEngine(initial, initial, {},
        make_layout(source_cards(initial, {'orders': 'Orders'})), max_steps=max_steps)


@pytest.mark.parametrize('failure', ['undeclared', 'symlink'])
def test_observer_must_be_declared_and_contained(tmp_path, contract, failure):
    content = json.dumps(contract).encode()
    profile = b'def build(root):\n    return root\n'
    (tmp_path / 'private').mkdir()
    (tmp_path / 'private/verifier.json').write_bytes(content)
    (tmp_path / 'private/profile.py').write_bytes(profile)
    bundle = SimpleNamespace(root=tmp_path, manifest={
        'files': {'private/verifier.json': sha256(content).hexdigest(),
                  'private/profile.py': sha256(profile).hexdigest()},
        'metadata': {'private_verifier_artifact': {'path': 'private/verifier.json'}}})
    if failure == 'undeclared':
        del bundle.manifest['files']['private/profile.py']
    else:
        observer = tmp_path / 'private/profile.py'
        observer.unlink()
        observer.symlink_to(tmp_path / 'private/verifier.json')
    with pytest.raises(ValueError):
        private_verifier_files(bundle)


def test_sql_checks_have_independent_budgets():
    with engine(2_000) as db:
        for _ in range(30):
            assert db.check('SELECT sum(id) AS total FROM current.orders', ['orders'],
                assertion={'mode': 'scalar', 'operator': 'eq', 'expected': 4950})['passed']


def test_sql_limit_stops_expensive_query_without_poisoning_next_check():
    with engine(2_000) as db:
        with pytest.raises(VerificationError, match='interrupted'):
            db.check('SELECT sum(a.id*b.id*c.id) AS total FROM current.orders a '
                     'CROSS JOIN current.orders b CROSS JOIN current.orders c', ['orders'])
        assert db.check('SELECT id FROM current.orders WHERE id < 0', ['orders'])['passed']
