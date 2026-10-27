"""Existing ReAct method over the public task-layer contract, across both world families."""
from mac_bench.runtime.agents.workflow import ReactSingleWorkflow
from mac_bench.runtime.types import PublicTask
from mac_bench.runtime.types import SolverWorkspace
from mac_bench.runtime.tools import ToolSpec, ToolResult, ToolExecutionError, ToolErrorCode
from .collaboration import NativeWorldTools
from mac_bench.runtime.agents.context import ContextPolicy


class ReactSingle:
    name = 'react_single'

    def __init__(self, config=None, *, context_policy=None):
        import yaml
        from pathlib import Path
        from mac_bench.baselines.common.config import SPECS
        self.spec = yaml.safe_load(Path(config or SPECS/'react_single.yaml').read_text())
        if self.spec.get('method') != self.name:
            raise ValueError('Expected a react_single method configuration')
        if context_policy is None:
            context_policy = ContextPolicy(**self.spec.get('context', {}))
        self.context_policy = context_policy

    def run(self, context):
        tools = []
        for definition in context.task.tools:
            schema = definition.input_schema

            def invoke(tool_context, arguments, name=definition.name):
                result = context.environment.call(name, dict(arguments))
                if result.get('error') and result.get('status') not in {'accepted', 'unknown'}:
                    error = result['error']
                    return ToolResult.failed(ToolExecutionError(ToolErrorCode.EXECUTION_ERROR,
                        error.get('message', 'Task tool failed'), details=result))
                return ToolResult.completed(result)

            tools.append(ToolSpec(definition.name, definition.description, schema.get('properties', {}),
                tuple(schema.get('required', ())), invoke,
                additional_properties=schema.get('additionalProperties', False)))
        workflow = ReactSingleWorkflow(context.client, NativeWorldTools(), budget=context.budget.limits,
            workspace_tools=False, extra_tools=tools,
            system_prompt=context.task.interaction_instructions + '\nUse finish to submit your final report.',
            user_prompt=context.task.instruction, output_limit=60000,
            model_max_tokens=self.spec['model_parameters']['max_tokens'],
            model_temperature=self.spec['model_parameters']['temperature'],
            context_policy=self.context_policy,
            completion_recovery_attempts=self.spec.get('completion_recovery', {}).get('max_attempts', 2),
            completion_recovery_max_tokens=self.spec.get('completion_recovery', {}).get('max_output_tokens', 32768))
        # This placeholder is loop compatibility only. No sandbox capability is supplied.
        context.event({'kind': 'agent_start', 'agent_id': 'agent-1'})
        try:
            return workflow.run_single(task=PublicTask(context.task.task_id, 'collaboration-task', context.task.instruction),
                workspace=SolverWorkspace(None, cwd='.', kind='artifact'), event_callback=context.event)
        finally:
            context.event({'kind': 'agent_end', 'agent_id': 'agent-1'})
