"""Native loop termination, message delivery and explicit recruitment semantics."""
from mac_bench.runtime.paths import repository_root
import asyncio
from pathlib import Path

import pytest

from mac_bench.baselines.common.source import source_modules
from mac_bench.baselines.meta_team.tasks import native_environment, _install_native_lifecycle_status

pytestmark = pytest.mark.baselines


@pytest.mark.parametrize('stop', ['max_steps', 'model_error', 'reflection_wait'])
def test_native_worker_liveness_and_messaging(tmp_path, monkeypatch, stop):
    root = repository_root()/'.cache/baselines/Meta-Team'
    with source_modules(root, ('core', 'tools', 'main', 'benchmarks')):
        with native_environment():
            from core import llm
            from core.agent import Agent
            from core.runner import Runner
            from core.tool_registry import ToolRegistry, Tool
            from tools.primitives import build_primitive_tools, cleanup_handlers
            from litellm import ModelResponse
            calls, writes = [], []
            async def complete(*args, **kwargs):
                calls.append(kwargs)
                if stop == 'model_error':
                    raise RuntimeError('bounded native model error')
                return ModelResponse(model='gpt-4o-mini', choices=[{'finish_reason': 'tool_calls',
                    'message': {'role': 'assistant', 'content': '', 'tool_calls': [{
                        'id': 'native-one', 'type': 'function', 'function': {
                            'name': 'terminal', 'arguments': '{"command":"one native write"}'}}]}}],
                    usage={'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5})
            monkeypatch.setattr(llm, 'complete', complete)
            async def terminal(command):
                writes.append(command)
                return 'written'
            registry = ToolRegistry(str(tmp_path/'empty_registry'))
            registry._tools['terminal'] = Tool('terminal', {'name': 'terminal',
                'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}},
                               'required': ['command']}}, terminal)

            async def run():
                runner = Runner(root/'agents/pool_SWE_Pro', 'planner', tool_registry=registry,
                                idle_timeout=.01, enable_reflection=stop=='reflection_wait')
                leader = runner.load_agent_from_pool('planner')
                leader.bind_message_store(runner.message_store)
                leader._runner_ref = runner
                runner.agents['planner'] = leader
                worker = runner.load_agent_from_pool('developer')
                worker.config.max_steps = 1
                worker.config.tools = ['terminal']
                runner.start_agent(worker)
                runner.message_store.send('planner', 'developer', 'Do one write.')
                task = runner._agent_tasks['developer']
                if stop=='reflection_wait':
                    for _ in range(100):
                        if writes and worker._state=='idle':
                            break
                        await asyncio.sleep(.005)
                    assert writes == ['one native write'] and not task.done()
                    _install_native_lifecycle_status(runner, Agent)
                    assert runner.get_agent_states()['developer']['state']=='idle'
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    assert runner.get_agent_states()['developer']['state']=='stopped'
                    return
                outcome = await asyncio.wait_for(task, 3)
                # Reproduce upstream's false WORKING report after real loop exit.
                assert runner.get_agent_states()['developer']['state']=='working'
                if stop=='max_steps':
                    assert outcome.metadata['stopped']=='max_steps' and len(writes)==1
                else:
                    assert 'bounded native model error' in outcome.output and not writes
                _install_native_lifecycle_status(runner, Agent)
                assert runner.get_agent_states()['developer']['state']=='stopped'
                build_primitive_tools(runner)
                result = leader._handle_messaging_tool('check_agent_status', {'agent_name':'developer'})
                assert 'STOPPED' in result and 'WORKING' not in result
                duplicate = await leader._execute_tool_call('start_agent', {'name':'developer'})
                assert 'execution loop has stopped' in duplicate and 'stop_agent' in duplicate
                assert 'already running' not in duplicate and len(calls)==1
                pool = await leader._execute_tool_call('list_pool', {})
                assert '**developer** [STOPPED' in pool
                denied = await worker._execute_tool_call('start_agent', {'name':'developer'})
                assert 'only available to the Chairman' in denied
                runner.message_store.send('developer', 'planner', 'Already queued reply.')
                reply = await asyncio.wait_for(leader._handle_wait_for_replies(
                    {'from_agents':['developer'], 'reason':'get final reply'}), .1)
                assert 'Already queued reply.' in reply and 'loops have stopped' in reply
                empty = await asyncio.wait_for(leader._handle_wait_for_replies(
                    {'from_agents':['developer'], 'reason':'check stopped loop'}), .1)
                assert 'cannot produce further replies' in empty and 'still WORKING' not in empty
                # A genuinely live worker keeps the original wait behavior.
                reviewer = runner.load_agent_from_pool('reviewer')
                reviewer.bind_message_store(runner.message_store)
                reviewer._state = 'working'
                runner.agents['reviewer'] = reviewer
                event = asyncio.Event()
                active = asyncio.create_task(event.wait())
                runner._agent_tasks['reviewer'] = active
                leader._idle_timeout = .01
                mixed = await leader._handle_wait_for_replies(
                    {'from_agents':['developer','reviewer'], 'reason':'wait for active reviewer'})
                assert 'reviewer is still WORKING' in mixed
                assert 'developer is still WORKING' not in mixed
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
                # Explicit firing of a completed worker releases its slot once,
                # retains native cost accounting, and does not start a new loop.
                worker._total_cost_usd = 1.25
                fired = await leader._execute_tool_call('stop_agent', {'name':'developer'})
                assert 'has been stopped' in fired
                assert runner._stopped_agents_cost==1.25
                assert not runner.stop_agent('developer')
                assert 'developer' not in runner.agents and len(calls)==1
                replacement = runner.load_agent_from_pool('developer')
                runner.start_agent(replacement)
                assert 'developer' in runner._agent_tasks
                runner._agent_tasks['developer'].cancel()
                await asyncio.gather(runner._agent_tasks['developer'], return_exceptions=True)
                cleanup_handlers(runner)
            asyncio.run(run())
