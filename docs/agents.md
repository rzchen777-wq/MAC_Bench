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

## Work with business records

Use `public/records.json` to locate initial records, then read their current values through the application tools. In `public/facts.json`, projected rows retain links to their source sections and conditions. Follow result cursors to read all pages; application pagination and saved-result pagination are separate.

`describe_tool` distinguishes list pages, document details and write confirmations. Financial write cards give the document ID and the operation for reading its current amounts. HRMS `docstatus` means draft, submitted or cancelled; it is separate from a business field named `status`. Calendar tools report the event and its relationships; reservations and staffing remain separate application records.

Pretix refund actions have distinct effects: `create` records a refund, `done` records completion, `process` acknowledges an external refund, and `order_payment.refund` executes a refund through the payment provider. Read the operation card for required states and effects on the parent order. The `mark_refunded` flag changes the order lifecycle; it does not itself transfer money. Use returned payment/refund states to check completion.

Give every independent application call a fresh `request_id`, including reads and calls from different workers. If a reply is lost, `read_result(request_id=original_id, path="")` retrieves its saved receipt. Check the receipt status and current record before deciding on another write. Calls to different applications can overlap; operations in the same application are serialized.

Adapters that use scripts can request a shared workspace with `context.open_execution()`. On business tasks, this creates a separate small sandbox containing the public files. Start an attributed process with `execution.call('process.spawn', arguments, public_tools=True)` inside `call_scope`; its Python scripts can use `from mac_tools import call, load_result`. These calls use the same public tools, return the same receipts, and count toward the same episode budget. Workers share workspace files and application state. The [script guide](../src/mac_bench/evaluation/tasks/public_tools.md) covers discovery, complete results and handovers; the [execution interface](../src/mac_bench/evaluation/tasks/execution.py) defines process and file operations.

## Context reference

| Attribute | Use |
| --- | --- |
| `task.instruction` | User-facing task description |
| `task.interaction_instructions` | Instructions for using the environment |
| `task.tools` | Tools with names, descriptions and JSON argument schemas |
| `environment.call(name, arguments)` | Execute a tool and receive its structured result |
| `open_execution()` | Open the team’s shared file and process workspace |
| `client.complete(messages, ...)` | Make a recorded model request |
| `budget` | Shared limits and current accounting |
| `event(payload)` | Append execution events |
| `artifacts` | Directory for adapter-specific outputs; create it before writing |

Return a dictionary or dataclass with your system's outcome, such as its final response or trajectory. Save larger method artifacts under `context.artifacts`.

The agent receives the public task interface. Reset operations, private tests and reference materials stay with the evaluator. To inspect the public interface before implementing an adapter, run `mac-bench evaluate --task-id TASK_ID --inspect`.

## Use another model client

`run_episode` accepts a client implementing `complete(messages, *, temperature, max_tokens, tools)`. It receives `ChatMessage` objects and returns a `ChatResponse`, including tool calls and provider-reported `prompt_tokens` and `completion_tokens`. The supplied `LiteLLMChatClient` handles chat-completions endpoints; a different client can implement the same [protocol](../src/mac_bench/runtime/llm/client.py).
