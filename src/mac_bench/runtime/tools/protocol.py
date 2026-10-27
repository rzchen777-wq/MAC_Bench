from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class FunctionCall:
    name: str
    arguments: Mapping[str, Any]
    thought: str = ""


def function_tool_schema(
    *,
    name: str,
    description: str,
    properties: Mapping[str, Any],
    required: Sequence[str] = (),
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": dict(properties),
                "required": list(required),
                "additionalProperties": additional_properties,
            },
        },
    }


def normalize_tool_calls(
    tool_calls: Sequence[Mapping[str, Any]],
    *,
    agent_id: str,
    turn: int,
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(tool_calls):
        call = dict(raw)
        call["id"] = str(call.get("id") or f"call-{agent_id}-{turn}-{index}")
        call.setdefault("type", "function")
        function = call.get("function")
        call["function"] = dict(function) if isinstance(function, Mapping) else {}
        normalized.append(call)
    return tuple(normalized)


def parse_function_call(tool_call: Mapping[str, Any]) -> FunctionCall:
    function = tool_call.get("function")
    if not isinstance(function, Mapping):
        raise ValueError("tool_call missing function payload")
    name = str(function.get("name") or "").strip()
    if not name:
        raise ValueError("tool_call function name is empty")
    raw_arguments = function.get("arguments") or {}
    if isinstance(raw_arguments, Mapping):
        arguments = dict(raw_arguments)
    else:
        try:
            parsed = json.loads(str(raw_arguments))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid tool arguments JSON: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise ValueError("tool_call arguments must be a JSON object")
        arguments = dict(parsed)
    thought = str(
        arguments.get("thought")
        or arguments.get("reason")
        or arguments.get("rationale")
        or ""
    ).strip()
    return FunctionCall(name=name, arguments=arguments, thought=thought)
