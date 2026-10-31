"""Run a minimal tool-calling agent on a MAC-Bench task.

From the repository root, after completing docs/getting-started.md:
    python examples/minimal_agent.py --task-id flit_packages --out outputs/my-agent
"""
import argparse
import json
from pathlib import Path

from mac_bench.evaluation.episodes.identity import call_scope
from mac_bench.evaluation.episodes.runner import run_episode
from mac_bench.evaluation.tasks import load_catalog, open_task
from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.runtime.llm.client import (
    ChatMessage, LiteLLMChatClient, assistant_chat_message,
)
from mac_bench.runtime.llm.config import LLMConfig
from mac_bench.runtime.paths import repository_root


class MinimalAgent:
    name = "minimal_agent"

    def run(self, context):
        agent_id = "agent-1"
        context.event({"kind": "agent_start", "agent_id": agent_id})
        try:
            with call_scope(agent_id=agent_id):
                return self.solve(context)
        finally:
            context.event({"kind": "agent_end", "agent_id": agent_id})

    def solve(self, context):
        messages = [
            ChatMessage("system", context.task.interaction_instructions),
            ChatMessage("user", context.task.instruction),
        ]
        functions = [tool.as_function() for tool in context.task.tools]
        while True:
            reply = context.client.complete(messages, tools=functions, max_tokens=4096)
            messages.append(assistant_chat_message(reply))
            if not reply.tool_calls:
                return {"final_response": reply.content}
            for call in reply.tool_calls:
                function = call["function"]
                result = context.environment.call(
                    function["name"], json.loads(function["arguments"])
                )
                messages.append(ChatMessage(
                    "tool", json.dumps(result, ensure_ascii=False), tool_call_id=call["id"]
                ))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default="op26_service_administration_007")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=1_000_000)
    parser.add_argument("--max-model-calls", type=int, default=80)
    parser.add_argument("--max-tool-calls", type=int, default=160)
    parser.add_argument("--max-seconds", type=int, default=2400)
    args = parser.parse_args(argv)

    root = repository_root()
    record = load_catalog(root / "data/tasks.json", task_ids=[args.task_id])[0]
    client = LiteLLMChatClient(LLMConfig.from_env(require_api_key=True))
    budget = AgentBudget(
        max_tokens=args.max_tokens, max_model_calls=args.max_model_calls,
        max_tool_calls=args.max_tool_calls, max_env_actions=args.max_tool_calls,
        max_wall_seconds=args.max_seconds,
    )
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    task = open_task(record, output / "runtime")
    result = run_episode(
        task, MinimalAgent(), client, output=output / "episode", budget=budget,
    )
    print(json.dumps({key: result.get(key) for key in
                     ("task_id", "status", "ps", "rr", "stop_reason")}, indent=2))
    print(f"Episode files: {output / 'episode'}")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
