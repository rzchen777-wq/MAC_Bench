"""Native reflection across dev worlds, with sealed scoring and frozen test reuse."""
import json
from dataclasses import replace
from pathlib import Path
import pytest
import yaml
from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.evaluation import run_episode
from mac_bench.baselines.meta_team.tasks import MetaTeam
from mac_bench.baselines.meta_team.evolution import DevEvolution, load_frozen
from mac_bench.baselines.common.config import ROOT, tree_hash
from .support import handle, Script, response, operations

pytestmark = pytest.mark.baselines

PATCH = 'Dispatch assignments with explicit acceptance criteria and non-overlapping mutation scopes.'


class ReflectingClient(Script):
    def __init__(self, domain):
        super().__init__([])
        self.domain = domain
        self.steps = {}
        self.saw_feedback = False
        self.sealed_result = False

    def complete(self, messages, **options):
        self.calls.append((messages, options))
        tools = {t['function']['name'] for t in options['tools']}
        phase = 'l1' if 'update_prompt_patch' in tools else 'l2' if 'update_correlation' in tools else 'l3' if 'propose_reflection' in tools else 'execute'
        step = self.steps.get(phase, 0)
        self.steps[phase] = step + 1
        if phase == 'execute':
            if step == 0:
                return response(operations(self.domain))
            return response([('finalize_task', {'output': 'Both requested changes completed.'})])
        content = '\n'.join(str(m.content) for m in messages)
        assert 'SECRET_PRIVATE' not in content
        self.saw_feedback |= 'Dev evaluation: passed; PS=1.0; RR=1.0.' in content
        if phase == 'l1':
            if step == 0:
                name, args = operations(self.domain)[0]
                return response([(name, args)])  # Must not mutate after finalize.
            if step == 1:
                self.sealed_result = 'dev_candidate_sealed' in content
                return response([('update_prompt_patch', {'action': 'add', 'patch': PATCH})])
            return response([('skip_l1_reflection', {'reason': 'Recorded dispatch improvement.'})])
        if phase == 'l2':
            return response([('skip_l2_reflection', {'reason': 'No other members used in this scripted case.'})])
        return response([('skip_reflection', {'reason': 'Keep existing team structure.'})])


@pytest.mark.parametrize('domain', ['engineering', 'operational'])
def test_native_dev_evolution_carries_patch_and_freezes_for_test(tmp_path, domain):
    evolution = DevEvolution(tmp_path / 'dev')
    tasks = []
    owners = []
    for index in range(2):
        task, owner = handle(tmp_path / f'fixture{index}', domain, task_id=f'dev{index}')
        task.record = replace(task.record, split='dev')
        original = owner.grade_submission if domain == 'engineering' else owner.evaluate
        def grade(*args, _original=original):
            value = _original(*args)
            value['private_checker'] = 'SECRET_PRIVATE'
            return value
        if domain == 'engineering':
            owner.grade_submission = grade
        else:
            owner.evaluate = grade
        tasks.append(task)
        owners.append(owner)
        client = ReflectingClient(domain)
        result = evolution.run_case(task.record, client, task_factory=lambda *a, _task=task: _task,
            budget=AgentBudget(max_model_calls=25, max_tool_calls=20, max_tokens=10000, max_wall_seconds=30))
        assert result['status'] == 'completed' and result['ps'] == 1, result
        assert client.saw_feedback and client.sealed_result
        assert len(owner.writes) == 2 and owner.closed
        if index:
            assert PATCH in client.calls[0][0][0].content
        assert any(json.loads(p.read_text())['identity']['phase'] == 'l1_reflection'
            for p in (tmp_path / 'dev' / evolution.state['cases'][-1]['case'] / 'episode/model_calls').glob('*.json'))
    frozen = evolution.freeze(tmp_path / 'frozen')
    pools = load_frozen(frozen, evolution.method.spec)
    method = MetaTeam(pools=pools)
    method.validate()
    version = evolution.state['domains'][domain]['version']
    assert version != 'v000'
    assert PATCH in (Path(pools[domain]['path']) / ('planner' if domain == 'engineering' else 'chairman') / 'evolution/prompt_patches.md').read_text()
    with pytest.raises(ValueError, match='used for team dev'):
        method.validate_task(replace(tasks[0].record, split='test'))
    task, owner = handle(tmp_path / 'test_fixture', domain, task_id='held_out')
    client = Script([operations(domain), [('set_final_output', {'output': 'Changed'}), ('terminate', {})]])
    before = tree_hash(frozen)
    result = run_episode(task, method, client, output=tmp_path / 'test_episode',
        budget=AgentBudget(max_model_calls=10, max_tool_calls=10, max_tokens=10000, max_wall_seconds=30))
    assert result['status'] == 'completed' and result['ps'] == 1, result
    assert PATCH in client.calls[0][0][0].content
    assert 'finalize_task' not in {t['function']['name'] for t in client.calls[0][1]['tools']}
    assert tree_hash(frozen) == before and owner.closed
    assert not (tmp_path / 'test_episode/dev_feedback.json').exists()


