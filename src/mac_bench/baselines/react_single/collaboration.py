"""The existing single-agent ReAct loop adapted to live collaboration business APIs."""

from mac_bench.runtime.agents.workflow import AgentBudget, ReactSingleWorkflow
from mac_bench.runtime.agents.context import ContextPolicy
from mac_bench.runtime.types import PublicTask
from mac_bench.environments.tools.api import DESCRIPTIONS, SCHEMAS
from mac_bench.runtime.tools import ToolErrorCode, ToolExecutionError, ToolResult, ToolSpec

SYSTEM_PROMPT = """You are solving a task in live business applications.
The task describes the required final state and fixed facts you must preserve.
Use list_apps and list_tools to discover available operations. Use describe_tool to
read an operation's usage card. For complex inputs, inspect its input/fields sections
using the returned paths; examples show synthetic input shapes. call_tool.arguments
contains the operation input directly. Use call_tool to execute it. Read the
relevant records before changing them, and verify the resulting native state.
Native IDs identify records; names alone are not reliable identifiers. request_id
identifies one logical call: keep it unchanged if recovering a lost response, and
use a new ID for a different call. A rejected, accepted or unknown receipt does
not prove that the requested business change completed. Inspect native state.
The task's applications are the environment; you have no shell or grading access.
Large results are saved: use read_result and result_ref to recover exact fields or
continue a page without repeating the app call. Saved observations are historical;
use a new native read and new request_id when you need current state.
Call finish with a concise summary when done. Describing a solution without making
the required changes does not complete the task."""


class NativeWorldTools:
    """Workspace compatibility for the shared loop; no workspace operations registered."""

    def collect_patch(self, workspace):
        return ""


def tool_specs(agent_call):
    specs = []
    for name, schema in SCHEMAS.items():

        def handler(context, arguments, name=name):
            result = agent_call(name, dict(arguments))
            # RPC/schema failures are not completed business operations. Keep
            # native receipts (including unknown/accepted) intact for readback.
            if result.get('error') and ('status' not in result or result['status'] in {'rejected', 'error'}):
                error = result['error']
                code = (ToolErrorCode.INVALID_ARGUMENTS if error.get('code') == 'invalid_arguments'
                        else ToolErrorCode.EXECUTION_ERROR)
                return ToolResult.failed(ToolExecutionError(
                    code, error.get('message', 'World tool failed.'), details=result))
            return ToolResult.completed(result)

        specs.append(
            ToolSpec(
                name,
                DESCRIPTIONS[name],
                schema.get("properties", {}),
                tuple(schema.get("required", ())),
                handler,
            )
        )
    return specs


def run_world(
    client,
    task_text,
    *,
    world_id,
    workspace,
    agent_call,
    budget=None,
    event_callback=None,
    output_limit=60000,
    context_policy=None,
):
    if context_policy is None:
        from mac_bench.runtime.config import load_method_config
        context_policy = ContextPolicy(**load_method_config('react_single').get('context', {}))
    workflow = ReactSingleWorkflow(
        client,
        NativeWorldTools(),
        budget=budget
        or AgentBudget(
            max_model_calls=80,
            max_env_actions=160,
            max_tool_calls=160,
            max_tokens=400000,
            max_wall_seconds=2400,
        ),
        output_limit=output_limit,
        model_max_tokens=8192,
        workspace_tools=False,
        extra_tools=tool_specs(agent_call),
        system_prompt=SYSTEM_PROMPT,
        context_policy=context_policy,
    )
    return workflow.run_single(
        task=PublicTask(world_id, "native-business-apps", task_text),
        workspace=workspace,
        event_callback=event_callback,
    )
