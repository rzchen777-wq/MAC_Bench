"""Public task data and evaluator-owned lifecycle; no construction objects in model input."""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

CATEGORIES = frozenset({'business_finance', 'service_administration', 'project_content_operations',
                        'product_application', 'platform_data', 'developer_release'})
ENGINEERING = frozenset({'product_application', 'platform_data', 'developer_release'})


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    world_revision: str
    category: str
    split: str
    bundle: Path

    def __post_init__(self):
        if not isinstance(self.task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', self.task_id):
            raise ValueError('Task ID must be a safe directory/session component')
        if not isinstance(self.world_revision, str) or not self.world_revision:
            raise ValueError('Task identity and sealed revision are required')
        if self.category not in CATEGORIES or self.split not in {'dev', 'test'}:
            raise ValueError('Unknown task category or split')
        object.__setattr__(self, 'bundle', Path(self.bundle).resolve())


@dataclass(frozen=True, init=False)
class ToolDefinition:
    name: str
    description: str
    _input_json: str

    def __init__(self, name, description, input_schema):
        if not isinstance(name, str) or not name or not isinstance(description, str):
            raise ValueError('Tool name and description are required')
        object.__setattr__(self, 'name', name)
        object.__setattr__(self, 'description', description)
        object.__setattr__(self, '_input_json', json.dumps(input_schema, allow_nan=False))

    @property
    def input_schema(self):
        return json.loads(self._input_json)

    def as_function(self):
        return {'type': 'function', 'function': {'name': self.name, 'description': self.description,
                                                 'parameters': self.input_schema}}


@dataclass(frozen=True)
class TaskView:
    task_id: str
    world_revision: str
    instruction: str
    tools: tuple[ToolDefinition, ...]
    interaction_instructions: str

    def as_dict(self):
        return {'format': 'collaboration.task.v1', 'task_id': self.task_id,
                'world_revision': self.world_revision, 'instruction': self.instruction,
                'tools': [tool.as_function() for tool in self.tools],
                'interaction_instructions': self.interaction_instructions}


class TaskHandle(Protocol):
    """Owner-only API. Pass view and environment to a method, never this handle."""
    record: TaskRecord
    view: TaskView
    environment: Any
    is_open: bool

    def open(self): ...
    def assert_initial(self) -> Mapping[str, Any]: ...
    def reset(self) -> Mapping[str, Any]: ...
    def begin_episode(self, instance_id: str, *, admit_call: Callable, event: Callable): ...
    def end_episode(self): ...
    def observe(self) -> Mapping[str, Any]: ...
    def evaluate(self) -> Mapping[str, Any]: ...
    def export_receipts(self, destination: Path): ...
    def close(self): ...
