# Agent integration

[Home](../README.md) · [Getting started](getting-started.md) · [Tasks](tasks.md) · [Evaluation](evaluation.md) · **Agent integration** · [Environments](environments.md)

An adapter is a Python object with a `name` and a `run(context)` method. The evaluator initializes the task, calls your adapter, and checks the final environment after it returns.

## Start with a working example

After the [Quickstart](getting-started.md), run:

```bash
python examples/minimal_agent.py \
  --task-id op26_service_administration_007 --out outputs/my-agent
```

[The complete example](../examples/minimal_agent.py) implements a small tool-calling agent and its evaluation entry point. Its core loop is:

```python
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
```

The file includes the imports, agent identity and command-line entry point. Replace this loop with your system's planning and communication logic. Keep model requests on `context.client` and task actions on `context.environment` so usage and interactions remain recorded.

The example finishes when the model responds without a tool call. The evaluator also stops execution at the configured budget and scores the state reached so far. The built-in [ReAct adapter](../src/mac_bench/baselines/react_single/tasks.py) adds context management and completion recovery for longer runs.

## Connect the evaluator

The example loads a catalog entry and passes your adapter to `run_episode`:

```python
record = load_catalog(root / "data/tasks.json", task_ids=[task_id])[0]
task = open_task(record, output / "runtime")
result = run_episode(
    task, MinimalAgent(), client,
    output=output / "episode", budget=budget,
)
```

`open_task` creates a handle; `run_episode` opens its environment, checks the initial state, records execution, evaluates the outcome and closes the environment. Its returned dictionary contains `ps`, `rr`, `status`, token usage and other episode fields. This Python entry point accepts your adapter directly, without a CLI registry change.

## Run a team

Pass the same `context.environment` to all workers in the episode: updates by one worker affect what others see. Use `context.client` for every worker's model requests so all workers share the budget. Join workers before returning from `run`; evaluation then captures their final state.

Use a stable identifier for each agent in events and call records:

```python
from mac_bench.evaluation.episodes.identity import call_scope

agent_id = "planner"
context.event({"kind": "agent_start", "agent_id": agent_id})
try:
    with call_scope(agent_id=agent_id):
        # Run this agent's model and tool calls here.
        ...
finally:
    context.event({"kind": "agent_end", "agent_id": agent_id})
```

Enter `call_scope` inside each worker thread or asynchronous task. It associates that worker's calls with its identity. Team delegation and communication remain part of your system.

## Context reference

| Attribute | Use |
| --- | --- |
| `task.instruction` | User-facing task description |
| `task.interaction_instructions` | Instructions for using the environment |
| `task.tools` | Tools with names, descriptions and JSON argument schemas |
| `environment.call(name, arguments)` | Execute a tool and receive its structured result |
| `client.complete(messages, ...)` | Make a recorded model request |
| `budget` | Shared limits and current accounting |
| `event(payload)` | Append execution events |
| `artifacts` | Directory for adapter-specific outputs; create it before writing |

Return a dictionary or dataclass with your system's outcome, such as its final response or trajectory. Save larger method artifacts under `context.artifacts`.

The agent receives the public task interface. Reset operations, private tests and reference materials stay with the evaluator. To inspect the public interface before implementing an adapter, run `mac-bench evaluate --task-id TASK_ID --inspect`.

## Use another model client

`run_episode` accepts a client implementing `complete(messages, *, temperature, max_tokens, tools)`. It receives `ChatMessage` objects and returns a `ChatResponse`, including tool calls and provider-reported `prompt_tokens` and `completion_tokens`. The supplied `LiteLLMChatClient` handles chat-completions endpoints; a different client can implement the same [protocol](../src/mac_bench/runtime/llm/client.py).