def test_test_world_cannot_supply_dev_feedback(tmp_path):
    task, owner = handle(tmp_path, 'engineering')
    with pytest.raises(ValueError, match='dev-split'):
        run_episode(task, MetaTeam(), Script([]), output=tmp_path / 'episode', phase='dev')
    assert not task.is_open and not (tmp_path / 'episode').exists()


def test_dev_token_reserve_enters_native_reflection_and_promotes(tmp_path):
    task, owner = handle(tmp_path / 'fixture', 'engineering')
    task.record = replace(task.record, split='dev')
    spec = yaml.safe_load((ROOT / 'configs/baselines/meta_team.yaml').read_text())
    spec['learning']['dev_execution_max_tokens'] = 1
    config = tmp_path / 'reserved.yaml'
    config.write_text(yaml.safe_dump(spec))
    evolution = DevEvolution(tmp_path / 'dev', config=config)
    client = ReflectingClient('engineering')
    result = evolution.run_case(task.record, client, task_factory=lambda *a: task,
        budget=AgentBudget(max_model_calls=25, max_tool_calls=20, max_tokens=10000, max_wall_seconds=30))
    assert result['status'] == 'completed' and owner.closed, result
    assert evolution.state['status'] == 'ready' and evolution.state['cases'][0]['promoted']
    ep = tmp_path / 'dev' / evolution.state['cases'][0]['case'] / 'episode'
    events = [json.loads(line) for line in (ep / 'events.jsonl').read_text().splitlines()]
    assert any(e['kind'] == 'meta_team_dev_handover' for e in events)
    assert (ep / 'dev_feedback.json').exists()
    calls = [json.loads(p.read_text()) for p in (ep / 'model_calls').glob('*.json')]
    assert any(c['identity']['phase'] == 'l1_reflection' for c in calls)


def test_dev_error_does_not_promote_or_freeze(tmp_path):
    task, owner = handle(tmp_path / 'fixture', 'engineering')
    task.record = replace(task.record, split='dev')
    evolution = DevEvolution(tmp_path / 'dev')
    client = Script([operations('engineering'), RuntimeError('provider failure')])
    result = evolution.run_case(task.record, client, task_factory=lambda *a: task,
        budget=AgentBudget(max_model_calls=10, max_tool_calls=10, max_tokens=10000, max_wall_seconds=30))
    assert result['status'] == 'error' and owner.closed
    assert evolution.state['domains']['engineering']['version'] == 'v000'
    assert evolution.state['status'] == 'blocked'
    with pytest.raises(ValueError, match='interrupted'):
        evolution.freeze(tmp_path / 'frozen')


def test_failed_case_retry_keeps_committed_pool_and_attempt_evidence(tmp_path):
    evolution = DevEvolution(tmp_path / 'dev')
    budget = AgentBudget(max_model_calls=25, max_tool_calls=20,
                         max_tokens=10000, max_wall_seconds=30)
    first, _ = handle(tmp_path / 'first', 'engineering', task_id='first')
    first.record = replace(first.record, split='dev')
    evolution.run_case(first.record, ReflectingClient('engineering'), budget=budget,
                       task_factory=lambda *a: first)
    committed = dict(evolution.state['domains']['engineering'])
    failed, _ = handle(tmp_path / 'failed', 'engineering', task_id='failed')
    failed.record = replace(failed.record, split='dev')
    evolution.run_case(failed.record, Script([RuntimeError('transport failed')]),
                       budget=budget, task_factory=lambda *a: failed)
    prior = evolution.root / evolution.state['cases'][-1]['case'] / 'episode/result.json'
    original = prior.read_bytes()
    with pytest.raises(ValueError, match='different or promoted'):
        evolution.run_case(first.record, ReflectingClient('engineering'), budget=budget,
                           retry_reason='Cannot repeat a promoted case')
    retry, owner = handle(tmp_path / 'retry', 'engineering', task_id='failed')
    retry.record = failed.record
    result = evolution.run_case(retry.record, ReflectingClient('engineering'), budget=budget,
        task_factory=lambda *a: retry, retry_reason='Owner reviewed transport failure',
        execution_max_tokens=1)
    assert result['status'] == 'completed' and owner.closed
    assert evolution.state['status'] == 'ready'
    assert len(evolution.state['cases']) == 3
    row = evolution.state['cases'][-1]
    assert row['from_version'] == committed['version']
    assert row['retry_of'] == evolution.state['cases'][1]['case']
    assert row['execution_max_tokens'] == 1 and row['promoted']
    assert prior.read_bytes() == original
    frozen = evolution.freeze(tmp_path / 'frozen')
    assert len(json.loads((frozen / 'manifest.json').read_text())['dev_cases']) == 3


