import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.evaluation.reports.metrics import evaluation_fields
from mac_bench.evaluation.suites.cache import (
    CompletedEpisodeCache, episode_identity, identity_key,
)


def identity():
    return {'task_id': 'sample', 'world_revision': 'sealed-revision',
            'method': 'react_single', 'model': {'model': 'backbone', 'base_url': 'https://provider'},
            'budget': {'max_tokens': 1000}, 'repetition': 1}


def complete(directory, definition, *, success=False, status='completed'):
    episode = directory / 'episode'
    episode.mkdir()
    score = {'status': 'passed' if success else 'failed', 'gc': 1.0 if success else 0.0,
             'task_success': success}
    result = {**evaluation_fields(score), 'status': status, 'closed': True,
              'initial_state_matches': True, 'task_id': definition['task_id'],
              'world_revision': definition['world_revision'], 'baseline': definition['method'],
              'model': definition['model']['model'], 'budget': definition['budget'],
              'stop_reason': 'budget:tokens', 'input_tokens': 1000}
    for name, data in [('result', result), ('evaluation', score), ('initial', {}), ('final', {})]:
        (episode / (name + '.json')).write_text(json.dumps(data))
    (episode / 'events.jsonl').write_text('{}\n')
    (directory / 'runtime').mkdir()
    (directory / 'runtime/sandbox.json').write_text('{"status":"terminated"}')
    (directory / 'runtime/submission.tar').write_bytes(b'actual submitted bytes')
    return result


@pytest.mark.parametrize('success', [True, False])
def test_completed_including_zero_score_and_budget_stop_is_not_run_twice(tmp_path, success):
    cache = CompletedEpisodeCache(tmp_path / 'cache')
    original, first = cache.get_or_run(identity(), lambda p: complete(p, identity(), success=success))
    def forbidden(_):
        pytest.fail('A completed hit must not allocate a sandbox or call a provider')
    reused, second = CompletedEpisodeCache(tmp_path / 'cache').get_or_run(identity(), forbidden)
    assert reused == original and second['reused'] and not first['reused']
    assert reused['input_tokens'] == 1000
    assert first['artifact_directory'] == second['artifact_directory']


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(world_revision='new'),
    lambda d: d.update(method='aorchestra'),
    lambda d: d['model'].update(model='another'),
    lambda d: d['model'].update(base_url='https://another-provider'),
    lambda d: d['budget'].update(max_tokens=2000),
    lambda d: d.update(repetition=2),
    lambda d: d.update(implementation={'adapter': 'changed'}),
])
def test_different_experiment_identity_does_not_reuse(tmp_path, mutation):
    cache = CompletedEpisodeCache(tmp_path)
    a = identity(); cache.get_or_run(a, lambda p: complete(p, a))
    b = copy.deepcopy(a); mutation(b)
    _, info = cache.get_or_run(b, lambda p: complete(p, b))
    assert not info['reused'] and identity_key(a) != identity_key(b)


def test_error_attempt_kept_and_retry_is_fresh(tmp_path):
    cache = CompletedEpisodeCache(tmp_path)
    _, failed = cache.get_or_run(identity(), lambda p: complete(p, identity(), status='error'))
    _, retried = cache.get_or_run(identity(), lambda p: complete(p, identity()))
    assert not failed['cacheable'] and retried['cacheable']
    assert failed['artifact_directory'] != retried['artifact_directory']
    assert Path(failed['artifact_directory']).exists()


@pytest.mark.parametrize('damage', ['submission', 'missing_score', 'pending_cleanup', 'corrupt_receipt'])
def test_damaged_or_incomplete_evidence_is_not_reused(tmp_path, damage):
    cache = CompletedEpisodeCache(tmp_path)
    _, old = cache.get_or_run(identity(), lambda p: complete(p, identity()))
    directory = Path(old['artifact_directory'])
    if damage == 'submission':
        (directory / 'runtime/submission.tar').write_bytes(b'corrupt')
    elif damage == 'missing_score':
        (directory / 'episode/evaluation.json').unlink()
    elif damage == 'pending_cleanup':
        (directory / 'runtime/sandbox.json').write_text('{"status":"running"}')
    else:
        (directory.parent.parent / 'completed.json').write_text('not json')
    _, new = cache.get_or_run(identity(), lambda p: complete(p, identity()))
    assert not new['reused'] and new['artifact_directory'] != str(directory)


