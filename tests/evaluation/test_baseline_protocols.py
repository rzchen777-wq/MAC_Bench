"""Replay probe failure paths without model requests or paid sandboxes."""
from mac_bench.runtime.paths import repository_root
import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from mac_bench.baselines.aorchestra.protocol import action_messages, complete_action
from mac_bench.runtime.llm.client import ChatResponse


@pytest.mark.baselines
def test_aorchestra_native_delegate_can_report_interrupted_worker(monkeypatch, tmp_path):
    from pathlib import Path
    from mac_bench.baselines.common.source import source_modules
    from mac_bench.baselines.common.gateway import MethodStop
    from mac_bench.baselines.aorchestra.tasks import _propagate_worker_stop
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    root = repository_root() / '.cache/baselines/AOrchestra'
    with source_modules(root, ('base', 'benchmark', 'aorchestra'), workdir=tmp_path):
        import aorchestra.tools.delegate as native
        monkeypatch.setattr(native, 'create_llm_instance', lambda config: object())
        monkeypatch.setattr(native, 'LLMsConfig', SimpleNamespace(default=lambda:
                            SimpleNamespace(get=lambda model: {})))
        monkeypatch.setattr(native, 'Memory', lambda **kwargs: object())
        monkeypatch.setattr(native, 'ReActAgent', lambda **kwargs: object())
        class InterruptedRunner:
            async def run(self, agent, env):
                _propagate_worker_stop(MethodStop(ModelOutputTruncatedError(
                    'output was cut; no draft executed', response=ChatResponse(
                        '{"action":"execute","params":', model='replay', usage={}, finish_reason='length'))))
        env = SimpleNamespace(instruction='whole task')
        delegate = native.DelegateTaskTool(env, InterruptedRunner(), ['replay'], benchmark_type='terminalbench')
        result = asyncio.run(delegate('one assignment', 'replay'))
        assert result['done'] is False and 'output was cut' in result['error']
        assert env.instruction == 'whole task'
    with pytest.raises(MethodStop):
        _propagate_worker_stop(MethodStop(RuntimeError('provider/storage failure')))


@pytest.mark.baselines
def test_aorchestra_long_delegation_does_not_become_false_finished(tmp_path):
    from pathlib import Path
    from mac_bench.baselines.common.source import source_modules
    from mac_bench.baselines.aorchestra.tasks import _coordinator_runner
    root = repository_root() / '.cache/baselines/AOrchestra'
    with source_modules(root, ('base', 'benchmark'), workdir=tmp_path):
        from benchmark.common.runner import Runner
        from benchmark.common.env import BasicInfo
        class ShortTimerRunner(Runner):
            step_timeout = 0.001
        class Agent:
            llm = SimpleNamespace(get_usage_summary=lambda: {'model': 'replay'})
            def reset(self, info):
                pass
            async def step(self, **kwargs):
                await asyncio.sleep(0.02)
                return {'action': 'submit'}, 'completed real delegation', None
        class Environment:
            def get_basic_info(self):
                return BasicInfo(env_id='replay', instruction='delegate then submit', action_space='submit', max_steps=1)
            async def reset(self):
                return {}
            async def step(self, action):
                return {}, 0.0, True, {'submitted': True}
        original = asyncio.run(ShortTimerRunner().run(Agent(), Environment()))
        assert original.done and original.trace[-1].info['error'] == 'step_timeout'
        fixed = asyncio.run(_coordinator_runner(ShortTimerRunner).run(Agent(), Environment()))
        assert fixed.done and fixed.trace[-1].info['submitted']
        assert 'error' not in fixed.trace[-1].info
        assert ShortTimerRunner.step_timeout == 0.001


