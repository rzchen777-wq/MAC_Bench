"""Shared model-facing tools and OpenAI tool-call protocol helpers."""

from .protocol import (
    FunctionCall,
    function_tool_schema,
    normalize_tool_calls,
    parse_function_call,
)
from .catalog import (
    WORKSPACE_TOOL_NAMES,
    phase3_tool_registry,
    phase4_tool_registry,
    phase5_tool_registry,
    workspace_tool_schemas,
)
from .environment import ExecutionEnvironment, SafePath
from .patch import PatchHunk, PatchOperation, apply_patch_tool_spec, parse_patch
from .process import (
    ProcessManager,
    ProcessSession,
    exec_tool_schema,
    exec_tool_spec,
    process_tool_schema,
    process_tool_spec,
)
from .runtime import (
    ImageContent,
    TextContent,
    ToolContext,
    ToolCall,
    ToolErrorCode,
    ToolExecutionError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from .workspace import WorkspaceTools

__all__ = [
    "FunctionCall",
    "ExecutionEnvironment",
    "ImageContent",
    "PatchHunk",
    "PatchOperation",
    "ProcessManager",
    "ProcessSession",
    "SafePath",
    "TextContent",
    "ToolContext",
    "ToolCall",
    "ToolErrorCode",
    "ToolExecutionError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "WORKSPACE_TOOL_NAMES",
    "WorkspaceTools",
    "function_tool_schema",
    "normalize_tool_calls",
    "parse_function_call",
    "phase3_tool_registry",
    "phase4_tool_registry",
    "phase5_tool_registry",
    "apply_patch_tool_spec",
    "parse_patch",
    "exec_tool_spec",
    "exec_tool_schema",
    "process_tool_schema",
    "process_tool_spec",
    "workspace_tool_schemas",
]