def test_concurrent_same_episode_executes_once(tmp_path):
    cache = CompletedEpisodeCache(tmp_path)
    started, release = Event(), Event()
    calls = []
    def execute(path):
        calls.append(path); started.set(); assert release.wait(5)
        return complete(path, identity())
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cache.get_or_run, identity(), execute)
        assert started.wait(5)
        second = pool.submit(cache.get_or_run, identity(), execute)
        release.set()
        assert sorted([first.result()[1]['reused'], second.result()[1]['reused']]) == [False, True]
    assert len(calls) == 1


def test_identity_ignores_credentials_and_config_file_location(tmp_path, monkeypatch):
    import mac_bench.baselines.common.config as config
    monkeypatch.setattr(config, 'tree_hash', lambda _: 'content-hash')
    record = SimpleNamespace(task_id='task', world_revision='rev', category='platform_data', split='test')
    method = SimpleNamespace(name='react_single', spec={'llm': {'api_key': 'SECRET'}, '_spec_path': '/first'})
    connection = SimpleNamespace(base_url='https://provider', model='backbone', timeout_seconds=180,
                                 max_retries=0, thinking_mode='disabled', image_input=False, api_key='SECRET')
    first = episode_identity(record, method, connection, AgentBudget(), 1)
    method.spec['_spec_path'] = '/elsewhere'; method.spec['llm']['api_key'] = 'DIFFERENT'
    connection.api_key = 'DIFFERENT'
    assert first == episode_identity(record, method, connection, AgentBudget(), 1)
    assert 'SECRET' not in json.dumps(first) and 'DIFFERENT' not in json.dumps(first)


def test_identity_hashes_frozen_pool_directory_manifest(tmp_path, monkeypatch):
    import mac_bench.baselines.common.config as config
    from hashlib import sha256
    monkeypatch.setattr(config, 'tree_hash', lambda _: 'content-hash')
    pool = tmp_path / 'frozen'; pool.mkdir()
    manifest = pool / 'manifest.json'; manifest.write_text('{"domains": {}}')
    record = SimpleNamespace(task_id='task', world_revision='rev', category='platform_data', split='test')
    connection = SimpleNamespace(base_url='https://provider', model='backbone', timeout_seconds=180,
                                 max_retries=0, thinking_mode='disabled', image_input=False)
    method = SimpleNamespace(name='meta_team', spec={'learning': {'frozen_artifact': str(pool)}})
    value = episode_identity(record, method, connection, AgentBudget(), 1)
    assert value['assets']['frozen_manifest'] == sha256(manifest.read_bytes()).hexdigest()
    manifest.write_text('{"domains": {"engineering": {}}}')
    assert identity_key(value) != identity_key(episode_identity(record, method, connection, AgentBudget(), 1))


def test_identity_accepts_member_lists_and_hashes_pool_artifacts(monkeypatch):
    import mac_bench.baselines.common.config as config
    monkeypatch.setattr(config, 'tree_hash', lambda _: 'content-hash')
    record = SimpleNamespace(task_id='task', world_revision='rev', category='platform_data', split='dev')
    connection = SimpleNamespace(base_url='https://provider', model='backbone', timeout_seconds=180,
                                 max_retries=0, thinking_mode='disabled', image_input=False)
    method = SimpleNamespace(name='magentic_one', spec={'teams': {'engineering': [{'name': 'Coder'}]}})
    assert episode_identity(record, method, connection, AgentBudget(), 1)['assets'] == {}
    method.name = 'meta_team'
    method.spec['teams']['engineering'] = {'artifact': 'configs/baselines/teams/meta_team/engineering'}
    assert episode_identity(record, method, connection, AgentBudget(), 1)['assets'] == {'engineering': 'content-hash'}


