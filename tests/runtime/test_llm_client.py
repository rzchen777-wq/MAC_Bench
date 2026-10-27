from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mac_bench.runtime.llm.client import (
    ChatMessage,
    ChatResponse,
    LiteLLMChatClient,
    ModelOutputTruncatedError,
    assistant_chat_message,
)
from mac_bench.runtime.llm.config import LLMConfig


def _fake_response(
    *,
    content: str | None = "ok",
    reasoning: str | None = None,
    finish: str = "stop",
    tool_calls=None,
):
    message = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        model_dump=lambda exclude_none=True: {
            key: value
            for key, value in {
                "role": "assistant",
                "content": content,
                "reasoning_content": reasoning,
                "tool_calls": tool_calls,
            }.items()
            if not exclude_none or value is not None
        },
    )
    choice = SimpleNamespace(message=message, finish_reason=finish)
    return SimpleNamespace(
        id="response-1",
        model="fake",
        choices=[choice],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class LLMClientTest(unittest.TestCase):
    @patch("mac_bench.runtime.llm.client.apply_gateway_env")
    def test_client_can_avoid_publishing_process_environment(self, apply_env) -> None:
        config = LLMConfig(
            base_url="https://judge.invalid/v1",
            api_key="judge-key",
            model="judge-model",
        )
        LiteLLMChatClient(config, publish_env=False)
        apply_env.assert_not_called()

        LiteLLMChatClient(config)
        apply_env.assert_called_once_with(config)

    @patch("mac_bench.runtime.llm.gateway.time.sleep", create=True)
    @patch("mac_bench.runtime.llm.client.gateway_complete")
    def test_transient_failure_is_retried_by_gateway(self, complete, sleep) -> None:
        del sleep
        # Client itself calls gateway_complete once; retries live inside gateway.
        # Here we assert the client maps a successful gateway response.
        complete.return_value = _fake_response()
        client = LiteLLMChatClient(
            LLMConfig(
                base_url="https://example.invalid/v1",
                api_key="secret",
                model="fake",
                max_retries=2,
            )
        )
        response = client.complete([ChatMessage("user", "hello")])
        self.assertEqual("ok", response.content)
        self.assertEqual(1, complete.call_count)
        kwargs = complete.call_args.kwargs
        self.assertEqual("fake", kwargs["model"])
        self.assertEqual([{"role": "user", "content": "hello"}], kwargs["messages"])
        self.assertEqual("secret", kwargs["api_key"])
        self.assertEqual(120.0, kwargs["timeout_seconds"])
        self.assertEqual("disabled", kwargs["thinking_mode"])

    @patch("mac_bench.runtime.llm.client.gateway_complete")
    def test_clients_pass_connection_state_per_request(self, complete) -> None:
        complete.return_value = _fake_response()
        first = LiteLLMChatClient(
            LLMConfig(
                base_url="https://solver.invalid/v1",
                api_key="solver-key",
                model="solver-model",
            )
        )
        second = LiteLLMChatClient(
            LLMConfig(
                base_url="https://judge.invalid/v1",
                api_key="judge-key",
                model="judge-model",
                timeout_seconds=45,
                thinking_mode="enabled",
            )
        )

        first.complete([ChatMessage("user", "solve")])
        second.complete([ChatMessage("user", "judge")])

        first_call, second_call = complete.call_args_list
        self.assertEqual("solver-key", first_call.kwargs["api_key"])
        self.assertEqual("https://solver.invalid/v1", first_call.kwargs["api_base"])
        self.assertEqual("judge-key", second_call.kwargs["api_key"])
        self.assertEqual("https://judge.invalid/v1", second_call.kwargs["api_base"])
        self.assertEqual(45, second_call.kwargs["timeout_seconds"])

    @patch("mac_bench.runtime.llm.gateway.time.sleep", return_value=None)
    @patch("mac_bench.runtime.llm.gateway.litellm.completion")
    def test_gateway_retries_then_succeeds(self, completion, sleep) -> None:
        from mac_bench.runtime.llm import gateway

        del sleep
        completion.side_effect = [
            ConnectionError("temporary"),
            _fake_response(content="recovered"),
        ]
        response = gateway.complete(
            model="fake",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0,
            max_tokens=16,
            api_base="https://example.invalid/v1",
            max_retries=2,
        )
        self.assertEqual("recovered", response.choices[0].message.content)
        self.assertEqual(2, completion.call_count)
        # openai/ prefix when custom base is set
        self.assertEqual("openai/fake", completion.call_args.kwargs["model"])
        self.assertEqual("https://example.invalid/v1", completion.call_args.kwargs["api_base"])

    def test_resolve_model_strips_deepseek_prefix_when_base_set(self) -> None:
        from mac_bench.runtime.llm.gateway import resolve_model_name

        self.assertEqual(
            "openai/deepseek-v4-flash",
            resolve_model_name(
                "deepseek/deepseek-v4-flash",
                api_base="https://api.deepseek.com",
            ),
        )
        self.assertEqual(
            "openai/deepseek-v4-flash",
            resolve_model_name("deepseek-v4-flash", api_base="https://api.deepseek.com"),
        )

    def test_reasoning_parameters_follow_gateway_protocol(self) -> None:
        from mac_bench.runtime.llm.gateway import _reasoning_extra_body

        self.assertEqual(
            {"thinking": {"type": "disabled"}},
            _reasoning_extra_body(
                api_base="https://api.deepseek.com",
                model="deepseek-v4-flash",
                thinking_mode="disabled",
            ),
        )
        self.assertEqual(
            {"reasoning": {"effort": "none"}},
            _reasoning_extra_body(
                api_base="https://openrouter.ai/api/v1",
                model="deepseek/deepseek-chat",
                thinking_mode="disabled",
            ),
        )
        self.assertEqual(
            {"reasoning": {"enabled": True}},
            _reasoning_extra_body(
                api_base="https://openrouter.ai/api/v1",
                model="openai/gpt-5-nano",
                thinking_mode="enabled",
            ),
        )
        self.assertIsNone(
            _reasoning_extra_body(
                api_base="https://example.invalid/v1",
                model="custom-model",
                thinking_mode="disabled",
            )
        )

    def test_client_syncs_thinking_mode_to_gateway_env(self) -> None:
        from mac_bench.runtime.llm.gateway import apply_gateway_env

        env: dict[str, str] = {}
        apply_gateway_env(
            LLMConfig(
                base_url="https://openrouter.ai/api/v1",
                api_key="secret",
                model="openai/gpt-5-nano",
                thinking_mode="enabled",
            ),
            environ=env,
        )
        self.assertEqual("enabled", env["MAC_BENCH_LLM_THINKING"])

    @patch("mac_bench.runtime.llm.client.gateway_complete")
    def test_length_finish_with_reasoning_raises_truncation_error(self, complete) -> None:
        complete.return_value = _fake_response(content="", reasoning="thinking", finish="length")
        client = LiteLLMChatClient(
            LLMConfig(
                base_url="https://api.deepseek.com",
                api_key="secret",
                model="deepseek-v4-flash",
                thinking_mode="enabled",
            )
        )
        with self.assertRaisesRegex(ModelOutputTruncatedError, "max_tokens"):
            client.complete([ChatMessage("user", "hello")])

    def test_reasoning_tool_message_round_trips_without_changes(self) -> None:
        details = [
            {
                "type": "reasoning.text",
                "text": "inspect first",
                "signature": "signed-value",
                "index": 0,
            }
        ]
        tool_calls = [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path":"a.py"}'},
            }
        ]
        response = ChatResponse(
            content="",
            model="fake",
            usage={"prompt_tokens": 1, "completion_tokens": 2},
            finish_reason="tool_calls",
            tool_calls=tuple(tool_calls),
            assistant_message={
                "role": "assistant",
                "content": "",
                "reasoning": "inspect first",
                "reasoning_content": "inspect first",
                "reasoning_details": details,
                "tool_calls": tool_calls,
            },
        )

        message = assistant_chat_message(response)

        self.assertEqual("inspect first", message.reasoning)
        self.assertEqual("inspect first", message.reasoning_content)
        self.assertEqual(details, message.as_dict()["reasoning_details"])
        self.assertEqual(tool_calls, message.as_dict()["tool_calls"])
        self.assertIsNot(details, message.as_dict()["reasoning_details"])

    @patch("mac_bench.runtime.llm.client.gateway_complete")
    def test_tool_calls_are_passed_through_and_allow_empty_content(self, complete) -> None:
        tool_calls = [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_mailbox", "arguments": "{}"},
            }
        ]
        complete.return_value = _fake_response(content=None, finish="tool_calls", tool_calls=tool_calls)
        client = LiteLLMChatClient(
            LLMConfig(
                base_url="https://example.invalid/v1",
                api_key="secret",
                model="fake",
            )
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_mailbox",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        response = client.complete(
            [ChatMessage("user", "hello"), ChatMessage("tool", "{}", tool_call_id="old")],
            tools=tools,
        )

        self.assertEqual("", response.content)
        self.assertEqual(tuple(tool_calls), response.tool_calls)
        self.assertEqual(tools, complete.call_args.kwargs["tools"])
        self.assertEqual(
            {"role": "tool", "content": "{}", "tool_call_id": "old"},
            complete.call_args.kwargs["messages"][1],
        )

    @patch("mac_bench.runtime.llm.client.gateway_complete")
    def test_multimodal_tool_result_is_forwarded_as_native_content_blocks(self, complete) -> None:
        complete.return_value = _fake_response(content="inspected")
        client = LiteLLMChatClient(
            LLMConfig(
                base_url="https://example.invalid/v1",
                api_key="secret",
                model="fake",
                image_input=True,
            )
        )
        content = [
            {"type": "text", "text": '{"path":"plot.png"}'},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64,YWJj",
                    "detail": "high",
                },
            },
        ]

        response = client.complete(
            [ChatMessage("tool", content, tool_call_id="image-call")]
        )

        self.assertEqual("inspected", response.content)
        self.assertEqual(frozenset({"image_input"}), client.capabilities)
        self.assertEqual(content, complete.call_args.kwargs["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