def test_frozen_pool_tampering_rejected(tmp_path):
    evolution = DevEvolution(tmp_path / 'dev')
    task, owner = handle(tmp_path / 'fixture', 'operational')
    task.record = replace(task.record, split='dev')
    result = evolution.run_case(task.record, ReflectingClient('operational'), task_factory=lambda *a: task,
        budget=AgentBudget(max_model_calls=25, max_tool_calls=20, max_tokens=10000, max_wall_seconds=30))
    assert result['status'] == 'completed'
    frozen = evolution.freeze(tmp_path / 'frozen')
    (frozen / 'operational/chairman/prompt.md').write_text('replacement')
    with pytest.raises(ValueError, match='hash/path mismatch'):
        load_frozen(frozen, evolution.method.spec)


class StructuralClient(ReflectingClient):
    def __init__(self, unsupported=False, communication=False):
        super().__init__('engineering')
        self.unsupported = unsupported
        self.communication = communication
        self.structural_steps = 0
        self.collaboration_steps = 0

    def complete(self, messages, **options):
        tools = {t['function']['name'] for t in options['tools']}
        if 'update_correlation' in tools:
            self.calls.append((messages, options))
            step = self.collaboration_steps
            self.collaboration_steps += 1
            if step == 0:
                return response([('update_teammate_profile', {'teammate_name': 'developer',
                    'profile': 'reliability: high\nstrengths:\n  - Executes bounded assignments with evidence'})])
            if step == 1:
                return response([('update_correlation', {'partner_name': 'developer',
                    'suggestion': 'Explicit acceptance criteria and disjoint mutation scopes clarify handovers.'})])
            return response([('skip_l2_reflection', {'reason': 'Recorded collaboration notes.'})])
        if 'propose_reflection' not in tools:
            return super().complete(messages, **options)
        self.calls.append((messages, options))
        step = self.structural_steps
        self.structural_steps += 1
        if step == 0:
            config = yaml.safe_load((ROOT / 'configs/baselines/teams/meta_team/engineering/reviewer/config.yaml').read_text())
            config['max_steps'] = 77
            if self.communication:
                config['tools'].append('send_message')
            if self.unsupported:
                config['tools'] = ['host_shell']
            return response([('propose_reflection', {'target_file': 'reviewer/config.yaml',
                'content': yaml.safe_dump(config), 'reason': 'Allocate more independent verification steps.'})])
        if step == 1:
            return response([('propose_reflection', {'target_file': 'reviewer/prompt.md',
                'content': 'Review delegated changes against their acceptance criteria.\n- Inspect the submitted implementation.\n- Report correctness concerns and verification evidence to the planner.',
                'reason': 'Clarify independent review handover.'})])
        return response([('apply_reflection', {'confirmation': 'apply'})])


def test_config_only_native_l3_role_can_be_promoted_and_frozen(tmp_path):
    class NewRoleClient(ReflectingClient):
        def complete(self, messages, **options):
            tools = {t['function']['name'] for t in options['tools']}
            if 'propose_reflection' not in tools:
                return super().complete(messages, **options)
            step = self.steps.get('new_role', 0)
            self.steps['new_role'] = step + 1
            if step == 0:
                config = {'name': 'data-steward', 'description': 'Extract bounded evidence',
                          'model': 'scripted', 'max_steps': 20,
                          'tools': ['terminal', 'send_message']}
                return response([('propose_reflection', {
                    'target_file': 'data-steward/config.yaml', 'content': yaml.safe_dump(config),
                    'reason': 'Add a bounded evidence extraction role.'})])
            return response([('apply_reflection', {'confirmation': 'apply'})])
    task, owner = handle(tmp_path / 'fixture', 'engineering')
    task.record = replace(task.record, split='dev')
    evolution = DevEvolution(tmp_path / 'dev')
    result = evolution.run_case(task.record, NewRoleClient('engineering'),
        task_factory=lambda *a: task,
        budget=AgentBudget(max_model_calls=25, max_tool_calls=20, max_tokens=10000, max_wall_seconds=30))
    assert result['status'] == 'completed' and owner.closed
    frozen = evolution.freeze(tmp_path / 'frozen')
    pools = load_frozen(frozen, evolution.method.spec)
    new_role = Path(pools['engineering']['path']) / 'data-steward'
    assert (new_role / 'config.yaml').exists() and not (new_role / 'prompt.md').exists()
    MetaTeam(pools=pools).validate()


