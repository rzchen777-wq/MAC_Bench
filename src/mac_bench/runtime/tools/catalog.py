from __future__ import annotations

from typing import Any

from .csv import read_csv_tool_spec
from .image import read_image_tool_spec
from .patch import apply_patch_tool_spec
from .process import (
    ProcessManager,
    exec_tool_schema,
    exec_tool_spec,
    process_tool_schema,
    process_tool_spec,
)
from .read import read_tool_spec
from .runtime import ToolRegistry
from .search import search_tool_spec

WORKSPACE_TOOL_NAMES = (
    "exec",
    "process",
    "read",
    "search",
    "apply_patch",
    "read_image",
    "read_csv",
)

def workspace_tool_schemas(*, image_input: bool) -> list[dict[str, Any]]:
    schemas = [
        exec_tool_schema(),
        process_tool_schema(),
        read_tool_spec().schema(),
        search_tool_spec().schema(),
        apply_patch_tool_spec().schema(),
        read_csv_tool_spec().schema(),
    ]
    if image_input:
        schemas.insert(-1, read_image_tool_spec().schema())
    return schemas


def phase3_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(read_tool_spec())
    registry.register(search_tool_spec())
    registry.register(read_csv_tool_spec())
    registry.register(read_image_tool_spec())
    return registry


def phase4_tool_registry() -> ToolRegistry:
    registry = phase3_tool_registry()
    registry.register(apply_patch_tool_spec())
    return registry


def phase5_tool_registry(manager: ProcessManager | None = None) -> ToolRegistry:
    process_manager = manager or ProcessManager()
    registry = ToolRegistry()
    registry.register(exec_tool_spec(process_manager))
    registry.register(process_tool_spec(process_manager))
    registry.register(read_tool_spec())
    registry.register(search_tool_spec())
    registry.register(apply_patch_tool_spec())
    registry.register(read_image_tool_spec())
    registry.register(read_csv_tool_spec())
    return registry
