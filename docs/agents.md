# Connect an agent system

A system implements `run(context)`. The episode context provides:

| Attribute | Use |
| --- | --- |
| `task.instruction` | Read the task description |
| `task.tools` | Discover permitted tools and their argument schemas |
| `task.interaction_instructions` | Read environment usage instructions |
| `environment.call(name, arguments)` | Invoke a task tool |
| `client` | Make recorded model calls under the team budget |
| `event` | Record agent activity |
| `artifacts` | Save method-specific outputs |

Workers share `context.environment`. Join workers before returning from `run`; the evaluator then closes tool access, captures the candidate and scores it. Reset, private verification and reference materials are evaluator-owned operations.

The [ReAct adapter](../src/mac_bench/baselines/react_single/tasks.py) is a compact implementation example. A custom system can be passed directly to [run_episode](../src/mac_bench/evaluation/episodes/runner.py) without adding a CLI registry entry. The registry connects the four included method names to their adapters.

For collaboration analysis, emit agent-start and agent-end events with stable agent IDs and preserve those identities on tool calls. For comparable token budgets, use the provided recorded client or adapt your model calls to its usage-accounting interface.