def test_completed_native_promotion_can_resume_without_model_or_world(tmp_path, monkeypatch):
    task, owner = handle(tmp_path / 'fixture', 'engineering')
    task.record = replace(task.record, split='dev')
    evolution = DevEvolution(tmp_path / 'dev')
    original = MetaTeam._validate_evolved
    def reject_promotion(self, domain, pool):
        config = yaml.safe_load((pool / 'reviewer/config.yaml').read_text())
        if 'send_message' in config['tools']:
            raise ValueError('Injected promotion validation failure')
        return original(self, domain, pool)
    monkeypatch.setattr(MetaTeam, '_validate_evolved', reject_promotion)
    client = StructuralClient(communication=True)
    with pytest.raises(ValueError, match='Injected promotion'):
        evolution.run_case(task.record, client, task_factory=lambda *a: task,
            budget=AgentBudget(max_model_calls=25, max_tool_calls=20, max_tokens=10000, max_wall_seconds=30))
    assert owner.closed and evolution.state['status'] == 'blocked'
    calls, writes = len(client.calls), list(owner.writes)
    case = evolution.root / evolution.state['cases'][-1]['case'] / 'episode'
    evidence = {name: (case / name).read_bytes() for name in
                ['result.json', 'evaluation.json', 'native/meta_team_result.json']}
    monkeypatch.setattr(MetaTeam, '_validate_evolved', original)
    row = evolution.finish_case_promotion()
    assert row['promoted'] and row['promotion_resumed_without_model_calls']
    assert evolution.state['status'] == 'ready'
    assert len(client.calls) == calls and owner.writes == writes
    assert all((case / name).read_bytes() == content for name, content in evidence.items())
    frozen = evolution.freeze(tmp_path / 'frozen')
    pools = load_frozen(frozen, evolution.method.spec)
    MetaTeam(pools=pools).validate()


def test_native_l2_and_accepted_l3_changes_survive_next_version(tmp_path):
    task, owner = handle(tmp_path / 'fixture', 'engineering')
    task.record = replace(task.record, split='dev')
    evolution = DevEvolution(tmp_path / 'dev')
    count = [0]
    original = owner.grade_submission
    def grade(*args):
        count[0] += 1
        return original(*args)
    owner.grade_submission = grade
    result = evolution.run_case(task.record, StructuralClient(), task_factory=lambda *a: task,
        budget=AgentBudget(max_model_calls=30, max_tool_calls=20, max_tokens=10000, max_wall_seconds=30))
    assert result['status'] == 'completed' and count == [1], result
    item = evolution.state['domains']['engineering']
    pool = evolution.root / item['pool']
    assert yaml.safe_load((pool / 'reviewer/config.yaml').read_text())['max_steps'] == 77
    assert yaml.safe_load((pool / 'planner/config.yaml').read_text())['model'] == 'experiment.model'
    assert yaml.safe_load((pool / 'developer/config.yaml').read_text())['model'] == 'experiment.model'
    assert 'acceptance criteria' in (pool / 'reviewer/prompt.md').read_text()
    assert 'reliability: high' in (pool / 'planner/evolution/teammate_profiles.yaml').read_text()
    assert (pool / 'planner/evolution/correlations/developer.md').exists()
    assert evolution.state['cases'][0]['reflection_applied']
    resumed = DevEvolution(evolution.root, resume=True)
    assert resumed.state['domains']['engineering']['hash'] == tree_hash(pool)
    frozen = evolution.freeze(tmp_path / 'frozen')
    config = dict(evolution.method.spec)
    config.pop('_protocol'); config.pop('_spec_path')
    config['learning'] = {**config['learning'], 'frozen_artifact': str(frozen)}
    cfg_path = tmp_path / 'method.yaml'
    cfg_path.write_text(yaml.safe_dump(config))
    selected = MetaTeam(cfg_path)
    selected.validate()
    assert selected._pool('engineering') == frozen / 'engineering'
    with pytest.raises(ValueError, match='no pool for domain'):
        selected._pool('operational')


def test_unsupported_l3_tool_is_not_promoted(tmp_path):
    task, owner = handle(tmp_path / 'fixture', 'engineering')
    task.record = replace(task.record, split='dev')
    evolution = DevEvolution(tmp_path / 'dev')
    with pytest.raises(ValueError, match='unsupported environment tools'):
        evolution.run_case(task.record, StructuralClient(unsupported=True), task_factory=lambda *a: task,
            budget=AgentBudget(max_model_calls=30, max_tool_calls=20, max_tokens=10000, max_wall_seconds=30))
    assert owner.closed and evolution.state['status'] == 'blocked'
    assert evolution.state['domains']['engineering']['version'] == 'v000'
