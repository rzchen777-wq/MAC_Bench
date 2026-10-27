"""Inspect a public task or evaluate a configured method on either world family."""
import argparse
from pathlib import Path
import json
from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.runtime.common.artifacts import write_json
from .tasks import task_record, open_task, load_catalog, CATEGORIES
from .episodes.runner import run_episode
from .reports.metrics import summarize_episodes
from mac_bench.runtime.paths import artifact_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--world', type=Path)
    source.add_argument('--catalog', type=Path, help='Task catalogue JSON')
    parser.add_argument('--task-id', help='Task ID from the dataset or --catalog')
    parser.add_argument('--category', choices=sorted(CATEGORIES), help='Task category if absent from task metadata')
    parser.add_argument('--inspect', action='store_true', help='Print only public task data; no model call or sandbox allocation')
    parser.add_argument('--dry-run', action='store_true', help='Print the selected revision, config and budget without creating outputs or calling providers')
    parser.add_argument('--out', type=Path)
    reuse = parser.add_mutually_exclusive_group()
    reuse.add_argument('--reuse-completed', action='store_true',
                       help='Reuse matching completed episodes')
    reuse.add_argument('--no-cache', dest='reuse_completed', action='store_false',
                       help='Run fresh episodes without reading or publishing the result cache (default)')
    parser.set_defaults(reuse_completed=False)
    parser.add_argument('--cache-dir', type=Path,
                        default=None,
                        help='Persistent completed episodes; shared across experiment output directories')
    parser.add_argument('--cache-namespace', default='main',
                        help='Non-secret experiment/account isolation; keep fixed when resuming')
    parser.add_argument('--method', choices=['react_single','aorchestra','meta_team','magentic_one'], default='react_single')
    parser.add_argument('--method-config', type=Path)
    parser.add_argument('--repetitions', type=int)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--max-tool-calls', type=int)
    parser.add_argument('--max-tokens', type=int)
    parser.add_argument('--max-seconds', type=int)
    args = parser.parse_args(argv)
    requested_repetitions = args.repetitions
    if args.repetitions is None:
        args.repetitions = 2
    if not args.reuse_completed:
        import os
        os.environ['MAC_BENCH_LLM_CACHE'] = '0'
    if args.repetitions < 1:
        parser.error('repetitions must be positive')
    frozen_budget = {}
    if args.world:
        if args.task_id:
            parser.error('--task-id selects from a catalogue')
        record = task_record(args.world, category=args.category)
    else:
        from mac_bench.runtime.paths import repository_root
        if not args.task_id:
            parser.error('--task-id is required; use mac-bench tasks list')
        args.catalog = args.catalog or repository_root() / 'data' / 'tasks.json'
        record = load_catalog(args.catalog, task_ids=[args.task_id])[0]
        if args.category and args.category != record.category:
            parser.error('--category conflicts with catalogue')
    for field, previous_default in (('max_model_calls', 80), ('max_tool_calls', 160),
                                     ('max_tokens', 400000), ('max_seconds', 2400)):
        if getattr(args, field) is None:
            setattr(args, field, frozen_budget.get(field, previous_default))
    if args.cache_dir is None:
        args.cache_dir = artifact_root() / 'cache/completed_episodes'
    if args.dry_run:
        print(json.dumps({'task_id': record.task_id,
                          'world_revision': record.world_revision, 'category': record.category,
                          'split': record.split, 'world': str(record.bundle), 'method': args.method,
                          'method_config': str(args.method_config) if args.method_config else None,
                          'repetitions': args.repetitions,
                          'budget': {field: getattr(args, field) for field in
                                     ('max_tokens', 'max_model_calls', 'max_tool_calls', 'max_seconds')},
                          'out': str(args.out) if args.out else None,
                          'cache_dir': str(args.cache_dir), 'model_calls': 0, 'native_execution': False}, indent=2))
        return 0
    if args.inspect:
        task = open_task(record, artifact_root() / 'cache/inspection')
        print(json.dumps(task.view.as_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.out is None:
        parser.error('--out is required for evaluation')
    args.out = args.out.resolve()
    budget = AgentBudget(max_model_calls=args.max_model_calls, max_tool_calls=args.max_tool_calls,
        max_env_actions=args.max_tool_calls, max_tokens=args.max_tokens, max_wall_seconds=args.max_seconds)
    from .episodes.budget import EpisodeBudget
    EpisodeBudget(budget)  # Validate before creating output or allocating resources.
    from mac_bench.baselines.common.registry import create_method
    method = create_method(args.method, config=args.method_config)
    if hasattr(method, 'validate'):
        method.validate()
    from mac_bench.baselines.common.config import model_connection
    connection = model_connection(method)
    from .suites.cache import CompletedEpisodeCache, episode_identity
    cache = CompletedEpisodeCache(args.cache_dir) if args.reuse_completed else None
    identity = episode_identity(record, method, connection, budget, 1,
                                namespace=args.cache_namespace)
    identities = [{**identity, 'repetition': index + 1} for index in range(args.repetitions)]
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / 'experiment.json'
    manifest = {'format': 'collaboration.single_task_experiment.v1', 'episodes': identities,
                'reuse_completed': args.reuse_completed}
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        parser.error('--out belongs to a different configuration; choose a new output directory')
    if not manifest_path.exists() and any(args.out.iterdir()):
        parser.error('--out contains an unversioned previous run; choose a new output directory')
    write_json(manifest, manifest_path)
    report = {'world_revision': record.world_revision, 'task_id': record.task_id,
        'category': record.category, 'scenario_family': record.category if record.category not in
        {'platform_data','developer_release','product_application'} else None,
        'baseline': method.name, 'model': connection.model,
        'llm': {key:getattr(connection,key) for key in
                ('base_url','model','timeout_seconds','thinking_mode','max_retries')},
        'episodes': [],
        'closed': False, 'status': 'starting'}
    client = None

    def execute(attempt):
        nonlocal client
        # A cache hit never creates a provider client or a TaskHandle.
        if client is None:
            from mac_bench.baselines.common.config import model_client
            client = model_client(method)
        task = open_task(record, attempt / 'runtime')
        return run_episode(task, method, client, output=attempt / 'episode', budget=budget, close=True)

    write_json(report, args.out / 'summary.json')
    try:
        for index in range(args.repetitions):
            if cache is not None:
                result, reuse = cache.get_or_run(identities[index], execute)
            else:
                from uuid import uuid4
                attempt = args.out / 'attempts' / uuid4().hex
                attempt.mkdir(parents=True)
                write_json(identities[index], attempt / 'identity.json')
                result = execute(attempt)
                reuse = {'enabled': False, 'reused': False, 'cacheable': False,
                         'artifact_directory': str(attempt)}
            report['episodes'].append({**result, 'cache': reuse})
            write_json({'result': result, 'cache': reuse}, args.out / f'episode_{index+1}.json')
            report['metrics'] = summarize_episodes(report['episodes'])
            write_json(report, args.out / 'summary.json')
            if result['status'] != 'completed' or not result.get('closed'):
                break
        report['status'] = 'completed' if len(report['episodes']) == args.repetitions and all(
            e['status'] == 'completed' for e in report['episodes']) else 'error'
    except BaseException as exc:
        report.update(status='error', error_type=type(exc).__name__)
        raise
    finally:
        report['closed'] = bool(report['episodes']) and all(e.get('closed') for e in report['episodes'])
        report['reused_episodes'] = sum(e['cache']['reused'] for e in report['episodes'])
        write_json(report, args.out / 'summary.json')
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
