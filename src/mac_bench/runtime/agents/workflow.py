from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from mac_bench.runtime.types import PublicTask
from mac_bench.runtime.llm.client import (
    ChatClient,
    ChatMessage,
    ModelOutputTruncatedError,
    assistant_chat_message,
    trace_safe_content,
)
from mac_bench.runtime.types import SolverWorkspace
from mac_bench.runtime.tools import (
    WORKSPACE_TOOL_NAMES,
    ExecutionEnvironment,
    ProcessManager,
    ToolContext,
    ToolErrorCode,
    ToolExecutionError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    phase5_tool_registry,
)
from mac_bench.runtime.tools.protocol import (
    function_tool_schema,
    normalize_tool_calls,
    parse_function_call,
)
from mac_bench.runtime.tools.workspace import WorkspaceTools, backend_for_workspace
from .context import ContextPolicy, WorkingContext, ContextCapacityError, ContextBudgetStop


@dataclass(frozen=True)
class AgentBudget:
    """Multi-dimensional execution budget for one agent rollout."""

    max_model_calls: int = 12
    max_env_actions: int = 80
    max_tool_calls: int = 80
    max_tokens: int = 45000
    max_wall_seconds: float | None = None
    max_invalid_actions: int = 5
    max_repeated_actions: int = 4

@dataclass(frozen=True)
class AgentOutcome:
    agent_id: str
    summary: str
    trace: tuple[Mapping[str, Any], ...]
    input_tokens: int
    output_tokens: int
    model_calls: int
    tool_calls: int
    stop_reason: str = "unknown"
    artifacts: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class TeamOutcome:
    patch: str
    summaries: tuple[str, ...]
    trace: tuple[Mapping[str, Any], ...]
    input_tokens: int
    output_tokens: int
    model_calls: int
    tool_calls: int
    artifacts: tuple[Mapping[str, Any], ...] = ()


def _is_forbidden_offline_command(command: str) -> bool:
    return bool(
        re.search(
            r"(?:^|[;&|]\s*|\s)(?:python\s+-m\s+pip|pip\d*)\s+install\b|"
            r"(?:^|[;&|]\s*|\s)git\s+clone\b|"
            r"(?:^|[;&|]\s*|\s)(?:curl|wget)\b",
            command,
            flags=re.IGNORECASE,
        )
    )


def _normalize_react_action(action: str) -> str:
    return action.strip().lower()


def _parse_finish_payload(action_input: str) -> tuple[str, tuple[Mapping[str, Any], ...]]:
    """Parse Finish Action Input as plain summary or JSON with artifacts."""

    text = (action_input or "").strip()
    if not text:
        return "finished", ()
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text, ()
        if isinstance(payload, list):
            artifacts = tuple(item for item in payload if isinstance(item, Mapping))
            return "submitted artifacts", artifacts
        if isinstance(payload, dict):
            summary = str(
                payload.get("summary") or payload.get("content") or "finished"
            ).strip()
            raw_artifacts = payload.get("artifacts")
            if isinstance(raw_artifacts, list):
                artifacts = tuple(item for item in raw_artifacts if isinstance(item, Mapping))
                return summary or "submitted artifacts", artifacts
            if "artifact_type" in payload or "declared_outputs" in payload:
                return summary or "submitted artifact", (payload,)
            return summary or "finished", ()
    return text, ()


def _head_tail_truncate(text: str, *, limit: int) -> tuple[str, bool]:
    """mini-SWE-style head+tail truncation for model-facing observations."""

    if limit <= 0 or len(text) <= limit:
        return text, False
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    omitted = len(text) - head - tail
    truncated = (
        f"{text[:head]}\n"
        f"...[head+tail truncated {omitted} chars; use read/search or narrower output]...\n"
        f"{text[-tail:]}"
    )
    return truncated, True


def _react_tool_specs() -> tuple[str, ...]:
    workspace = (
        "Workspace tools: exec/process for commands, read/search for text, apply_patch for "
        "structured edits, read_csv for tables, and read_image when vision is available."
    )
    finish = (
        "finish: stop after completing the submission contract "
        "stated in the task. Action Input may be a short summary or JSON: "
        '{"summary":"...","artifacts":[{"artifact_id":"...","artifact_type":"...",'
        '"content":"...","declared_outputs":{...},"evidence_refs":["E1"]}]}. '
        "Workspace files and repository changes are collected by the harness."
    )
    return (workspace, finish)


