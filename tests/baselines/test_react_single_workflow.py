"""Tests for the react_single native tool-calling workflow."""

from __future__ import annotations

import unittest
import json

from mac_bench.runtime.agents.workflow import (
    AgentBudget,
    ReactAgent,
    _compact_react_history,
)
from mac_bench.runtime.types import PublicTask
from mac_bench.runtime.llm.client import ChatMessage, ChatResponse, ModelOutputTruncatedError
from mac_bench.runtime.sandbox.base import CommandResult
from mac_bench.runtime.tools import ToolSpec, ToolResult


def tool_call(call_id, name, arguments):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}


class ScriptedToolClient:
    def __init__(self, tool_calls: list[dict | list[dict]]) -> None:
        self.tool_calls = tool_calls
        self.calls = 0
        self.seen_messages: list[list] = []
        self.seen_tools: list[list | None] = []

    def complete(self, messages, *, temperature=0.0, max_tokens=None, tools=None):
        self.seen_messages.append(list(messages))
        self.seen_tools.append(list(tools) if tools is not None else None)
        scripted = self.tool_calls.pop(0) if self.tool_calls else []
        tool_calls = tuple(scripted) if isinstance(scripted, list) else (scripted,)
        self.calls += 1
        return ChatResponse(
            content="",
            model="fake",
            usage={"prompt_tokens": 10, "completion_tokens": 2},
            finish_reason="tool_calls" if tool_calls else "stop",
            tool_calls=tool_calls,
        )


class FakeTools:
    def __init__(self):
        self.commands = []
    def exec_solver(self, workspace, command, *, timeout=120):
        self.commands.append(command)
        return CommandResult(command, 0, "1 passed", 0.1)
    def __iter__(self):
        def execute(context, args):
            result = self.exec_solver(None, args['command'])
            return ToolResult.completed({'exit_code': result.exit_code}, model_text=result.stdout)
        yield ToolSpec('exec', 'Run a task command', {'command': {'type': 'string'}}, ('command',), execute)

TASK = PublicTask("task-001", "owner/repo", "Fix the behavior.")