@pytest.mark.parametrize('interrupted_second,reuse_completed', [(False, True), (True, True), (False, False)])
def test_cli_cache_is_opt_in_and_debug_runs_are_fresh(tmp_path, monkeypatch, interrupted_second, reuse_completed):
    import mac_bench.baselines.common.config as config
    import mac_bench.baselines.common.registry as registry
    from mac_bench.evaluation import __main__ as cli
    from mac_bench.environments.packaging.bundle import seal_bundle
    world = tmp_path / 'world'; (world / 'public').mkdir(parents=True)
    (world / 'public/task.txt').write_text('Implement behavior.')
    seal_bundle(world, world_id='sample', public_files=['public/task.txt'],
                metadata={'world_family': 'platform_data'})
    connection = SimpleNamespace(base_url='https://provider', model='backbone', timeout_seconds=180,
                                 max_retries=0, thinking_mode='disabled', image_input=False)
    method = SimpleNamespace(name='react_single', spec={})
    monkeypatch.setattr(registry, 'create_method', lambda *a, **k: method)
    monkeypatch.setattr(config, 'model_connection', lambda _: connection)
    monkeypatch.setattr(config, 'tree_hash', lambda _: 'content-hash')
    calls = {'client': 0, 'world': 0, 'provider': 0, 'sandbox': 0}
    def client(_):
        calls['client'] += 1; return object()
    def task(record, directory):
        calls['world'] += 1; return SimpleNamespace(record=record)
    def episode(task, method, client, *, output, budget, close):
        assert close
        calls['provider'] += 1; calls['sandbox'] += 1
        definition = {'task_id': task.record.task_id, 'world_revision': task.record.world_revision,
                      'method': method.name, 'model': {'model': connection.model}, 'budget': asdict(budget)}
        status = 'error' if interrupted_second and calls['provider'] == 2 else 'completed'
        return complete(output.parent, definition, status=status)
    monkeypatch.setattr(config, 'model_client', client)
    monkeypatch.setattr(cli, 'open_task', task)
    monkeypatch.setattr(cli, 'run_episode', episode)
    repetitions = 2 if interrupted_second else 1
    args = ['--world', str(world), '--cache-dir', str(tmp_path / 'cache'), '--repetitions', str(repetitions)]
    if reuse_completed:
        args.append('--reuse-completed')
    assert cli.main(args + ['--out', str(tmp_path / 'first')]) == int(interrupted_second)
    before = dict(calls)
    assert cli.main(args + ['--out', str(tmp_path / 'second')]) == 0
    if not reuse_completed:
        assert calls == {'client': 2, 'world': 2, 'provider': 2, 'sandbox': 2}
        assert not (tmp_path / 'cache').exists()
        assert cli.main(args + ['--out', str(tmp_path / 'second')]) == 0
        assert calls == {'client': 3, 'world': 3, 'provider': 3, 'sandbox': 3}
        report = json.loads((tmp_path / 'second/summary.json').read_text())
        assert report['reused_episodes'] == 0 and report['closed']
        assert report['episodes'][0]['cache']['enabled'] is False
        assert len(list((tmp_path / 'second/attempts').iterdir())) == 2
        return
    if interrupted_second:
        # First repeat is retained; only the failed repeat starts a fresh world.
        assert calls == {'client': 2, 'world': 3, 'provider': 3, 'sandbox': 3}
    else:
        assert calls == before == {'client': 1, 'world': 1, 'provider': 1, 'sandbox': 1}
    before = dict(calls)
    assert cli.main(args + ['--out', str(tmp_path / 'second')]) == 0
    assert calls == before
    report = json.loads((tmp_path / 'second/summary.json').read_text())
    assert report['reused_episodes'] == repetitions and report['closed']


@pytest.mark.parametrize('reason', ['context_capacity', 'context_summary_failed', 'model_output_truncated'])
def test_context_capacity_failure_is_not_reusable(tmp_path, reason):
    cache = CompletedEpisodeCache(tmp_path)
    def blocked(path):
        result = complete(path, identity())
        result['stop_reason'] = reason
        (path / 'episode/result.json').write_text(json.dumps(result))
        return result
    _, first = cache.get_or_run(identity(), blocked)
    _, second = cache.get_or_run(identity(), lambda p: complete(p, identity()))
    assert not first['cacheable'] and not second['reused']
