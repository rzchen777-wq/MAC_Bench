"""Concurrent world calls retain identities, budgets and sealing semantics."""
import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from mac_bench.baselines.common.gateway import Gateway
from mac_bench.baselines.meta_team.tasks import _EnvironmentCalls
from mac_bench.evaluation.episodes.budget import EpisodeBudget
from mac_bench.evaluation.episodes.identity import call_scope
from mac_bench.evaluation.tasks import AgentEnvironment, ToolDefinition
from mac_bench.runtime.agents.workflow import AgentBudget


def test_long_operation_does_not_block_read_and_seal_drains_cancelled_worker():
    started, release = Event(), Event()
    writes, events = [], []

    def dispatch(name, args):
        if name == 'write':
            started.set()
            assert release.wait(5)
            writes.append(args['value'])
        return {'value': args['value']}

    schema = {'type': 'object', 'properties': {'value': {'type': 'integer'}},
              'required': ['value']}
    env = AgentEnvironment([ToolDefinition(n, n, schema) for n in ('write', 'read')], dispatch)
    budget = EpisodeBudget(AgentBudget(max_tool_calls=10, max_env_actions=10))
    env.begin(admit_call=budget.admit_tool, event=events.append)
    gateway = Gateway(SimpleNamespace(environment=env), {})

    async def run():
        calls = _EnvironmentCalls(gateway, max_inflight=2)
        with call_scope(agent_id='worker-a', role='worker'):
            long = asyncio.create_task(calls.call('write', {'value': 1}))
        assert await asyncio.to_thread(started.wait, 2)
        with call_scope(agent_id='worker-b', role='worker'):
            assert await asyncio.wait_for(calls.call('read', {'value': 2}), 1) == {'value': 2}
        assert not long.done() and not writes
        long.cancel()
        with pytest.raises(asyncio.CancelledError):
            await long
        sealing = asyncio.create_task(calls.close())
        await asyncio.sleep(0)
        assert not sealing.done()
        denied = await asyncio.wait_for(calls.call('write', {'value': 3}), 1)
        assert denied['error']['code'] == 'dev_candidate_sealed'
        release.set()
        await asyncio.wait_for(sealing, 2)

    try:
        asyncio.run(run())
    finally:
        release.set()
        env.end()
    assert writes == [1] and budget.tool_calls == 2
    completed = {e['result']['value']: e['agent_id'] for e in events if e['kind'] == 'task_tool_end'}
    assert completed == {1: 'worker-a', 2: 'worker-b'}


def test_concurrency_bound_and_queued_calls_rejected_after_seal():
    async def run():
        release, two_started = asyncio.Event(), asyncio.Event()
        started = []

        async def tool(name, args):
            started.append(args['value'])
            if len(started) == 2:
                two_started.set()
            await release.wait()
            return args

        calls = _EnvironmentCalls(SimpleNamespace(tool=tool), max_inflight=2)
        workers = [asyncio.create_task(calls.call('write', {'value': i})) for i in range(8)]
        await asyncio.wait_for(two_started.wait(), 1)
        await asyncio.sleep(.01)
        assert len(started) == 2
        workers[0].cancel()
        await asyncio.gather(workers[0], return_exceptions=True)
        await asyncio.sleep(.01)
        assert len(started) == 2  # Cancellation has not released its slot.
        sealing = asyncio.create_task(calls.close())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(sealing, 1)
        results = await asyncio.gather(*workers[1:])
        assert len(started) == 2
        assert sum('error' in r for r in results) == 6

    asyncio.run(run())