class ReactAgentTest(unittest.TestCase):
    def test_assistant_text_is_preserved_as_thinking_before_next_tool_call(self) -> None:
        class ThinkingClient:
            def __init__(self) -> None:
                self.calls = 0
                self.seen_messages = []

            def complete(self, messages, *, temperature=0.0, max_tokens=None, tools=None):
                del temperature, max_tokens, tools
                self.seen_messages.append(list(messages))
                self.calls += 1
                if self.calls == 1:
                    return ChatResponse(
                        content="I should inspect the task before deciding.",
                        model="fake",
                        usage={"prompt_tokens": 10, "completion_tokens": 5},
                    )
                call = tool_call("call-finish", "finish", json.dumps({"summary": "done"}))
                return ChatResponse(
                    content="",
                    model="fake",
                    usage={"prompt_tokens": 10, "completion_tokens": 2},
                    finish_reason="tool_calls",
                    tool_calls=(call,),
                )

        client = ThinkingClient()
        outcome = ReactAgent(client, FakeTools()).run(task=TASK)

        self.assertEqual("finished", outcome.stop_reason)
        self.assertEqual(
            ["system", "user", "assistant"],
            [message.role for message in client.seen_messages[1]],
        )
        self.assertEqual(
            "assistant_text",
            next(item for item in outcome.trace if item["kind"] == "thought")["protocol"],
        )

    def test_react_stops_cleanly_on_truncated_model_output(self) -> None:
        class TruncatedClient:
            calls = 0

            def complete(self, messages, *, temperature=0.0, max_tokens=None, tools=None):
                del messages, temperature, max_tokens, tools
                self.calls += 1
                raise ModelOutputTruncatedError("increase max_tokens")

        client = TruncatedClient()

        outcome = ReactAgent(client, FakeTools()).run(task=TASK)

        self.assertEqual(1, client.calls)
        self.assertEqual("model_output_truncated", outcome.stop_reason)
        self.assertIn("increase max_tokens", outcome.summary)

    def test_truncated_text_continues_and_counts_failed_request(self) -> None:
        class Client:
            def __init__(self):
                self.caps = []
                self.histories = []

            def complete(self, messages, **kwargs):
                self.caps.append(kwargs['max_tokens'])
                self.histories.append(list(messages))
                if len(self.caps) == 1:
                    raise ModelOutputTruncatedError('length', response=ChatResponse(
                        'Already computed allocation A; next is B', model='fake',
                        usage={'prompt_tokens': 30, 'completion_tokens': 10}, finish_reason='length'))
                return ChatResponse('', model='fake', usage={'prompt_tokens': 20, 'completion_tokens': 3},
                    tool_calls=(tool_call('done', 'finish', '{"summary":"done"}'),))

        client = Client()
        outcome = ReactAgent(client, FakeTools(), model_max_tokens=100).run(task=TASK)
        self.assertEqual('finished', outcome.stop_reason)
        self.assertEqual([100, 200], client.caps)
        self.assertEqual(2, outcome.model_calls)
        self.assertEqual(50, outcome.input_tokens)
        self.assertEqual(13, outcome.output_tokens)
        self.assertEqual('Already computed allocation A; next is B', client.histories[1][-2].content)
        self.assertIn('NOT executed', client.histories[1][-1].content)

    def test_context_preparation_reserves_recovery_output_cap(self) -> None:
        from unittest.mock import patch
        from mac_bench.runtime.agents.context import ContextPolicy, WorkingContext
        class Client:
            count = 0
            def complete(self, messages, **kwargs):
                self.count += 1
                if self.count == 1:
                    raise ModelOutputTruncatedError('length', response=ChatResponse(
                        'unfinished', model='fake', usage={}, finish_reason='length'))
                return ChatResponse('', model='fake', usage={}, tool_calls=(tool_call('done', 'finish', '{"summary":"done"}'),))
        reservations = []
        original = WorkingContext.prepare
        def prepare(context, messages, schemas, output_tokens, *args):
            reservations.append(output_tokens)
            return original(context, messages, schemas, output_tokens, *args)
        with patch.object(WorkingContext, 'prepare', prepare):
            outcome = ReactAgent(Client(), FakeTools(), model_max_tokens=100,
                context_policy=ContextPolicy()).run(task=TASK)
        self.assertEqual('finished', outcome.stop_reason)
        self.assertEqual([100, 200], reservations)

    def test_truncated_tool_calls_are_withheld_even_with_valid_json(self) -> None:
        call = tool_call('partial', 'exec', '{"command":"write once"}')

        class Client(ScriptedToolClient):
            def __init__(self):
                super().__init__([call, tool_call('done', 'finish', '{"summary":"done"}')])
                self.caps = []

            def complete(self, messages, **kwargs):
                self.caps.append(kwargs['max_tokens'])
                if len(self.caps) == 1:
                    raise ModelOutputTruncatedError('length', response=ChatResponse(
                        '', model='fake', usage={'prompt_tokens': 30, 'completion_tokens': 10},
                        finish_reason='length', tool_calls=(call,)))
                return super().complete(messages, **kwargs)

        client, tools = Client(), FakeTools()
        outcome = ReactAgent(client, tools, model_max_tokens=100).run(task=TASK)
        self.assertEqual('finished', outcome.stop_reason)
        self.assertEqual(['write once'], tools.commands)
        self.assertEqual([100, 200, 100], client.caps)
        self.assertFalse(any(m.tool_calls for m in client.seen_messages[0]))
        self.assertEqual(3, outcome.model_calls)

    def test_incomplete_tool_argument_draft_is_available_for_chunk_recovery(self) -> None:
        prefix = '{"command":"cat > result.py <<EOF\\nVALUE = 17\\n'

        class Client:
            count = 0
            def complete(self, messages, **kwargs):
                self.count += 1
                if self.count == 1:
                    raise ModelOutputTruncatedError('length', response=ChatResponse(
                        '', model='fake', usage={'prompt_tokens': 10, 'completion_tokens': 5},
                        finish_reason='length', tool_calls=(tool_call('draft', 'exec', prefix),)))
                if self.count == 2:
                    assert prefix in messages[-1].content
                    assert 'not present on disk' in messages[-1].content
                    assert not any(m.tool_calls for m in messages)
                    name, arguments = 'exec', '{"command":"write complete first chunk"}'
                else:
                    name, arguments = 'finish', '{"summary":"done"}'
                return ChatResponse('', model='fake', usage={'prompt_tokens': 10, 'completion_tokens': 2},
                    tool_calls=(tool_call(str(self.count), name, arguments),))

        client, tools = Client(), FakeTools()
        outcome = ReactAgent(client, tools, model_max_tokens=100).run(task=TASK)
        self.assertEqual('finished', outcome.stop_reason)
        self.assertEqual(['write complete first chunk'], tools.commands)
        self.assertEqual(3, outcome.model_calls)

    def test_tool_arguments_continue_across_two_fragments_and_execute_once(self) -> None:
        class Client:
            count = 0
            def complete(self, messages, **kwargs):
                self.count += 1
                usage = {'prompt_tokens': 10, 'completion_tokens': 2}
                if self.count == 1:
                    raise ModelOutputTruncatedError('length', response=ChatResponse(
                        '', model='fake', usage=usage, finish_reason='length', tool_calls=(tool_call('draft', 'exec', '{"command":"echo PRE'),)))
                if self.count == 2:
                    assert not kwargs.get('tools')
                    assert '{"command":"echo PRE' in messages[-1].content
                    raise ModelOutputTruncatedError('length', response=ChatResponse(
                        'FIX', model='fake', usage=usage, finish_reason='length'))
                if self.count == 3:
                    assert '{"command":"echo PREFIX' in messages[-1].content
                    return ChatResponse('"}', model='fake', usage=usage, finish_reason='stop')
                return ChatResponse('', model='fake', usage=usage, tool_calls=(tool_call('done', 'finish', '{"summary":"done"}'),))

        client, tools = Client(), FakeTools()
        outcome = ReactAgent(client, tools, model_max_tokens=100,
                             completion_recovery_attempts=3).run(task=TASK)
        self.assertEqual('finished', outcome.stop_reason)
        self.assertEqual(['echo PREFIX'], tools.commands)
        self.assertEqual((4, 40, 8), (outcome.model_calls, outcome.input_tokens, outcome.output_tokens))
        client, tools = Client(), FakeTools()
        outcome = ReactAgent(client, tools, model_max_tokens=100,
                             budget=AgentBudget(max_model_calls=2)).run(task=TASK)
        self.assertEqual('model_call_budget', outcome.stop_reason)
        self.assertEqual(2, outcome.model_calls)
        self.assertEqual([], tools.commands)

    def test_failed_argument_continuation_preserves_normal_tool_recovery(self) -> None:
        class Client:
            count = 0
            def complete(self, messages, **kwargs):
                self.count += 1
                usage = {'prompt_tokens': 10, 'completion_tokens': 2}
                if self.count == 1:
                    raise ModelOutputTruncatedError('length', response=ChatResponse(
                        '', model='fake', usage=usage, finish_reason='length', tool_calls=(tool_call('bad', 'exec', '{"command":"repeat repeat'),)))
                if self.count == 2:
                    assert not kwargs.get('tools')
                    raise ModelOutputTruncatedError('length', response=ChatResponse(
                        'repeat repeat', model='fake', usage=usage, finish_reason='length'))
                assert kwargs.get('tools')
                if self.count == 3:
                    assert 'Do not reissue the entire large file' in messages[-1].content
                    assert not any('repeat repeat' in (m.content or '') for m in messages)
                    assert not any(m.tool_calls for m in messages)
                    name, arguments = 'exec', '{"command":"write a small fresh chunk"}'
                else:
                    name, arguments = 'finish', '{"summary":"done"}'
                return ChatResponse('', model='fake', usage=usage, tool_calls=(tool_call(str(self.count), name, arguments),))
        tools = FakeTools()
        outcome = ReactAgent(Client(), tools, model_max_tokens=100).run(task=TASK)
        self.assertEqual('finished', outcome.stop_reason)
        self.assertEqual(['write a small fresh chunk'], tools.commands)
        self.assertEqual(4, outcome.model_calls)

    def test_truncation_recovery_is_bounded_and_charged(self) -> None:
        class Client:
            def __init__(self): self.caps = []
            def complete(self, messages, **kwargs):
                self.caps.append(kwargs['max_tokens'])
                raise ModelOutputTruncatedError('length', response=ChatResponse(
                    'unfinished', model='fake', usage={'prompt_tokens': 10, 'completion_tokens': 2},
                    finish_reason='length'))

        client = Client()
        outcome = ReactAgent(client, FakeTools(), model_max_tokens=100,
            completion_recovery_max_tokens=300).run(task=TASK)
        self.assertEqual([100, 200, 300], client.caps)
        self.assertEqual('model_output_truncated', outcome.stop_reason)
        self.assertEqual(3, outcome.model_calls)
        self.assertEqual(30, outcome.input_tokens)
        self.assertEqual(6, outcome.output_tokens)
        for budget, reason in [(AgentBudget(max_model_calls=1), 'model_call_budget'),
                               (AgentBudget(max_tokens=12), 'token_budget')]:
            client = Client()
            outcome = ReactAgent(client, FakeTools(), budget=budget).run(task=TASK)
            self.assertEqual(1, len(client.caps))
            self.assertEqual(reason, outcome.stop_reason)

    def test_react_tool_loop_preserves_reasoning_in_second_request(self) -> None:
        class ReasoningClient:
            def __init__(self) -> None:
                self.message_batches = []
                self.max_tokens = []

            def complete(self, messages, *, temperature=0.0, max_tokens=None, tools=None):
                del temperature, tools
                self.message_batches.append(list(messages))
                self.max_tokens.append(max_tokens)
                if len(self.message_batches) == 1:
                    call = tool_call("reasoning-read", "exec", json.dumps({"command": "cat src/a.py"}))
                    return ChatResponse(
                        content="",
                        model="fake",
                        usage={"prompt_tokens": 1, "completion_tokens": 5},
                        finish_reason="tool_calls",
                        tool_calls=(call,),
                        assistant_message={
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "Need to inspect the file.",
                            "reasoning_details": [
                                {
                                    "type": "reasoning.text",
                                    "text": "Need to inspect the file.",
                                    "signature": "sig",
                                    "index": 0,
                                }
                            ],
                            "tool_calls": [call],
                        },
                    )
                call = tool_call("reasoning-finish", "finish", json.dumps({"summary": "done"}))
                return ChatResponse(
                    content="",
                    model="fake",
                    usage={"prompt_tokens": 1, "completion_tokens": 1},
                    finish_reason="tool_calls",
                    tool_calls=(call,),
                    assistant_message={"role": "assistant", "tool_calls": [call]},
                )

        client = ReasoningClient()

        outcome = ReactAgent(client, FakeTools(), model_max_tokens=4321).run(
            task=TASK
        )

        self.assertEqual("finished", outcome.stop_reason)
        self.assertEqual([4321, 4321], client.max_tokens)
        assistant = client.message_batches[1][2]
        self.assertEqual("Need to inspect the file.", assistant.reasoning_content)
        self.assertEqual("sig", assistant.reasoning_details[0]["signature"])
        self.assertEqual("reasoning-read", client.message_batches[1][3].tool_call_id)

    def test_react_history_compaction_preserves_openai_tool_pairing(self) -> None:
        messages = [ChatMessage("system", "system"), ChatMessage("user", "task")]
        for index in range(10):
            call_id = f"call-{index}"
            messages.extend(
                [
                    ChatMessage(
                        "assistant",
                        tool_calls=(
                            tool_call(call_id, "exec", json.dumps(
                                        {"command": "printf x" + "x" * 500}
                                    )),
                        ),
                    ),
                    ChatMessage("tool", "output " + "y" * 1000, tool_call_id=call_id),
                ]
            )

        stats = _compact_react_history(
            messages,
            keep_recent_tool_results=2,
            tool_result_limit=120,
            assistant_limit=120,
        )

        self.assertEqual(8, stats["tool_results"])
        self.assertEqual(8, stats["assistant_messages"])
        self.assertGreater(stats["chars_removed"], 0)
        assistant_ids = {
            str(call["id"])
            for message in messages
            for call in message.tool_calls
        }
        tool_ids = {str(message.tool_call_id) for message in messages if message.role == "tool"}
        self.assertEqual(assistant_ids, tool_ids)
        self.assertTrue(str(messages[3].content).startswith("[older tool result compacted"))
        self.assertNotIn("_compacted", str(messages[-2].tool_calls[0]))
        self.assertEqual("output " + "y" * 1000, messages[-1].content)

    def test_react_history_compaction_does_not_rewrite_reasoning_assistant(self) -> None:
        call = tool_call("signed-call", "exec", json.dumps({"command": "printf " + "x" * 500}))
        message = ChatMessage(
            "assistant",
            tool_calls=(call,),
            reasoning_details=(
                {
                    "type": "reasoning.text",
                    "text": "run it",
                    "signature": "signed",
                    "index": 0,
                },
            ),
        )
        messages = [
            ChatMessage("system", "system"),
            ChatMessage("user", "task"),
            message,
            ChatMessage("tool", "y" * 1000, tool_call_id="signed-call"),
        ]

        _compact_react_history(messages, keep_recent_tool_results=0, assistant_limit=50)

        self.assertEqual(message.as_dict(), messages[2].as_dict())

    def test_react_tool_schemas_follow_image_capability(self) -> None:
        class VisionToolClient(ScriptedToolClient):
            @property
            def capabilities(self):
                return frozenset({"image_input"})

        client = VisionToolClient(
            [
                tool_call("call-finish", "finish", json.dumps({"summary": "schema inspected"}))
            ]
        )

        ReactAgent(client, FakeTools()).run(task=TASK)

        names = {item["function"]["name"] for item in client.seen_tools[0]}
        self.assertEqual(
            {"exec", "finish"},
            names,
        )

    def test_react_agent_completes_entire_batch_when_finish_is_mixed_in(self) -> None:
        client = ScriptedToolClient(
            [
                [
                    tool_call("call-finish", "finish", json.dumps({"summary": "batch complete"})),
                    tool_call("call-exec", "exec", json.dumps({"command": "git status --short"})),
                ]
            ]
        )
        tools = FakeTools()

        outcome = ReactAgent(client, tools).run(task=TASK)

        self.assertEqual(["git status --short"], tools.commands)
        self.assertEqual("batch complete", outcome.summary)
        self.assertEqual("finished", outcome.stop_reason)
        self.assertEqual(1, client.calls)
        actions = [item for item in outcome.trace if item["kind"] == "action"]
        self.assertEqual(["finish", "exec"], [item["action"] for item in actions])

    def test_react_agent_executes_multiple_native_tool_calls_in_order(self) -> None:
        client = ScriptedToolClient(
            [
                [
                    tool_call("call-first", "exec", json.dumps(
                                {"thought": "inspect files", "command": "cat src/a.py"}
                            )),
                    tool_call("call-second", "exec", json.dumps(
                                {"thought": "inspect tests", "command": "cat tests/test_a.py"}
                            )),
                ],
                tool_call("call-finish", "finish", json.dumps({"summary": "inspection complete"})),
            ]
        )
        tools = FakeTools()

        outcome = ReactAgent(client, tools).run(task=TASK)

        self.assertEqual(["cat src/a.py", "cat tests/test_a.py"], tools.commands)
        self.assertEqual(2, outcome.tool_calls)
        self.assertEqual(2, client.calls)
        second_messages = client.seen_messages[1]
        self.assertEqual(
            ["system", "user", "assistant", "tool", "tool"],
            [message.role for message in second_messages],
        )
        self.assertEqual(2, len(second_messages[2].tool_calls))
        self.assertEqual("call-first", second_messages[3].tool_call_id)
        self.assertEqual("call-second", second_messages[4].tool_call_id)
        self.assertTrue(
            all(
                "1 passed" in str(message.content)
                for message in second_messages[3:]
            )
        )
        self.assertTrue(
            all("1 passed" in str(message.content) for message in second_messages[3:])
        )
        tool_events = [item for item in outcome.trace if item["kind"] == "tool"]
        self.assertEqual([0, 0], [item["step"] for item in tool_events])
        self.assertEqual(["exec", "exec"], [item["tool"] for item in tool_events])

    def test_react_agent_uses_native_tool_calls_when_available(self) -> None:
        client = ScriptedToolClient(
            [
                tool_call("call-exec", "exec", json.dumps(
                            {"thought": "inspect", "command": "cat src/a.py"}
                        )),
                tool_call("call-finish", "finish", json.dumps({"summary": "inspected file"})),
            ]
        )
        tools = FakeTools()

        outcome = ReactAgent(client, tools).run(task=TASK)

        self.assertEqual(["cat src/a.py"], tools.commands)
        self.assertEqual("inspected file", outcome.summary)
        self.assertTrue(client.seen_tools[0])
        second_roles = [message.role for message in client.seen_messages[1]]
        self.assertEqual(["system", "user", "assistant", "tool"], second_roles)
        self.assertTrue(client.seen_messages[1][2].tool_calls)
        self.assertEqual("call-exec", client.seen_messages[1][3].tool_call_id)
        actions = [item for item in outcome.trace if item["kind"] == "action"]
        self.assertEqual(["tool_calls", "tool_calls"], [item["protocol"] for item in actions])

    def test_react_prompt_uses_public_submission_contract_without_task_mode(self) -> None:
        class CaptureClient(ScriptedToolClient):
            def __init__(self) -> None:
                super().__init__([tool_call("call-finish", "finish", json.dumps({"summary": "nothing to change"}))])
                self.systems: list[str] = []

            def complete(self, messages, *, temperature=0.0, max_tokens=None, tools=None):
                self.systems.append(messages[0].content)
                return super().complete(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                )

        client = CaptureClient()
        ReactAgent(client, FakeTools()).run(task=TASK)
        self.assertIn(
            "provided task tools",
            client.systems[0],
        )
        self.assertNotIn("Task mode:", client.systems[0])
        self.assertNotIn("exactly one tool", client.systems[0])

    def test_react_unified_finish_accepts_artifacts(self) -> None:
        artifact = {
            "artifact_id": "protocol_card",
            "artifact_type": "analysis_memo",
            "content": "protocol note",
            "declared_outputs": {"protocol_id": "rg_a_g2_acs_income_geo_shift"},
            "citation_refs": ["C1", "C2", "C3"],
        }
        class CaptureClient(ScriptedToolClient):
            def __init__(self) -> None:
                super().__init__(
                    [
                        tool_call("call-exec", "exec", json.dumps({"command": "ls visible"})),
                        tool_call("call-finish", "finish", json.dumps(
                                    {
                                        "summary": "submitted research artifact",
                                        "artifacts": [artifact],
                                    },
                                    ensure_ascii=False,
                                )),
                    ]
                )
                self.systems: list[str] = []

            def complete(self, messages, *, temperature=0.0, max_tokens=None, tools=None):
                self.systems.append(messages[0].content)
                return super().complete(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                )

        client = CaptureClient()
        tools = FakeTools()
        outcome = ReactAgent(client, tools).run(task=TASK)
        self.assertIn("call finish", client.systems[0])
        self.assertEqual(["ls visible"], tools.commands)
        self.assertEqual(1, len(outcome.artifacts))
        self.assertEqual("protocol_card", outcome.artifacts[0]["artifact_id"])
        finish = next(item for item in outcome.trace if item["kind"] == "finish")
        self.assertNotIn("task_mode", finish)

    def test_react_observation_uses_head_tail_truncation(self) -> None:
        class BigOutputTools(FakeTools):
            def exec_solver(self, workspace, command, *, timeout=120):
                self.commands.append(command)
                return CommandResult(command, 0, "A" * 5000, 0.1)

        client = ScriptedToolClient(
            [
                tool_call("call-exec", "exec", json.dumps({"command": "cat huge.py"})),
                tool_call("call-finish", "finish", json.dumps({"summary": "done"})),
            ]
        )
        outcome = ReactAgent(client, BigOutputTools(), output_limit=200).run(
            task=TASK
        )
        observation = next(
            item["observation"] for item in outcome.trace if item["kind"] == "observation"
        )
        self.assertIn("truncated", observation)
        self.assertIn("head+tail", observation)
        self.assertLess(len(observation), 5000)
        tool = next(item for item in outcome.trace if item["kind"] == "tool")
        self.assertEqual("exec", tool["tool"])
        self.assertEqual("completed", tool["result"]["status"])


if __name__ == "__main__":
    unittest.main()