def test_aorchestra_rejects_dsml_multiple_actions_and_wrong_parameters():
    async def run():
        contracts = {'execute': {'type': 'object', 'properties': {
            'command': {'type': 'string'}}, 'required': ['command']}}
        invalid = [
            '<｜｜DSML｜｜ calls>execute</｜｜DSML｜｜ calls>',
            '{"action":"execute","params":{"command":"one"}}\n'
            '{"action":"execute","params":{"command":"two"}}',
            '{"action":"execute","params":{"command":42}}',
        ]
        for draft in invalid:
            values = iter([draft, '{"action":"execute","params":{"command":"true"}}'])
            calls, events = [], []
            async def complete(messages):
                calls.append(list(messages))
                return ChatResponse(next(values), model='replay', usage={})
            result = await complete_action(complete,
                action_messages('Task', contracts, coordinator=False), contracts,
                event=events.append)
            assert json.loads(result.content)['params']['command'] == 'true'
            assert len(calls) == 2 and len(events) == 1
            assert calls[0][0]['role'] == 'system'
            assert 'no action was executed' in calls[1][-1]['content']
    asyncio.run(run())


def test_aorchestra_coordinator_cannot_execute_worker_tools_and_retries_are_bounded():
    async def run():
        calls = []
        async def complete(messages):
            calls.append(messages)
            return ChatResponse('{"action":"execute","params":{}}', model='replay', usage={})
        contracts = {'delegate_task': {'type': 'object'}, 'submit': {'type': 'object'}}
        with pytest.raises(ValueError, match='delegate_task'):
            await complete_action(complete, action_messages('Task', contracts,
                coordinator=True), contracts, event=lambda event: None)
        assert len(calls) == 3
    asyncio.run(run())


@pytest.mark.baselines
def test_native_magentic_replan_dispatches_again_and_executes_coder_block(tmp_path):
    pytest.importorskip('autogen_agentchat')
    from autogen_agentchat.agents import CodeExecutorAgent
    from autogen_agentchat.teams import MagenticOneGroupChat
    from autogen_ext.agents.magentic_one import MagenticOneCoderAgent
    from autogen_ext.models.replay import ReplayChatCompletionClient
    from autogen_agentchat.teams._group_chat._magentic_one import _magentic_one_group_chat as group
    from mac_bench.baselines.magentic_one.orchestration import repaired_orchestrator
    from mac_bench.baselines.magentic_one.world_agents import WorldCodeExecutor

    async def run():
        events, commands = [], []
        async def tool(name, args):
            assert name == 'terminal'
            commands.append(args['command'])
            value = subprocess.run(args['command'], shell=True, cwd=tmp_path,
                capture_output=True, text=True, timeout=5)
            return {'exit_code': value.returncode, 'output': value.stdout + value.stderr}
        gateway = SimpleNamespace(context=SimpleNamespace(event=events.append), tool=tool)
        block = '```sh\nprintf done > marker.txt\n```'
        def ledger(speaker, instruction, done=False):
            return json.dumps({key: {'answer': answer, 'reason': 'replay'} for key, answer in {
                'is_request_satisfied': done, 'is_progress_being_made': False,
                'is_in_loop': False, 'next_speaker': speaker,
                'instruction_or_question': instruction}.items()})
        # The third stalled round replans. The fourth must dispatch to Coder,
        # not immediately replan again without a participant turn.
        coordinator = ReplayChatCompletionClient([
            'Initial facts', 'Initial plan', ledger('Coder', 'Write executable code'),
            ledger('ComputerTerminal', "Execute Coder's latest code block."),
            ledger('Coder', 'Review result'),
            'Updated facts: command executed', 'Updated plan: check result',
            ledger('Coder', 'Report observed progress'),
            ledger('Coder', 'Finish', done=True), 'Final report'])
        coder = MagenticOneCoderAgent('Coder', model_client=ReplayChatCompletionClient([
            block, 'The marker was written by the terminal.']))
        terminal = CodeExecutorAgent('ComputerTerminal', code_executor=WorldCodeExecutor(
            gateway, asyncio.Lock(), [], timeout=5))
        original = group.MagenticOneOrchestrator
        with repaired_orchestrator(engineering=True, event=events.append):
            team = MagenticOneGroupChat([coder, terminal], model_client=coordinator,
                                       max_stalls=3, max_turns=10)
            outcome = await team.run(task='Write marker.txt using the terminal.')
        assert group.MagenticOneOrchestrator is original
        assert (tmp_path/'marker.txt').read_text() == 'done'
        assert len(commands) == 1
        assert len([e for e in events if e['kind'] == 'stall_window_reset']) == 1
        assert sum(m.source == 'Coder' for m in outcome.messages) == 2
    asyncio.run(run())