def _react_system_prompt(*, network: str = "offline") -> str:
    tools = "\n".join(f"- {spec}" for spec in _react_tool_specs())
    if network == "open":
        environment = (
            "You are a ReAct agent solving one benchmark task inside a networked "
            "sandbox. Commands run inside the application workspace.\n"
        )
        layout = (
            "- You are already at the workspace root (`/app`). Product requirements "
            "are in the task text and also at `/problem/task.md` and "
            "`/problem/knowledge_base.json` (readable via exec). Write the product "
            "in `/app`; do not search `/work` or evaluator paths.\n"
        )
        net_rule = (
            "- Outbound network is available: apt, cargo, npm, pip, curl, and wget "
            "are allowed. Long builds should set exec timeout_ms (up to 600000). "
            "Do not git clone or import a pre-existing product repository; implement "
            "from the PRD. Leave the HTTP server running before Finish.\n"
        )
    else:
        environment = (
            "You are a ReAct agent solving one benchmark task inside an offline sandbox.\n"
        )
        layout = (
            "- You are already at the workspace root; never `cd` to /home, never `find /`, "
            "and never search outside `.`.\n"
        )
        net_rule = (
            "- The sandbox has no network; do not pip install, git clone, curl, or wget.\n"
        )
    return (
        environment
        + "The public task states the required work and may include an explicit submission "
        "contract; follow any such contract exactly. "
        "Use the files and README in the current workspace to understand its layout.\n"
        "When function tools are available, use them whenever they help. You may call "
        "multiple tools in one turn; they run sequentially in the returned order. "
        "If function tools are unavailable, emit exactly one step per turn in this format:\n"
        "Thought: <brief plan for this step>\n"
        "Action: <tool name>\n"
        "Action Input: <tool arguments>\n\n"
        f"Available tools:\n{tools}\n\n"
        "Rules:\n"
        "- Use only the provided workspace tools and Finish/Submit.\n"
        f"{layout}"
        "- Prefer targeted commands and relative paths; avoid dumping large files.\n"
        "- For repository edits, run focused visible tests before finishing when practical.\n"
        "- For artifact tasks, write the exact JSON artifact requested by the public task.\n"
        f"{net_rule}"
        "- Call Finish only after the requested output is in place."
    )


def _react_user_prompt(
    *,
    task: PublicTask,
    workspace: SolverWorkspace,
    memory: Sequence[str],
    network: str = "offline",
) -> str:
    env_label = (
        "networked workspace" if network == "open" else "offline workspace"
    )
    return (
        f"Task: {task.problem_statement}\n"
        f"Workspace / repository: {task.repo}\n"
        f"Environment: {env_label} at {workspace.cwd} (already the workspace root).\n"
        f"Prior memory (may be empty): {json.dumps(list(memory), ensure_ascii=False)}\n"
        "Begin the ReAct loop."
    )


def _react_tool_schemas(
    registry: Any | None = None,
    context: ToolContext | None = None,
) -> list[dict[str, Any]]:
    artifact_properties = {
        "artifact_id": {"type": "string"},
        "artifact_type": {"type": "string"},
        "content": {"type": "string"},
        "declared_outputs": {"type": "object"},
        "citation_refs": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    }
    workspace_schemas = registry.schemas(context) if registry is not None and context is not None else []
    return [
        *workspace_schemas,
        function_tool_schema(
            name="finish",
            description=(
                "End the task with a concise summary of completed work and any unresolved requirements. "
                "This reports the outcome; it does not perform remaining work. "
                "Structured artifacts may be supplied inline when required by the task."
            ),
            properties={
                "summary": {"type": "string"},
                "artifacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": artifact_properties,
                        "additionalProperties": True,
                    },
                },
                "thought": {"type": "string"},
            },
            required=("summary",),
        ),
    ]


def _continue_tool_arguments(partial, messages, complete_text, attempts):
    """Finish one interrupted JSON argument string without repeating its prefix."""
    from copy import deepcopy
    from dataclasses import replace

    calls = deepcopy(list(partial.tool_calls))
    incomplete = []
    for index, call in enumerate(calls):
        raw = call.get('function', {}).get('arguments')
        if not isinstance(raw, str):
            return None
        try:
            json.loads(raw)
        except ValueError:
            incomplete.append(index)
    # Multiple interrupted fields have ambiguous stream boundaries. Complete
    # earlier calls remain withheld until the final argument is finished.
    if incomplete != [len(calls) - 1]:
        return None
    target = calls[-1]['function']
    prefix = target['arguments']
    previous_error = ''
    for attempt in range(attempts):
        instruction = (
            'A function argument JSON object was interrupted. All pending tool calls were NOT executed; '
            'their generated bytes are not present on disk. Finish only the current argument object. '
            'Return plain text containing ONLY its missing suffix, starting exactly after the last '
            'character of the prefix below. Do not repeat the prefix or add Markdown fences. '
            'Your text will be appended to the fixed prefix, parsed and checked before any execution. '
            'Close the current command/code fragment and its JSON object; later work belongs in later '
            'complete tool calls. Do not emit another complete function call.\n'
            + previous_error + '\nFunction: ' + str(target.get('name'))
            + '\nBEGIN FIXED ARGUMENT PREFIX\n' + prefix + '\nEND FIXED ARGUMENT PREFIX')
        try:
            suffix = complete_text(list(messages) + [ChatMessage('user', instruction)])
        except ModelOutputTruncatedError as exc:
            if exc.response is None or exc.response.tool_calls or not exc.response.content:
                return None
            prefix += str(exc.response.content)
            continue
        if suffix.tool_calls:
            # Some backends may return complete replacement calls despite tools
            # being disabled. Let the normal registry validate those, never splice.
            if suffix.finish_reason in {'length', 'max_tokens'}:
                return None
            try:
                for call in suffix.tool_calls:
                    if not isinstance(json.loads(call['function']['arguments']), dict):
                        return None
            except (KeyError, TypeError, ValueError):
                return None
            return replace(suffix, usage={})
        combined = prefix + str(suffix.content or '')
        try:
            value = json.loads(combined)
        except ValueError as exc:
            previous_error = 'The last suffix did not produce valid JSON: ' + str(exc) + '. Try a new suffix.'
            continue
        if not isinstance(value, dict):
            return None
        target['arguments'] = combined
        return replace(partial, tool_calls=tuple(calls), usage={}, finish_reason='tool_calls')
    return None


