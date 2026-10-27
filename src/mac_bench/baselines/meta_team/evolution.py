"""Sequential native dev reflection, domain version chains and frozen test pools."""
import argparse
import json
import shutil
from pathlib import Path

from mac_bench.baselines.common.config import ROOT, model_client, tree_hash, redact_configuration
from mac_bench.baselines.common.source import source_modules
from mac_bench.runtime.common.artifacts import write_json
from mac_bench.evaluation import run_episode, open_task
from mac_bench.evaluation.tasks.catalog import load_catalog
from mac_bench.evaluation.tasks.protocol import ENGINEERING
from mac_bench.runtime.agents.workflow import AgentBudget


def domain_for(record):
    return 'engineering' if record.category in ENGINEERING else 'operational'


def load_frozen(path, spec):
    path = Path(path).resolve()
    manifest = json.loads((path / 'manifest.json').read_text())
    if manifest.get('format') != 'meta_team.frozen_pools.v1' or manifest['source_revision'] != spec['source']['revision']:
        raise ValueError('Invalid frozen Meta-Team source provenance')
    pools = {}
    for domain, item in manifest['domains'].items():
        if domain not in spec['teams']:
            raise ValueError('Unknown frozen domain')
        initial = ROOT / spec['teams'][domain]['artifact']
        if item['initial_pool_hash'] != tree_hash(initial):
            raise ValueError('Frozen pool was evolved from different initial assets')
        pool = (path / item['pool']).resolve()
        if not pool.is_relative_to(path) or tree_hash(pool) != item['hash']:
            raise ValueError('Frozen Meta-Team pool hash/path mismatch')
        pools[domain] = {**item, 'path': str(pool)}
    return pools


