"""Method-neutral single-episode evaluator; errors retain final-state evidence."""
import json
import os
import time
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from threading import Lock, RLock
from uuid import uuid4
from datetime import datetime, timezone
from mac_bench.runtime.common.artifacts import write_json
from ..reports.metrics import evaluation_fields
from ..tasks.protocol import ENGINEERING
from .budget import EpisodeBudget, BudgetExceeded
from .protocol import EpisodeContext, DevFeedback
from .recording import RecordingClient


def run_episode(task, baseline, client, *, output, budget=None, close=True, raise_errors=False, phase='test'):
    if phase not in {'dev', 'test'} or (phase == 'dev' and task.record.split != 'dev'):
        raise ValueError('Dev evolution requires a dev-split task')
    # Native methods temporarily change cwd; recorder paths must remain stable.
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    if not isinstance(baseline.name, str) or not baseline.name:
        raise ValueError('A baseline name is required')
    started = time.monotonic()
    instance = task.view.task_id + '-' + uuid4().hex
    result = {'format': 'collaboration.episode.v2', 'task_id': task.view.task_id,
        'world_id': task.view.task_id, 'world_revision': task.view.world_revision,
        'scenario_family': task.record.category if task.record.category not in ENGINEERING else None,
        'category': task.record.category, 'split': task.record.split, 'phase': phase, 'baseline': baseline.name,
        'instance_id': instance, 'status': 'starting', 'closed': False,
        'model': getattr(getattr(client, 'config', None), 'model', None),
        'started_at_utc': datetime.now(timezone.utc).isoformat()}
    event_lock = Lock()
    active_agents, seen_agents = set(), set()
    peak_agents = 0
    agent_events = False
    control_calls = 0
    accounting = None
    begun = False
    error = None
    capture_lock = RLock()
    capture = {'attempted': False, 'score': None, 'error': None, 'feedback': None}

    def event(value):
        nonlocal peak_agents, agent_events, control_calls
        with event_lock:
            if value.get('kind') == 'method_control_call':
                control_calls += 1
            if value.get('kind') in {'agent_start', 'agent_end'}:
                agent_events = True
                agent_id = value['agent_id']
                seen_agents.add(agent_id)
                if value['kind'] == 'agent_start':
                    active_agents.add(agent_id)
                    peak_agents = max(peak_agents, len(active_agents))
                else:
                    active_agents.discard(agent_id)
            with (output / 'events.jsonl').open('a') as stream:
                stream.write(json.dumps({'elapsed_seconds': time.monotonic() - started,
                                         'timestamp_utc': datetime.now(timezone.utc).isoformat(), **value},
                                       ensure_ascii=False) + '\n')

    def save():
        write_json(result, output / 'result.json')

    def record_error(phase, exc):
        # Owner evidence only: never feed internal failures or credentials into
        # a solver's context. Preserve the actual cause before the sandbox closes.
        details = ''.join(traceback.format_exception(exc))
        for name, value in os.environ.items():
            if len(value) >= 8 and any(word in name.upper() for word in ('TOKEN', 'PASSWORD', 'SECRET', 'API_KEY')):
                details = details.replace(value, '[REDACTED]')
        with (output / 'owner_errors.jsonl').open('a') as stream:
            stream.write(json.dumps({'phase': phase, 'error_type': type(exc).__name__,
                                     'traceback': details}, ensure_ascii=False) + '\n')

    def finish_candidate():
        # The same frozen state supplies dev reflection and final experiment scores.
        with capture_lock:
            if not capture['attempted']:
                capture['attempted'] = True
                try:
                    task.end_episode()
                    if callable(getattr(task, 'observe_and_evaluate', None)):
                        final = task.observe_and_evaluate()
                        write_json(final['observation'], output / 'final.json')
                        capture['score'] = final['evaluation']
                    else:
                        write_json(task.observe(), output / 'final.json')
                        capture['score'] = task.evaluate()
                    write_json(capture['score'], output / 'evaluation.json')
                except BaseException as exc:
                    capture['error'] = exc
            if capture['error'] is not None:
                raise capture['error']
            return capture['score']

    def dev_feedback():
        with capture_lock:
            if capture['feedback'] is None:
                fields = evaluation_fields(finish_candidate())
                feedback = DevFeedback(**{k: fields[k] for k in
                    ('evaluation_status', 'task_success', 'ps', 'rr')})
                write_json(asdict(feedback), output / 'dev_feedback.json')
                event({'kind': 'dev_feedback', **asdict(feedback)})
                capture['feedback'] = feedback
            return capture['feedback']

    save()
    try:
        if hasattr(baseline, 'validate'):
            baseline.validate()
        if hasattr(baseline, 'validate_task'):
            baseline.validate_task(task.record, phase=phase)
        if not task.is_open:
            task.open()
        initial = task.assert_initial()
        result['initial_state_matches'] = initial['matches']
        write_json(initial['observation'] if 'observation' in initial else task.observe(), output / 'initial.json')
        accounting = EpisodeBudget(budget)
        result['budget'] = asdict(accounting.limits)
        gateway = RecordingClient(client, output / 'model_calls', budget=accounting)
        task.begin_episode(instance, admit_call=accounting.admit_tool, event=event)
        begun = True
        result['status'] = 'running'
        save()
        outcome = baseline.run(EpisodeContext(task.view, task.environment, gateway, accounting, event,
                                             output / 'native', phase,
                                             dev_feedback if phase == 'dev' else None,
                                             getattr(task, 'open_execution', None)))
        payload = asdict(outcome) if is_dataclass(outcome) else dict(outcome)
        write_json(payload, output / 'trajectory.json')
        stops = [row for row in payload.get('trace', []) if row.get('kind') == 'agent_stop']
        result['stop_reason'] = stops[-1]['stop_reason'] if stops else payload.get('stop_reason', 'finished')
    except BudgetExceeded as exc:
        result['stop_reason'] = 'budget:' + str(exc)
        event({'kind': 'budget_stop', 'reason': str(exc)})
    except BaseException as exc:
        error = exc
        record_error('execution_or_startup', exc)
        result.update(status='error', error_type=type(exc).__name__)
        event({'kind': 'experiment_error', 'error_type': type(exc).__name__})
    finally:
        if accounting is not None:
            accounting.finish()
        if begun:
            try:
                score = finish_candidate()
                fields = evaluation_fields(score)
                result.update(fields)
                if error is None:
                    result['status'] = 'completed' if fields['evaluation_status'] in {'passed', 'failed'} else 'evaluation_error'
                    if result.get('stop_reason') in {'context_capacity', 'context_summary_failed', 'model_output_truncated'}:
                        result['status'] = 'execution_error'
            except BaseException as exc:
                record_error('finalization', exc)
                result['finalization_error_type'] = type(exc).__name__
                if error is None:
                    error = exc
                    result['status'] = 'evaluation_error'
            try:
                task.export_receipts(output)
            except Exception as exc:
                record_error('receipt_export', exc)
                result['receipt_export_error'] = type(exc).__name__
        if accounting is not None:
            result.update(accounting.snapshot())
        if close:
            try:
                task.close()
                result['closed'] = True
            except BaseException as exc:
                record_error('cleanup', exc)
                result['close_error_type'] = type(exc).__name__
                if error is None:
                    error = exc
                    result['status'] = 'cleanup_error'
        result['agents_used'] = sorted(seen_agents) if agent_events else None
        result['active_agents_peak'] = peak_agents if agent_events else None
        result['agents_active_at_return'] = len(active_agents) if agent_events else None
        result['internal_control_calls'] = control_calls
        method_manifest = output / 'native/method.json'
        if method_manifest.exists():
            manifest = json.loads(method_manifest.read_text())
            result['method_provenance'] = {k: manifest[k] for k in
                ('method', 'variant', 'resolved_config_hash')}
        result['elapsed_seconds'] = time.monotonic() - started
        result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        save()
    if error is not None and (raise_errors or not isinstance(error, Exception)):
        raise error
    return result
