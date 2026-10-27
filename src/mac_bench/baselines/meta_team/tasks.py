"""Official Meta-Team Runner, independent agent loops and messaging on domain pools."""
from mac_bench.runtime.paths import evidence_path
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
from copy import deepcopy
from contextlib import contextmanager
from mac_bench.baselines.common.config import ROOT, load_spec, resolve, tree_hash
from mac_bench.baselines.common.source import source_modules
from mac_bench.baselines.common.gateway import Gateway, MethodStop, agent_scope
from mac_bench.evaluation.episodes.identity import call_scope

PUBLIC_RESOURCE_TOOLS = {'list_resources','read_resource','query_resource',
                         'search_resource','query_result','compute'}
NATIVE_COMMUNICATION_TOOLS = frozenset({'send_message', 'wait_for_replies'})


def _propagate_completion_stop(stop):
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    # The native agent loop owns malformed model output. Keep infrastructure
    # and budget failures outside its broad Exception handler.
    if isinstance(stop.error, ModelOutputTruncatedError) and stop.error.response is not None:
        raise stop.error
    raise stop


def _install_native_lifecycle_status(runner, Agent):
    """Make native messaging reflect task liveness; never restart a worker."""
    from tools import messaging

    original_states = runner.get_agent_states
    original_stop = runner.stop_agent
    original_check = messaging.execute_check_agent_status
    original_wait = Agent._handle_wait_for_replies
    original_execute = Agent._execute_tool_call

    def states():
        result = original_states()
        for name, info in result.items():
            task = runner._agent_tasks.get(name)
            if task is not None and task.done():
                agent = runner.agents[name]
                info.update(state='stopped',
                    state_since=getattr(agent, '_loop_finished_at', info['state_since']),
                    stop_reason=getattr(agent, '_loop_stop_reason',
                                        'cancelled' if task.cancelled() else 'loop_ended'))
        return result

    def stop(name):
        task = runner._agent_tasks.get(name)
        if task is None or not task.done():
            return original_stop(name)
        # Same native removal/accounting as firing a live worker, but a
        # completed task has no cancellation to await. Recruitment is explicit.
        agent = runner.agents.get(name)
        if agent:
            runner._stopped_agents_cost += getattr(agent, '_total_cost_usd', 0.0)
        runner.message_store.unregister(name)
        runner.agents.pop(name, None)
        runner._agent_tasks.pop(name, None)
        return True

    def check(agent_name=None, *, _agent_name, _message_store, _agent_states):
        names = [agent_name] if agent_name else [n for n in _agent_states if n != _agent_name]
        stopped = [n for n in names if _agent_states.get(n, {}).get('state') == 'stopped']
        if not stopped:
            return original_check(agent_name, _agent_name=_agent_name,
                _message_store=_message_store, _agent_states=_agent_states)
        live = {n: s for n, s in _agent_states.items() if s['state'] != 'stopped'}
        result = '' if agent_name in stopped else original_check(agent_name,
            _agent_name=_agent_name, _message_store=_message_store, _agent_states=live)
        lines = []
        for name in stopped:
            info = _agent_states[name]
            messages = _message_store.count_from(sender=_agent_name, receiver=name)
            replies = _message_store.count_from(sender=name, receiver=_agent_name)
            lines.append(f"- {name}: STOPPED ({info['stop_reason']})\n"
                f"  Your messages: {messages['total']} sent, {messages['unread']} unread; "
                f"unread replies: {replies['unread']}.")
        return result + '\nStopped agent loops:\n' + '\n'.join(lines) + (
            '\nThese loops have ended and cannot read new messages or produce new replies. '
            'Already queued replies remain available. No worker was automatically restarted.')

    async def wait(agent, args):
        requested = args.get('from_agents')
        current = runner.get_agent_states()
        if requested and all(current.get(n, {}).get('state') == 'stopped' for n in requested):
            # Preserve delivery of every pending message, including other agents,
            # without spending a timeout waiting for execution that has ended.
            result = messaging.execute_read_messages(sender=None, unread_only=True, limit=20,
                _agent_name=agent.config.name, _message_store=agent.message_store)
            return result + '\nRequested agent loops have stopped: ' + ', '.join(requested) + (
                '. They cannot produce further replies; no worker was automatically restarted.')
        return await original_wait(agent, args)

    async def execute(agent, name, args, cwd=None):
        # Keep native authorization, validation and actions. Correct only its
        # membership-based "running/ACTIVE" descriptions of completed loops.
        result = await original_execute(agent, name, args, cwd)
        if name == 'start_agent':
            member = str(args.get('name', '')).strip()
            info = runner.get_agent_states().get(member, {})
            if info.get('state') == 'stopped' and result == f"Agent '{member}' is already running.":
                return (f"Agent '{member}' is already recruited, but its execution loop has stopped "
                    f"({info['stop_reason']}). Use stop_agent(name=\"{member}\") to release its slot, "
                    f"then start_agent(name=\"{member}\") if you want a new execution loop. "
                    'No worker was automatically restarted.')
        elif name == 'list_pool':
            for member, info in runner.get_agent_states().items():
                if info['state'] == 'stopped':
                    result = result.replace(f"- **{member}** [🟢 ACTIVE]",
                        f"- **{member}** [STOPPED — recruited; use stop_agent to release]")
        return result

    runner.get_agent_states = states
    runner.stop_agent = stop
    messaging.execute_check_agent_status = check
    Agent._handle_wait_for_replies = wait
    Agent._execute_tool_call = execute


