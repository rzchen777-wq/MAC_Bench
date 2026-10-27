from __future__ import annotations

from mac_bench.runtime.tools.protocol import normalize_tool_calls, parse_function_call


def test_openai_tool_call_helpers_normalize_and_parse_arguments() -> None:
    calls = normalize_tool_calls(
        ({"function": {"name": "exec", "arguments": '{"command":"pwd"}'}},),
        agent_id="agent-1",
        turn=3,
    )

    assert calls[0]["id"] == "call-agent-1-3-0"
    parsed = parse_function_call(calls[0])
    assert parsed.name == "exec"
    assert parsed.arguments["command"] == "pwd"