class DevEvolution:
    """Only reflection artifacts persist; every case gets a fresh candidate world."""
    def __init__(self, output, *, config=None, resume=False):
        from .tasks import MetaTeam
        self.method = MetaTeam(config)
        self.method.validate()
        if self.method.pools:
            raise ValueError('Dev evolution starts from reviewed initial pools, not a test frozen_artifact')
        self.root = Path(output).resolve()
        self.config = config
        self.state_path = self.root / 'evolution.json'
        if self.root.exists():
            if not resume:
                raise FileExistsError(self.root)
            self.state = json.loads(self.state_path.read_text())
            if (self.state['source_revision'] != self.method.spec['source']['revision'] or
                    self.state['method_spec'] != redact_configuration(self.method.spec)):
                raise ValueError('Cannot resume with a different method configuration')
            for domain, item in self.state['domains'].items():
                if tree_hash(self.root / item['pool']) != item['hash']:
                    raise ValueError('Dev team version changed outside recorded evolution')
                if item['initial_pool_hash'] != tree_hash(ROOT / self.method.spec['teams'][domain]['artifact']):
                    raise ValueError('Initial pool changed since dev evolution')
        else:
            self.root.mkdir(parents=True)
            self.state = {'format': 'meta_team.dev_evolution.v1',
                'source_revision': self.method.spec['source']['revision'],
                'method_spec': redact_configuration(self.method.spec), 'domains': {}, 'cases': [], 'status': 'ready'}
            self._save()

    def _save(self):
        write_json(self.state, self.state_path)

    def _manager(self, domain):
        from .run_manager_bridge import init_run_at, load_run_at
        run_dir = self.root / 'domains' / domain
        if domain in self.state['domains']:
            return load_run_at(run_dir)
        initial = ROOT / self.method.spec['teams'][domain]['artifact']
        manager = init_run_at(run_dir, source_team_dir=initial, team_name=domain)
        version, pool = manager.get_latest_team_version()
        self.state['domains'][domain] = {'version': version, 'pool': str(pool.relative_to(self.root)),
            'hash': tree_hash(pool), 'initial_pool_hash': tree_hash(initial), 'dev_revisions': []}
        self._save()
        return manager

    def _promote(self, manager, runtime, method):
        """Use native persistence; also retain accepted L3 edits to existing roles.

        Upstream persistence copies evolution/skills/team files and new agents,
        but omits existing agents' config.yaml/prompt.md. Preserve those only
        when native apply_reflection accepted L3, without changing its decisions.
        """
        previous, prior_pool = manager.get_latest_team_version()
        applied = json.loads((runtime.parent / 'meta_team_result.json').read_text())['reflection_applied']
        version, changed = manager.persist_team_version(runtime, include_l3=applied)
        pool = manager.get_team_version_path(version)
        if applied:
            # Runtime model injection also changes config.yaml. Persist only
            # files named as accepted by native L3, not incidental bindings.
            accepted = set()
            for line in (runtime.parent.parent / 'events.jsonl').read_text().splitlines():
                event = json.loads(line)
                if event.get('event_type') == 'reflection.applied':
                    accepted.update(name for name in event['data']['files'] if ' (' not in name)
            replacements = [p for p in runtime.glob('*/*') if p.name in {'config.yaml', 'prompt.md'}
                and str(p.relative_to(runtime)) in accepted
                and p.is_file() and (not (pool / p.relative_to(runtime)).exists()
                    or (pool / p.relative_to(runtime)).read_bytes() != p.read_bytes())]
            if replacements and version == previous:
                version = f'v{int(previous[1:]) + 1:03d}'
                pool = manager.team_dir / version
                shutil.copytree(prior_pool, pool)
            for path in replacements:
                dest = pool / path.relative_to(runtime)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
                changed.append(str(path.relative_to(runtime)))
        try:
            method._validate_evolved(manager.run_dir.name, pool)
        except BaseException:
            if version != previous:
                manager.revert_team_version(version)
            raise
        return previous, version, pool, changed, applied

    def run_case(self, record, client, *, budget=None, task_factory=open_task,
                 retry_reason=None, execution_max_tokens=None):
        from .tasks import MetaTeam
        retry = None
        if retry_reason is not None:
            if not isinstance(retry_reason, str) or not retry_reason.strip():
                raise ValueError('A dev retry requires an explicit owner reason')
            if self.state['status'] != 'blocked' or not self.state['cases']:
                raise ValueError('Only the last blocked dev case may be retried')
            retry = self.state['cases'][-1]
            if retry['world_revision'] != record.world_revision or retry['promoted']:
                raise ValueError('Cannot retry a different or promoted dev case')
            result_path = self.root / retry['case'] / 'episode/result.json'
            prior_result = json.loads(result_path.read_text())
            if not prior_result.get('closed') or any(prior_result.get(k) for k in
                    ('finalization_error_type', 'close_error_type')):
                raise ValueError('Failed dev world must be graded and closed before retry')
        elif self.state['status'] != 'ready':
            raise ValueError('Inspect the failed or interrupted dev case before continuing evolution')
        if record.split != 'dev':
            raise ValueError('Only dev-split tasks may evolve a team')
        if retry is None and any(c['world_revision'] == record.world_revision for c in self.state['cases']):
            raise ValueError('Dev task already attempted in this version chain')
        domain = domain_for(record)
        source = ROOT / self.method.spec['source']['directory']
        with source_modules(source, ('core', 'tools', 'main', 'benchmarks')):
            manager = self._manager(domain)
            version, pool = manager.get_latest_team_version()
        item = self.state['domains'][domain]
        if version != item['version'] or tree_hash(pool) != item['hash']:
            raise ValueError('Uncommitted team version; inspect interrupted promotion before resume')
        method = MetaTeam(self.config, pools={domain: {**item, 'path': str(pool)}})
        if retry is not None and (retry['to_version'] != version or retry['from_version'] != version):
            raise ValueError('Failed dev retry must use the last committed pool')
        if execution_max_tokens is not None:
            if (isinstance(execution_max_tokens, bool) or not isinstance(execution_max_tokens, int)
                    or execution_max_tokens <= 0 or budget is None
                    or execution_max_tokens >= budget.max_tokens):
                raise ValueError('Dev execution allowance must leave reflection budget')
            method.spec['learning']['dev_execution_max_tokens'] = execution_max_tokens
        method.validate()
        index = len(self.state['cases']) + 1
        case_dir = self.root / 'cases' / f'{index:03d}_{record.task_id}'
        if case_dir.exists():
            raise ValueError('Interrupted case exists; inspect it before retrying')
        task = task_factory(record, case_dir / 'runtime')
        result = run_episode(task, method, client, output=case_dir / 'episode', budget=budget, phase='dev')
        row = {'task_id': record.task_id, 'world_revision': record.world_revision, 'domain': domain,
            'case': str(case_dir.relative_to(self.root)), 'from_version': version,
            'to_version': version, 'status': result['status'], 'promoted': False,
            'model': result['model'], 'budget': result.get('budget'),
            'execution_max_tokens': method.spec['learning'].get('dev_execution_max_tokens'),
            'method_provenance': result.get('method_provenance'),
            'ps': result.get('ps'), 'rr': result.get('rr'),
            'usage': {key: result.get(key) for key in
                ('model_calls', 'tool_calls', 'input_tokens', 'output_tokens', 'elapsed_seconds')}}
        row['usage']['total_tokens'] = result.get('input_tokens', 0) + result.get('output_tokens', 0)
        if retry is not None:
            from hashlib import sha256
            row['retry_of'] = retry['case']
            row['retry_reason'] = retry_reason
            row['prior_result_sha256'] = sha256(result_path.read_bytes()).hexdigest()
        self.state['cases'].append(row)
        # Attempted tasks count as dev data even when grading/reflection fails.
        item['dev_revisions'].append(record.world_revision)
        self.state['status'] = 'blocked'
        self._save()
        native = case_dir / 'episode/native'
        if (result['status'] != 'completed' or not (native / 'meta_team_result.json').is_file()
                or not (case_dir / 'episode/dev_feedback.json').is_file()):
            return result
        native_result = json.loads((native / 'meta_team_result.json').read_text())
        if native_result['metadata']['phase'] != 'terminated':
            return result
        with source_modules(source, ('core', 'tools', 'main', 'benchmarks')):
            manager = self._manager(domain)
            previous, version, pool, changed, applied = self._promote(manager, native / 'team', method)
            manager.write_changelog(index, record.task_id, previous, version, changed, applied)
        item.update(version=version, pool=str(pool.relative_to(self.root)), hash=tree_hash(pool))
        row.update(to_version=version, promoted=True, modified_files=changed, reflection_applied=applied)
        self.state['status'] = 'ready'
        self._save()
        return result

    def finish_case_promotion(self):
        """Commit completed native feedback after an owner-reviewed adapter repair.

        No world or model is reopened. The failed promotion and its artifacts
        remain in the version directory and case history.
        """
        from hashlib import sha256
        from .tasks import MetaTeam
        if self.state['status'] != 'blocked' or not self.state['cases']:
            raise ValueError('Only the last blocked promotion can be completed')
        row = self.state['cases'][-1]
        if row['promoted']:
            raise ValueError('Dev case is already promoted')
        episode = self.root / row['case'] / 'episode'
        result = json.loads((episode / 'result.json').read_text())
        native = episode / 'native'
        native_result = json.loads((native / 'meta_team_result.json').read_text())
        if (result['status'] != 'completed' or not result.get('closed')
                or not (episode / 'dev_feedback.json').is_file()
                or native_result['metadata']['phase'] != 'terminated'
                or any(result.get(k) for k in ('finalization_error_type', 'close_error_type'))):
            raise ValueError('Native feedback must be completed, graded and closed')
        domain = row['domain']
        item = self.state['domains'][domain]
        if row['from_version'] != item['version'] or tree_hash(self.root / item['pool']) != item['hash']:
            raise ValueError('Committed pool changed after the failed promotion')
        method = MetaTeam(self.config, pools={domain: {**item, 'path': str(self.root / item['pool'])}})
        source = ROOT / self.method.spec['source']['directory']
        with source_modules(source, ('core', 'tools', 'main', 'benchmarks')):
            manager = self._manager(domain)
            version, pool = manager.get_latest_team_version()
            if version != item['version'] or tree_hash(pool) != item['hash']:
                raise ValueError('Native version manager disagrees with committed pool')
            previous, version, pool, changed, applied = self._promote(manager, native / 'team', method)
            manager.write_changelog(len(self.state['cases']), row['task_id'], previous, version, changed, applied)
        item.update(version=version, pool=str(pool.relative_to(self.root)), hash=tree_hash(pool))
        row.update(to_version=version, promoted=True, modified_files=changed, reflection_applied=applied,
                   promotion_resumed_without_model_calls=True,
                   completed_feedback_sha256=sha256((native / 'meta_team_result.json').read_bytes()).hexdigest())
        self.state['status'] = 'ready'
        self._save()
        return row

    def freeze(self, destination):
        if self.state['status'] != 'ready' or not self.state['domains']:
            raise ValueError('Cannot freeze an empty or interrupted evolution run')
        destination = Path(destination).resolve()
        if destination.exists():
            raise FileExistsError(destination)
        for item in self.state['domains'].values():
            if tree_hash(self.root / item['pool']) != item['hash']:
                raise ValueError('Dev artifact changed before freezing')
        destination.mkdir(parents=True)
        domains = {}
        for domain, item in self.state['domains'].items():
            shutil.copytree(self.root / item['pool'], destination / domain)
            domains[domain] = {**item, 'pool': domain}
        write_json({'format': 'meta_team.frozen_pools.v1',
            'source_revision': self.state['source_revision'], 'domains': domains,
            'dev_cases': self.state['cases'], 'method_spec': self.state['method_spec']}, destination / 'manifest.json')
        return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--method-config', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--freeze', type=Path)
    parser.add_argument('--max-model-calls', type=int, default=160)
    parser.add_argument('--max-tool-calls', type=int, default=320)
    parser.add_argument('--max-tokens', type=int, default=1000000)
    parser.add_argument('--max-seconds', type=int, default=3600)
    args = parser.parse_args(argv)
    records = load_catalog(args.catalog)
    dev = [r for r in records if r.split == 'dev']
    if not dev:
        parser.error('Catalogue must contain explicit dev-split tasks')
    budget = AgentBudget(max_model_calls=args.max_model_calls, max_tool_calls=args.max_tool_calls,
        max_env_actions=args.max_tool_calls, max_tokens=args.max_tokens, max_wall_seconds=args.max_seconds)
    from mac_bench.evaluation.episodes.budget import EpisodeBudget
    EpisodeBudget(budget)
    evolution = DevEvolution(args.out, config=args.method_config, resume=args.resume)
    attempted = {c['world_revision'] for c in evolution.state['cases']}
    client = model_client(evolution.method)
    for record in dev:
        if record.world_revision in attempted:
            continue
        result = evolution.run_case(record, client, budget=budget)
        if result['status'] != 'completed' or evolution.state['status'] != 'ready':
            return 1
    if args.freeze:
        evolution.freeze(args.freeze)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