@contextmanager
def native_environment():
    # Upstream's import-time provider initialization is unused after the bridge.
    # Confine its global env/litellm side effects to this serialized method scope.
    before = dict(os.environ)
    import litellm
    previous = {k:getattr(litellm,k,None) for k in ('set_verbose','drop_params')}
    os.environ['LLM_PROVIDER']='dev'
    os.environ['DEV_API_BASE']='http://unused.invalid'
    os.environ['DEV_API_KEY']='unused-recorded-gateway'
    # Upstream accepts a process-wide model override. The episode configuration
    # is authoritative here, including for recruited agents.
    os.environ.pop('META_TEAM_MODEL', None)
    try:
        yield
    finally:
        os.environ.clear();os.environ.update(before)
        for k,v in previous.items():setattr(litellm,k,v)


class MetaTeam:
    name = 'meta_team'

    def __init__(self, config=None, *, pools=None):
        self.spec = load_spec(self.name,config)
        self.pools = pools or {}
        frozen = self.spec['learning'].get('frozen_artifact')
        self.frozen = bool(frozen)
        if frozen and pools is not None:
            raise ValueError('Choose a frozen artifact or dev pools, not both')
        if frozen:
            from .evolution import load_frozen
            self.pools = load_frozen(ROOT / frozen, self.spec)

    def _pool(self, domain):
        if self.frozen and domain not in self.pools:
            raise ValueError('Frozen artifact has no pool for domain: ' + domain)
        return Path(self.pools[domain]['path']) if domain in self.pools else ROOT / self.spec['teams'][domain]['artifact']

    def validate_task(self, record, *, phase='test'):
        from .evolution import domain_for
        domain = domain_for(record)
        self._pool(domain)
        if phase == 'test' and record.world_revision in self.pools.get(domain, {}).get('dev_revisions', []):
            raise ValueError('Cannot test on a world used for team dev evolution')

    def _validate_evolved(self, domain, pool):
        import yaml
        config = yaml.safe_load((pool / 'pool.yaml').read_text())
        members = list(pool.glob('*/config.yaml'))
        names = {p.parent.name for p in members}
        if config['chairman'] not in names or not names:
            raise ValueError('Evolved pool requires a valid chairman')
        allowed = set(self.spec['teams'][domain]['environment_tools']) | NATIVE_COMMUNICATION_TOOLS
        if domain == 'operational':
            allowed |= PUBLIC_RESOURCE_TOOLS
        for path in members:
            member = yaml.safe_load(path.read_text())
            if member['name'] != path.parent.name or not set(member.get('tools', [])).issubset(allowed):
                raise ValueError('Evolved pool requests unsupported environment tools: ' + str(path))
            if not isinstance(member.get('max_steps'), int) or member['max_steps'] <= 0:
                raise ValueError('Invalid evolved member step limit')
            # Native Agent.load_prompt explicitly accepts an absent prompt.md.
            # New L3 roles may be specified by config alone; retain that native
            # behavior rather than inventing a prompt or rejecting recruitment.
            prompt = path.parent / 'prompt.md'
            if prompt.exists():
                prompt.read_text()
        if config['settings']['max_messages'] <= 0 or config['settings'].get('idle_timeout', 120) <= 0:
            raise ValueError('Invalid evolved pool limits')
        (pool / 'constitution.md').read_text()

    def validate(self):
        import yaml
        if self.spec['learning']['test_enable_reflection'] is not False:
            raise ValueError('Test pools must remain frozen; test reflection is disabled')
        for key in ('dev_execution_max_seconds', 'dev_reflection_phase_timeout',
                    'dev_execution_max_tokens'):
            value = self.spec['learning'].get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
                raise ValueError('Invalid dev time limit: ' + key)
        for domain, team in self.spec['teams'].items():
            pool = ROOT / team['artifact']
            config = yaml.safe_load((pool / 'pool.yaml').read_text())
            expected = {member['name'] for member in team['members']}
            actual = {path.parent.name for path in pool.glob('*/config.yaml')}
            if actual != expected or config['chairman'] != team['chairman']:
                raise ValueError('Domain pool composition differs from specification: ' + domain)
            (pool / 'constitution.md').read_text()
            manifest = yaml.safe_load((pool / 'upstream_adaptation.yaml').read_text())
            for asset in manifest['assets']:
                for path, expected_hash in (
                    (evidence_path(asset['source']), asset['source_sha256']),
                    (pool / asset['path'], asset['adapted_sha256']),
                ):
                    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                        raise ValueError('Meta-Team upstream adaptation drift: ' + str(path))
            for path in pool.glob('*/config.yaml'):
                member = yaml.safe_load(path.read_text())
                if member['name'] != path.parent.name or set(member['tools']) != set(team['environment_tools']):
                    raise ValueError('Domain member tool contract differs from specification: ' + str(path))
                (path.parent / 'prompt.md').read_text()
            settings = config['settings']
            if settings['max_messages'] <= 0 or settings.get('idle_timeout', 120) <= 0:
                raise ValueError('Domain pool limits must be positive')
        for domain, artifact in self.pools.items():
            if domain not in self.spec['teams'] or tree_hash(artifact['path']) != artifact['hash']:
                raise ValueError('Evolved Meta-Team pool differs from recorded artifact')
            self._validate_evolved(domain, Path(artifact['path']))

    def run(self, context):
        domain = 'engineering' if {t.name for t in context.task.tools}=={'terminal'} else 'operational'
        if context.phase == 'dev' and context.dev_feedback is None:
            raise ValueError('Dev reflection requires evaluator-owned feedback')
        if context.phase == 'dev' and self.spec['learning']['dev_enable_reflection'] is not True:
            raise ValueError('Enable dev reflection in the method configuration')
        pool = self._pool(domain)
        if not pool.is_dir():raise ValueError('Selected domain pool is missing')
        fingerprint=tree_hash(pool)
        recorded_spec = deepcopy(self.spec)
        if context.phase == 'dev':
            recorded_spec['variant'] = 'native_dev_evolution'
        elif domain in self.pools:
            recorded_spec['variant'] = 'native_dev_evolved_frozen_test'
        if domain in self.pools:
            recorded_spec['artifacts']['dev_evolved_pools'] = 'selected_hash_verified_version'
        directory=resolve(recorded_spec,context,assets={'domain':domain,'pool_hash':fingerprint,
            'adaptation': 'upstream_pool_native_dev_evolved' if domain in self.pools else self.spec['teams'][domain]['adaptation'],
            'runtime_phase':context.phase,'enable_reflection':context.phase == 'dev',
            'evolved_pool': self.pools.get(domain)})
        destination=directory/'team';shutil.copytree(pool,destination)
        try:
            with source_modules(ROOT/self.spec['source']['directory'],('core','tools','main','benchmarks')):
                with native_environment():
                    return asyncio.run(self._run(context,directory,destination))
        except MethodStop as stop:
            raise stop.error
        finally:
            if tree_hash(pool)!=fingerprint:
                raise RuntimeError('Frozen source pool was modified')

    async def _run(self, context,directory,pool):
        import yaml
        from core import llm
        from core.agent import Agent
        from core.runner import Runner
        from core.tool_registry import ToolRegistry, Tool
        from core.session import Session
        from mac_bench.evaluation.episodes.identity import call_identity
        gateway=Gateway(context,self.spec['model_parameters'])
        from mac_bench.runtime.agents.context import ContextPolicy
        gateway.context_policy = ContextPolicy(**self.spec.get('context_policy', {}))
        gateway.native_contexts = {}
        config=yaml.safe_load((pool/'pool.yaml').read_text())
        domain='engineering' if {t.name for t in context.task.tools}=={'terminal'} else 'operational'
        member_limits={m['name']:m['max_steps'] for m in self.spec['teams'][domain]['members']}
        for p in pool.glob('*/config.yaml'):
            member=yaml.safe_load(p.read_text())
            member.update(model=context.client.config.model, **self.spec['model_parameters'])
            # Frozen pools describe domain tools. The task layer also exposes
            # resource/query tools; every member needs the public task materials.
            available = {definition.name for definition in context.task.tools}
            allowed = set(member['tools']) | PUBLIC_RESOURCE_TOOLS
            member['tools'] = [definition.name for definition in context.task.tools
                               if definition.name in allowed & available]
            if domain not in self.pools:
                member['max_steps'] = member_limits[member['name']]
            p.write_text(yaml.safe_dump(member,sort_keys=False))
        access_lock = asyncio.Lock()
        world_closed = False
        registry=ToolRegistry(str(directory/'no_host_tools'))
        for definition in context.task.tools:
            async def execute(_definition=definition, **arguments):
                arguments.pop('cwd',None)
                async with access_lock:
                    if world_closed:
                        return json.dumps({'error': {'code': 'dev_candidate_sealed',
                            'message': 'Task execution ended. Reflect on the frozen submission and feedback; application tools are closed.'}})
                    return json.dumps(await gateway.tool(_definition.name,arguments),ensure_ascii=False)
            registry._tools[definition.name]=Tool(definition.name,
                definition.as_function()['function'],execute)
        settings=config['settings']
        total_seconds = context.budget.limits.max_wall_seconds or 2400
        execution_seconds = total_seconds
        reflection_timeout = settings.get('reflection_phase_timeout', 0)
        if context.phase == 'dev':
            execution_seconds = min(total_seconds,
                self.spec['learning'].get('dev_execution_max_seconds') or total_seconds)
            reflection_timeout = self.spec['learning'].get('dev_reflection_phase_timeout') or reflection_timeout
        execution_token_limit = self.spec['learning'].get('dev_execution_max_tokens') if context.phase == 'dev' else None
        if execution_token_limit is not None and execution_token_limit >= context.budget.limits.max_tokens:
            raise ValueError('Dev execution token limit must leave room for reflection')
        runner=Runner(pool_dir=pool,chairman_name=config['chairman'],
            constitution=(pool/'constitution.md').read_text(),tool_registry=registry,
            max_seconds=execution_seconds,
            max_messages=settings['max_messages'],idle_timeout=settings.get('idle_timeout', 120),
            enable_reflection=context.phase == 'dev',
            reflection_phase_timeout=reflection_timeout)
        _install_native_lifecycle_status(runner, Agent)
        async def complete(model, messages, **kwargs):
            phase = 'execution' if runner._phase == 'task_execution' else runner._phase
            with call_scope(phase=phase):
                try:
                    response = await gateway.native_complete(model, messages, **kwargs)
                except MethodStop as stop:
                    _propagate_completion_stop(stop)
            # Same handover path as native max_messages/max_cost guards. Reserve
            # part of the shared dev budget; never grant unrecorded extra tokens.
            if execution_token_limit is not None and runner._phase == 'task_execution' and not runner._forced_termination:
                usage = context.budget.snapshot()
                consumed = usage['input_tokens'] + usage['output_tokens']
                if consumed >= execution_token_limit:
                    runner._forced_termination = f'dev_execution_tokens ({consumed} >= {execution_token_limit})'
                    context.event({'kind': 'meta_team_dev_handover', 'reason': 'execution_token_limit',
                                   'consumed_tokens': consumed, 'execution_token_limit': execution_token_limit})
                    runner._done.set()
            return response
        llm.complete = complete
        if context.phase == 'dev':
            from core.types import TaskValidation
            async def validator(output):
                nonlocal world_closed
                async with access_lock:
                    world_closed = True
                    feedback = await asyncio.to_thread(context.dev_feedback)
                return TaskValidation(success=feedback.task_success is True,
                    summary=f'Dev evaluation: {feedback.evaluation_status}; PS={feedback.ps}; RR={feedback.rr}.',
                    details='The candidate is sealed. No private rules, tests or reference implementation are exposed.')
            def forced_capture():
                nonlocal world_closed
                world_closed = True
                feedback = context.dev_feedback()
                runner._task_validation = TaskValidation(success=feedback.task_success is True,
                    summary=f'Dev evaluation: {feedback.evaluation_status}; PS={feedback.ps}; RR={feedback.rr}.')
            runner.set_task_validator(validator)
            runner.set_pre_reflection_hook(forced_capture)
        error=None
        original=Agent.run_loop
        async def tracked(agent,*args,**kwargs):
            nonlocal error
            native_result = None
            loop_reason = 'loop_ended'
            with agent_scope(context,agent.config.name,'chairman' if agent.config.name==config['chairman'] else 'worker'):
                try:
                    native_result = await original(agent,*args,**kwargs)
                    loop_reason = native_result.metadata.get('stopped') or 'loop_ended'
                    return native_result
                except MethodStop as stop:
                    loop_reason = 'shared_gateway_stop'
                    error=stop
                    runner.message_store.terminate(reason='shared_gateway_stop')
                    runner._done.set()
                    raise asyncio.CancelledError()
                except asyncio.CancelledError:
                    loop_reason = 'cancelled'
                    raise
                finally:
                    import time
                    agent._loop_finished_at = time.time()
                    agent._loop_stop_reason = loop_reason
        Agent.run_loop=tracked
        session=Session.create(context.task.task_id,session_dir=directory/'session')
        native_event=session.event_log
        class Events:
            def log(self,event_type,agent=None,data=None):
                native_event.log(event_type,agent=agent,data=data)
                context.event({'kind':'meta_team_event','event_type':event_type,'agent_id':agent,
                    'data':data or {}})
                if event_type=='tool.call':
                    tool=(data or {}).get('tool')
                    if tool not in {t.name for t in context.task.tools}:
                        context.event({'kind':'method_control_call','tool':tool,**call_identity()})
            def close(self):native_event.close()
        session.event_log=Events()
        try:
            instruction = context.task.instruction+'\n\n'+context.task.interaction_instructions
            if context.phase == 'dev':
                instruction += '\n\nDev protocol: finalize_task seals the candidate and supplies aggregate evaluation feedback before native reflection. During reflection, environment tools are closed. Evolve only using the published environment tool names; private grading rules and answers are unavailable.'
            result=await runner.run(instruction,session=session)
            if error:raise error
            payload={'output':result.output,'agents':sorted(result.agents),
                     'cost_seconds':result.cost_seconds,'reflection_applied':result.reflection_applied,
                     'metadata':result.metadata}
            (directory/'meta_team_result.json').write_text(json.dumps(payload,indent=2,default=str))
            return {'stop_reason':result.metadata.get('forced_termination') or 'finished',
                    'trace':[],'native_trace':'native/session/events.jsonl','output':result.output}
        finally:
            # Original Runner cleanup is not guaranteed if its task fails early.
            from tools.primitives import cleanup_handlers
            runner.message_store.terminate(reason='episode_closed')
            remaining=[t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in remaining:task.cancel()
            if remaining:await asyncio.gather(*remaining,return_exceptions=True)
            cleanup_handlers(runner)
            session.event_log.close()