def _complete_react(
    client: ChatClient,
    messages: Sequence[ChatMessage],
    *,
    tools: Sequence[Mapping[str, Any]],
    max_tokens: int,
    temperature: float = 0.0,
) -> Any:
    """Call the model with the canonical OpenAI-style tool schema."""

    return client.complete(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
    )


def _office_system_prompt() -> str:
    return (
        "You are closing one office matter through its live API. "
        "Every function is an operation on a live record: search_records lists a resource, "
        "show reads one record, update changes fields on an existing record, and create adds a record. "
        "Read a record before you change it. "
        "upstream_ids, when you set it, is a list of {resource, id} taken from records you have read. "
        "Call finish only after the public task is done."
    )


def _office_user_prompt(*, task: PublicTask) -> str:
    return (
        f"Task: {task.problem_statement}\n"
        "The records are already live. Use the API functions to read them and to make the required changes."
    )


def _react_step_from_tool_call(
    tool_call: Mapping[str, Any],
    *,
    extra_names: frozenset[str] = frozenset(),
    workspace: bool = True,
) -> dict[str, Any]:
    call = parse_function_call(tool_call)
    name = _normalize_react_action(call.name)
    args = call.arguments
    thought = call.thought
    workspace_names = {
        "exec",
        "process",
        "read",
        "search",
        "apply_patch",
        "read_image",
        "read_csv",
    }
    if (workspace and name in workspace_names) or name in extra_names:
        clean_args = {key: value for key, value in args.items() if key != "thought"}
        return {
            "thought": thought,
            "action": name,
            "action_input": json.dumps(clean_args, ensure_ascii=False),
            "arguments": clean_args,
            "raw_action": call.name,
        }
    if name == "finish":
        summary = str(args.get("summary") or args.get("content") or "").strip()
        artifacts = args.get("artifacts")
        finish_payload: dict[str, Any] = {"summary": summary or "finished"}
        if isinstance(artifacts, list):
            finish_payload["artifacts"] = [
                dict(item) for item in artifacts if isinstance(item, Mapping)
            ]
        return {
            "thought": thought,
            "action": "finish",
            "action_input": json.dumps(finish_payload, ensure_ascii=False),
            "raw_action": call.name,
        }
    raise ValueError(f"unknown tool call {call.name!r}")


def _append_unexecuted_react_tool_results(
    messages: list[ChatMessage],
    tool_calls: Sequence[Mapping[str, Any]],
    *,
    error: str,
) -> None:
    for tool_call in tool_calls:
        messages.append(
            ChatMessage(
                "tool",
                json.dumps(
                    {
                        "ok": False,
                        "error": error,
                    },
                    ensure_ascii=False,
                ),
                tool_call_id=str(tool_call.get("id") or ""),
            )
        )


_COMPACTED_TOOL_PREFIX = "[older tool result compacted"


def _compact_react_history(
    messages: list[ChatMessage],
    *,
    keep_recent_tool_results: int = 8,
    tool_result_limit: int = 160,
    assistant_limit: int = 320,
    tool_char_budget: int | None = None,
) -> dict[str, int]:
    tool_indexes = [index for index, message in enumerate(messages) if message.role == "tool"]
    retained = min(keep_recent_tool_results, len(tool_indexes))
    if tool_char_budget is not None:
        # Keep the usual recent-result floor, then retain more complete results
        # while they fit. Small native records should not disappear simply
        # because an agent has read more than eight objects. This is a character
        # target for tool history, not a model context/token limit.
        used = 0
        for distance, index in enumerate(reversed(tool_indexes), 1):
            content = trace_safe_content(messages[index].content)
            rendered = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
            if distance > retained and used + len(rendered) > tool_char_budget:
                break
            used += len(rendered)
            retained = max(retained, distance)
    old_indexes = tool_indexes[:-retained] if retained else tool_indexes
    old_call_ids: set[str] = set()
    tool_results = 0
    assistant_messages = 0
    chars_removed = 0

    tool_names: dict[str, str] = {}
    for message in messages:
        if message.role != "assistant":
            continue
        for call in message.tool_calls:
            function = call.get("function") if isinstance(call, Mapping) else None
            call_id = str(call.get("id") or "") if isinstance(call, Mapping) else ""
            if call_id and isinstance(function, Mapping):
                tool_names[call_id] = str(function.get("name") or "tool")

    for index in old_indexes:
        message = messages[index]
        call_id = str(message.tool_call_id or "")
        if call_id:
            old_call_ids.add(call_id)
        safe_content = trace_safe_content(message.content)
        rendered = (
            safe_content
            if isinstance(safe_content, str)
            else json.dumps(safe_content, ensure_ascii=False, default=str)
        )
        if rendered.startswith(_COMPACTED_TOOL_PREFIX) or len(rendered) <= tool_result_limit:
            continue
        preview, _ = _head_tail_truncate(rendered, limit=tool_result_limit)
        name = tool_names.get(call_id, "tool")
        replacement = (
            f"{_COMPACTED_TOOL_PREFIX}: {name}; tool_call_id={call_id}; "
            f"original_chars={len(rendered)}]\n{preview}"
        )
        messages[index] = ChatMessage("tool", replacement, tool_call_id=call_id)
        tool_results += 1
        chars_removed += max(0, len(rendered) - len(replacement))

    if not old_call_ids:
        return {
            "tool_results": 0,
            "assistant_messages": 0,
            "chars_removed": 0,
        }

    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        if message.has_reasoning:
            continue
        call_ids = {
            str(call.get("id") or "")
            for call in message.tool_calls
            if isinstance(call, Mapping)
        }
        if not call_ids or not call_ids.issubset(old_call_ids):
            continue
        compacted_calls: list[dict[str, Any]] = []
        changed = False
        for raw_call in message.tool_calls:
            call = dict(raw_call)
            function = call.get("function")
            if isinstance(function, Mapping):
                compacted_function = dict(function)
                arguments = compacted_function.get("arguments")
                rendered = (
                    json.dumps(arguments, ensure_ascii=False, default=str)
                    if isinstance(arguments, Mapping)
                    else str(arguments or "")
                )
                if len(rendered) > assistant_limit:
                    compacted_function["arguments"] = json.dumps(
                        {"_compacted": True, "original_chars": len(rendered)},
                        separators=(",", ":"),
                    )
                    chars_removed += len(rendered) - len(compacted_function["arguments"])
                    changed = True
                call["function"] = compacted_function
            compacted_calls.append(call)
        content = message.content
        if isinstance(content, str) and len(content) > assistant_limit:
            content, _ = _head_tail_truncate(content, limit=assistant_limit)
            chars_removed += len(message.content) - len(content)
            changed = True
        if changed:
            messages[index] = ChatMessage(
                "assistant",
                content,
                tool_calls=tuple(compacted_calls),
                reasoning=message.reasoning,
                reasoning_content=message.reasoning_content,
                reasoning_details=message.reasoning_details,
            )
            assistant_messages += 1

    return {
        "tool_results": tool_results,
        "assistant_messages": assistant_messages,
        "chars_removed": chars_removed,
    }


class ReactAgent:
    """Single-agent ReAct loop over the shared workspace tool registry."""

    def __init__(
        self,
        client: ChatClient,
        tools: WorkspaceTools,
        *,
        budget: AgentBudget | None = None,
        output_limit: int = 4000,
        model_max_tokens: int = 16_384,
        model_temperature: float = 0.0,
        network: str = "offline",
        extra_tools: Sequence[ToolSpec] | None = None,
        workspace_tools: bool = True,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        history_tool_char_budget: int | None = None,
        context_policy: ContextPolicy | None = None,
        completion_recovery_attempts: int = 2,
        completion_recovery_max_tokens: int = 32_768,
    ) -> None:
        self.client = client
        self.tools = tools
        self.budget = budget or AgentBudget(
            max_model_calls=30,
            max_env_actions=30,
            max_tool_calls=30,
            max_tokens=200000,
        )
        self.output_limit = output_limit
        self.model_max_tokens = max(1, int(model_max_tokens))
        if completion_recovery_attempts < 0 or completion_recovery_max_tokens < 1:
            raise ValueError('completion recovery attempts must be nonnegative and token cap positive')
        self.completion_recovery_attempts = int(completion_recovery_attempts)
        self.completion_recovery_max_tokens = max(self.model_max_tokens, int(completion_recovery_max_tokens))
        self.model_temperature = float(model_temperature)
        self.network = network
        self.process_manager = ProcessManager()
        self.extra_tools = tuple(extra_tools or ())
        self.workspace_tools = workspace_tools
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        if history_tool_char_budget is not None and history_tool_char_budget < 0:
            raise ValueError('history_tool_char_budget must be nonnegative')
        self.history_tool_char_budget = history_tool_char_budget
        self.context_policy = context_policy

    def run(
        self,
        *,
        task: PublicTask,
        workspace: SolverWorkspace,
        agent_id: str = "agent-1",
        memory: Sequence[str] = (),
        event_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> AgentOutcome:
        if self.workspace_tools:
            system = _react_system_prompt(network=self.network)
            prompt = _react_user_prompt(
                task=task, workspace=workspace, memory=memory, network=self.network
            )
        else:
            system = _office_system_prompt()
            prompt = _office_user_prompt(task=task)
        system = self.system_prompt if self.system_prompt is not None else system
        prompt = self.user_prompt if self.user_prompt is not None else prompt
        messages = [ChatMessage("system", system), ChatMessage("user", prompt)]
        environment = (ExecutionEnvironment(backend_for_workspace(self.tools, workspace), workspace)
                       if self.workspace_tools else None)
        registry = phase5_tool_registry(self.process_manager) if self.workspace_tools else ToolRegistry()
        for spec in self.extra_tools:
            registry.register(spec)
        extra_names = frozenset(spec.name for spec in self.extra_tools)
        capabilities = frozenset(getattr(self.client, "capabilities", frozenset()))
        workspace_names = (
            {"exec", "process", "read", "search", "apply_patch", "read_image", "read_csv"}
            if self.workspace_tools
            else set()
        )
        tool_context = ToolContext(
            environment=environment,
            agent_id=agent_id,
            sample_id=task.public_id,
            permissions=frozenset(workspace_names | set(extra_names)),
            capabilities=capabilities,
        )
        tool_schemas = _react_tool_schemas(registry, tool_context)
        trace: list[dict[str, Any]] = []
        command_history: list[str] = []
        input_tokens = output_tokens = model_calls = tool_calls = 0
        env_actions = invalid_actions = repeated_actions = 0
        summary = "model-call budget exhausted"
        stop_reason = "model_call_budget"
        submitted_artifacts: tuple[Mapping[str, Any], ...] = ()
        started = time.monotonic()
        pending_tool_calls: list[dict[str, Any]] = []
        active_step = -1
        response: Any = None
        recovery_attempts = 0
        request_output_tokens = self.model_max_tokens
        working_context = WorkingContext(self.context_policy) if self.context_policy else None

        def context_event(value):
            enriched = {**value, 'agent_id': agent_id, 'step': model_calls}
            trace.append(enriched)
            if event_callback:
                event_callback(enriched)

        def counted_text_request(batch, max_tokens, phase):
            nonlocal model_calls, input_tokens, output_tokens
            if model_calls >= self.budget.max_model_calls:
                raise ContextBudgetStop('model_call_budget')
            if input_tokens + output_tokens >= self.budget.max_tokens:
                raise ContextBudgetStop('token_budget')
            if self.budget.max_wall_seconds is not None and time.monotonic() - started >= self.budget.max_wall_seconds:
                raise ContextBudgetStop('wall_time_budget')
            # Same recorded client/model and shared budget; no helper provider.
            from mac_bench.evaluation.episodes.identity import call_scope
            # Failed summary attempts also consume the local call allowance;
            # otherwise a truncation retry could bypass a standalone ReAct budget.
            model_calls += 1
            try:
                with call_scope(agent_id=agent_id, role='solver', phase=phase):
                    value = self.client.complete(batch, temperature=0.0, max_tokens=max_tokens)
            except ModelOutputTruncatedError as exc:
                if exc.response is not None:
                    input_tokens += int(exc.response.usage.get('prompt_tokens', 0))
                    output_tokens += int(exc.response.usage.get('completion_tokens', 0))
                raise
            input_tokens += int(value.usage.get('prompt_tokens', 0))
            output_tokens += int(value.usage.get('completion_tokens', 0))
            context_event({'kind': phase, 'usage': dict(value.usage)})
            return value

        def summarize_context(batch, max_tokens):
            return counted_text_request(batch, max_tokens, 'context_summary')

        def complete_argument_suffix(batch):
            nonlocal recovery_attempts
            if working_context:
                working_context.prepare(batch, (), self.model_max_tokens, summarize_context, context_event)
            recovery_attempts += 1
            return counted_text_request(batch, self.model_max_tokens, 'completion_continuation')

        if working_context:
            from dataclasses import asdict
            context_event({'kind': 'context_policy', 'policy': asdict(self.context_policy),
                           'estimator': 'utf8_proxy_provider_calibrated'})

        def cancel_pending_tool_calls(error: str) -> None:
            if not pending_tool_calls:
                return
            _append_unexecuted_react_tool_results(
                messages,
                pending_tool_calls,
                error=error,
            )
            for tool_call in pending_tool_calls:
                trace.append(
                    {
                        "step": active_step,
                        "kind": "tool_not_executed",
                        "error": error,
                        "agent_id": agent_id,
                        "protocol": "tool_calls",
                        "tool_call_id": str(tool_call.get("id") or "") or None,
                        "assistant_tool_calls": [dict(tool_call)],
                    }
                )
            pending_tool_calls.clear()

        while pending_tool_calls or model_calls < self.budget.max_model_calls:
            if stop_reason == "finished" and not pending_tool_calls:
                break
            if pending_tool_calls:
                step = active_step
                turn_tool_calls = (pending_tool_calls.pop(0),)
                native_step = True
            else:
                if (
                    self.budget.max_wall_seconds is not None
                    and time.monotonic() - started >= self.budget.max_wall_seconds
                ):
                    summary = "wall-clock budget exhausted"
                    stop_reason = "wall_time_budget"
                    break
                if input_tokens + output_tokens >= self.budget.max_tokens:
                    summary = 'token budget exhausted'
                    stop_reason = 'token_budget'
                    break
                step = model_calls
                active_step = step
                if working_context:
                    try:
                        compaction = working_context.prepare(messages, tool_schemas, request_output_tokens,
                            summarize_context, context_event)
                    except ContextCapacityError as exc:
                        summary = str(exc)
                        stop_reason = exc.reason if isinstance(exc, ContextBudgetStop) else 'context_capacity'
                        context_event({'kind': 'context_error', 'error': str(exc)})
                        break
                    if compaction:
                        context_event(compaction)
                    if model_calls >= self.budget.max_model_calls or input_tokens + output_tokens >= self.budget.max_tokens:
                        summary = 'execution budget exhausted during context summary'
                        stop_reason = 'model_call_budget' if model_calls >= self.budget.max_model_calls else 'token_budget'
                        break
                    step = model_calls
                    active_step = step
                    compaction = None
                else:
                    compaction = _compact_react_history(messages, tool_char_budget=self.history_tool_char_budget)
                if compaction and (compaction["tool_results"] or compaction["assistant_messages"]):
                    trace.append(
                        {
                            "step": step,
                            "kind": "context_compaction",
                            "agent_id": agent_id,
                            **compaction,
                            "retained_recent_tool_results": 8,
                            "history_tool_char_budget": self.history_tool_char_budget,
                        }
                    )
                response = None
                response_already_counted = False
                try:
                    response = _complete_react(
                        self.client,
                        messages,
                        tools=tool_schemas,
                        max_tokens=request_output_tokens,
                        temperature=self.model_temperature,
                    )
                except ModelOutputTruncatedError as exc:
                    # The provider completed a billed request, even though its output is incomplete.
                    model_calls += 1
                    partial = exc.response
                    if partial is not None:
                        input_tokens += int(partial.usage.get('prompt_tokens', 0))
                        output_tokens += int(partial.usage.get('completion_tokens', 0))
                        if working_context:
                            working_context.tokens.observe(messages, tool_schemas, partial.usage)
                    context_event({'kind': 'model_output_truncated', 'error': str(exc),
                        'request_output_tokens': request_output_tokens,
                        'withheld_tool_calls': len(partial.tool_calls) if partial is not None else 0})
                    if partial is not None and recovery_attempts < self.completion_recovery_attempts:
                        try:
                            response = _continue_tool_arguments(partial, messages, complete_argument_suffix,
                                max(0, self.completion_recovery_attempts - recovery_attempts - 1))
                        except (ContextBudgetStop, ContextCapacityError) as error:
                            summary = str(error)
                            stop_reason = getattr(error, 'reason', 'context_capacity')
                            break
                        response_already_counted = response is not None
                    if response is None:
                        if partial is None or recovery_attempts >= self.completion_recovery_attempts:
                            summary = str(exc)
                            stop_reason = 'model_output_truncated'
                            break
                        # Even valid JSON can be a semantically incomplete tool call. Never execute
                        # calls from a length-stopped response or put them into the tool protocol.
                        # Exact draft continuation was already attempted above.
                        # A rejected or corrupted prefix must not anchor a fresh
                        # tool-enabled attempt. The recorder retains every billed
                        # fragment; the live conversation retains execution facts.
                        if partial.content and not partial.tool_calls:
                            messages.append(ChatMessage('assistant', partial.content))
                        recovery = (
                            'Your previous response reached its output token limit and was interrupted. '
                            'Any tool calls in that response were NOT executed. '
                            'Continue from the current environment state using complete tool arguments.')
                        if partial.tool_calls:
                            recovery += (
                                'The unsuccessful tool argument draft is retained in the experiment trace, '
                                'and was discarded from the active conversation. '
                                'For a long file write, issue a complete command for one bounded code chunk, '
                                'about 50–100 lines, then append later chunks in subsequent calls. '
                                'Stage unfinished long files and install them only after all chunks are written. '
                                'Do not reissue the entire large file. '
                                'Bytes from an unexecuted draft are not present on disk. '
                                'Inspect the current file if needed, then write a complete first chunk '
                                'unless a previous successful tool call already wrote it. '
                                'Never send a bare JSON suffix as a tool call.')
                        messages.append(ChatMessage('user', recovery))
                        recovery_attempts += 1
                        request_output_tokens = min(self.completion_recovery_max_tokens, request_output_tokens * 2)
                        context_event({'kind': 'completion_recovery', 'attempt': recovery_attempts,
                            'next_output_tokens': request_output_tokens})
                        continue
                except RuntimeError as exc:
                    invalid_actions += 1
                    messages.append(
                        ChatMessage(
                            "user",
                            f"LLM error: {exc}. Continue using the provided tools.",
                        )
                    )
                    trace.append({"step": step, "kind": "llm_error", "error": str(exc)})
                    if invalid_actions >= self.budget.max_invalid_actions:
                        summary = "invalid-action budget exhausted after LLM error"
                        stop_reason = "invalid_action_budget"
                        break
                    continue

                recovery_attempts = 0
                request_output_tokens = self.model_max_tokens
                if not response_already_counted:
                    model_calls += 1
                    input_tokens += int(response.usage.get("prompt_tokens", 0))
                    output_tokens += int(response.usage.get("completion_tokens", 0))
                    if working_context:
                        working_context.tokens.observe(messages, tool_schemas, response.usage)
                normalized_calls = normalize_tool_calls(
                    response.tool_calls,
                    agent_id=agent_id,
                    turn=step,
                )
                native_step = bool(normalized_calls)
                if native_step:
                    messages.append(assistant_chat_message(response, tool_calls=normalized_calls))
                    pending_tool_calls.extend(normalized_calls)
                    turn_tool_calls = (pending_tool_calls.pop(0),)
                else:
                    turn_tool_calls = ()

            if native_step:
                try:
                    parsed = _react_step_from_tool_call(
                        turn_tool_calls[0],
                        extra_names=extra_names,
                        workspace=self.workspace_tools,
                    )
                except ValueError as exc:
                    invalid_actions += 1
                    observation = f"Protocol error: {exc}"
                    messages.append(
                        ChatMessage(
                            "tool",
                            json.dumps({"ok": False, "error": observation}, ensure_ascii=False),
                            tool_call_id=str(turn_tool_calls[0].get("id") or ""),
                        )
                    )
                    trace.append(
                        {
                            "step": step,
                            "kind": "protocol_error",
                            "error": str(exc),
                            "assistant_tool_calls": [dict(item) for item in turn_tool_calls],
                            "protocol": "tool_calls",
                        }
                    )
                    if invalid_actions >= self.budget.max_invalid_actions:
                        summary = "invalid-action budget exhausted after protocol error"
                        stop_reason = "invalid_action_budget"
                        cancel_pending_tool_calls(
                            "not executed because the invalid-action budget was exhausted"
                        )
                        break
                    continue
            else:
                messages.append(assistant_chat_message(response))
                trace.append(
                    {
                        "step": step,
                        "kind": "thought",
                        "thought": response.content,
                        "agent_id": agent_id,
                        "protocol": "assistant_text",
                    }
                )
                continue

            thought = parsed["thought"]
            action_name = _normalize_react_action(parsed["action"])
            action_input = parsed["action_input"]
            tool_call_id = str(turn_tool_calls[0].get("id") or "") if turn_tool_calls else ""
            trace.append(
                {
                    "step": step,
                    "kind": "thought",
                    "thought": thought,
                    "agent_id": agent_id,
                    "protocol": "tool_calls",
                }
            )
            trace.append(
                {
                    "step": step,
                    "kind": "action",
                    "action": action_name,
                    "action_input": action_input,
                    "raw_action": parsed.get("raw_action", parsed["action"]),
                    "agent_id": agent_id,
                    "protocol": "tool_calls",
                    "tool_call_id": tool_call_id or None,
                }
            )

            if action_name == "finish":
                summary, submitted_artifacts = _parse_finish_payload(action_input)
                if not summary:
                    summary = thought or "finished"
                observation = f"Finished with summary: {summary}"
                if submitted_artifacts:
                    observation += f" ({len(submitted_artifacts)} artifact(s) submitted)"
                else:
                    observation += " (harness will collect the requested workspace output)"
                trace.append(
                    {
                        "step": step,
                        "kind": "finish",
                        "summary": summary,
                        "agent_id": agent_id,
                        "artifacts": [dict(item) for item in submitted_artifacts],
                        "protocol": "tool_calls",
                        "tool_call_id": tool_call_id or None,
                    }
                )
                trace.append(
                    {
                        "step": step,
                        "kind": "observation",
                        "observation": observation,
                        "agent_id": agent_id,
                        "protocol": "tool_calls",
                        "tool_call_id": tool_call_id or None,
                    }
                )
                if native_step:
                    messages.append(
                        ChatMessage(
                            "tool",
                            json.dumps(
                                {
                                    "ok": True,
                                    "finished": True,
                                    "summary": summary,
                                    "artifacts": [dict(item) for item in submitted_artifacts],
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                            tool_call_id=tool_call_id,
                        )
                    )
                stop_reason = "finished"
                if pending_tool_calls:
                    continue
                break

            if (self.workspace_tools and action_name in WORKSPACE_TOOL_NAMES) or action_name in extra_names:
                if native_step:
                    arguments = dict(parsed.get("arguments") or {})
                elif action_name == "exec":
                    arguments = {"command": action_input.strip()}
                else:
                    try:
                        decoded = json.loads(action_input or "{}")
                    except json.JSONDecodeError:
                        decoded = {}
                    arguments = dict(decoded) if isinstance(decoded, Mapping) else {}

                env_actions += 1
                if env_actions > self.budget.max_env_actions:
                    summary = "environment-action budget exhausted"
                    stop_reason = "env_action_budget"
                    cancel_pending_tool_calls(
                        "not executed because the environment-action budget was exhausted"
                    )
                    break
                if tool_calls >= self.budget.max_tool_calls:
                    summary = "tool-call budget exhausted"
                    stop_reason = "tool_call_budget"
                    cancel_pending_tool_calls(
                        "not executed because the tool-call budget was exhausted"
                    )
                    break

                fingerprint = json.dumps(
                    {"tool": action_name, "arguments": arguments},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if command_history and fingerprint == command_history[-1]:
                    repeated_actions += 1
                else:
                    repeated_actions = 0
                if repeated_actions >= self.budget.max_repeated_actions:
                    summary = "repeated-action budget exhausted"
                    stop_reason = "repeated_action_budget"
                    cancel_pending_tool_calls(
                        "not executed because the repeated-action budget was exhausted"
                    )
                    break
                command_history.append(fingerprint)

                if event_callback:
                    event_callback(
                        {
                            "kind": "worker_tool_start",
                            "agent_id": agent_id,
                            "tool": action_name,
                            "arguments": arguments,
                        }
                    )
                if (
                    self.network != "open"
                    and action_name == "exec"
                    and _is_forbidden_offline_command(
                        str(arguments.get("command") or "")
                    )
                ):
                    trace.append(
                        {
                            "step": step,
                            "kind": "policy_blocked",
                            "tool": "exec",
                            "arguments": arguments,
                            "policy": "offline_workspace",
                            "agent_id": agent_id,
                        }
                    )
                    tool_result = ToolResult.failed(
                        ToolExecutionError(
                            ToolErrorCode.PERMISSION_DENIED,
                            "command rejected by the offline sealed-workspace policy",
                        )
                    )
                else:
                    tool_result = registry.execute(action_name, arguments, tool_context)
                tool_calls += 1
                result_trace = tool_result.trace_dict()
                model_content = tool_result.openai_content()
                observation_truncated = False
                if isinstance(model_content, str):
                    model_content, observation_truncated = _head_tail_truncate(
                        model_content,
                        limit=self.output_limit,
                    )
                trace_observation = trace_safe_content(model_content)
                trace.append(
                    {
                        "step": step,
                        "kind": "tool",
                        "tool": registry.canonical_name(action_name),
                        "arguments": arguments,
                        "result": result_trace,
                        "agent_id": agent_id,
                        "protocol": "tool_calls",
                        "tool_call_id": tool_call_id or None,
                    }
                )
                trace.append(
                    {
                        "step": step,
                        "kind": "observation",
                        "tool": registry.canonical_name(action_name),
                        "observation": trace_observation,
                        "output_truncated": observation_truncated,
                        "agent_id": agent_id,
                        "protocol": "tool_calls",
                        "tool_call_id": tool_call_id or None,
                    }
                )
                if event_callback:
                    event_callback(
                        {
                            "kind": "worker_tool_finish",
                            "agent_id": agent_id,
                            "tool": registry.canonical_name(action_name),
                            "status": tool_result.status,
                        }
                    )
                if native_step:
                    messages.append(
                        ChatMessage(
                            "tool",
                            model_content,
                            tool_call_id=tool_call_id,
                        )
                    )
                else:
                    fallback_observation: Any
                    if isinstance(model_content, str):
                        fallback_observation = f"Observation:\n{model_content}"
                    else:
                        fallback_observation = [
                            {"type": "text", "text": "Observation:"},
                            *model_content,
                        ]
                    messages.extend(
                        [
                            ChatMessage("assistant", response.content),
                            ChatMessage("user", fallback_observation),
                        ]
                    )
                if input_tokens + output_tokens >= self.budget.max_tokens:
                    summary = "token budget exhausted after tool action"
                    stop_reason = "token_budget"
                    cancel_pending_tool_calls(
                        "not executed because the token budget was exhausted"
                    )
                    break
                continue

        return AgentOutcome(
            agent_id=agent_id,
            summary=summary,
            trace=tuple(trace),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_calls=model_calls,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            artifacts=submitted_artifacts,
        )


class ReactSingleWorkflow:
    """Single-agent OpenAI tool-use workflow."""

    def __init__(
        self,
        client: ChatClient,
        tools: WorkspaceTools,
        *,
        budget: AgentBudget | None = None,
        output_limit: int = 2500,
        model_max_tokens: int = 16_384,
        model_temperature: float = 0.0,
        network: str = "offline",
        extra_tools: Sequence[ToolSpec] | None = None,
        workspace_tools: bool = True,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        history_tool_char_budget: int | None = None,
        context_policy: ContextPolicy | None = None,
        completion_recovery_attempts: int = 2,
        completion_recovery_max_tokens: int = 32_768,
    ) -> None:
        self.client = client
        self.tools = tools
        self.extra_tools = tuple(extra_tools or ())
        self.workspace_tools = workspace_tools
        react_budget = budget or AgentBudget(
            max_model_calls=30,
            max_env_actions=80,
            max_tool_calls=80,
            max_tokens=200000,
        )
        self.react_agent = ReactAgent(
            client,
            tools,
            budget=react_budget,
            output_limit=output_limit,
            model_max_tokens=model_max_tokens,
            model_temperature=model_temperature,
            network=network,
            extra_tools=self.extra_tools,
            workspace_tools=self.workspace_tools,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            history_tool_char_budget=history_tool_char_budget,
            context_policy=context_policy,
            completion_recovery_attempts=completion_recovery_attempts,
            completion_recovery_max_tokens=completion_recovery_max_tokens,
        )

    def run_single(
        self,
        *,
        task: PublicTask,
        workspace: SolverWorkspace,
        memory: Sequence[str] = (),
        event_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> TeamOutcome:
        """Run a tool-enabled single-agent loop and return patch/artifacts."""

        outcome = self.react_agent.run(
            task=task,
            workspace=workspace,
            agent_id="agent-1",
            memory=memory,
            event_callback=event_callback,
        )
        return self._combine((outcome,), self.tools.collect_patch(workspace) if self.workspace_tools else "")

    @staticmethod
    def _combine(outcomes: Sequence[AgentOutcome], patch: str) -> TeamOutcome:
        trace = []
        for outcome in outcomes:
            for item in outcome.trace:
                enriched = dict(item)
                enriched.setdefault("agent_id", outcome.agent_id)
                trace.append(enriched)
            trace.append(
                {
                    "kind": "agent_stop",
                    "agent_id": outcome.agent_id,
                    "summary": outcome.summary,
                    "stop_reason": outcome.stop_reason,
                    "model_calls": outcome.model_calls,
                    "tool_calls": outcome.tool_calls,
                    "input_tokens": outcome.input_tokens,
                    "output_tokens": outcome.output_tokens,
                }
            )
        return TeamOutcome(
            patch=patch,
            summaries=tuple(outcome.summary for outcome in outcomes),
            trace=tuple(trace),
            input_tokens=sum(outcome.input_tokens for outcome in outcomes),
            output_tokens=sum(outcome.output_tokens for outcome in outcomes),
            model_calls=sum(outcome.model_calls for outcome in outcomes),
            tool_calls=sum(outcome.tool_calls for outcome in outcomes),
            artifacts=tuple(
                dict(item)
                for outcome in outcomes
                for item in outcome.artifacts
            ),
        )
