"""Agent-facing contracts; native runtime bindings never enter public documents."""
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable

from jsonschema import Draft202012Validator


class ToolError(Exception):
    def __init__(self, code, message, *, outcome='not_started'):
        super().__init__(message)
        self.code, self.message, self.outcome = code, message, outcome


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def object_schema(properties=None, required=(), *, additional=False):
    return {'type': 'object', 'properties': properties or {}, 'required': list(dict.fromkeys(required)),
            'additionalProperties': additional}


def validate(schema, value):
    try:
        encoded(value)
    except (TypeError, ValueError):
        raise ToolError('invalid_arguments', 'Arguments must be finite JSON values.') from None
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        # Explain how to repair the call using schema names, never input values
        # or jsonschema's raw message (which can echo a whole business record).
        issues = []
        def names(values):
            values = list(values)
            return ', '.join(str(x) for x in values[:12]) + ('; more in input documentation' if len(values) > 12 else '')
        for e in errors[:8]:
            detail = f'violates {e.validator}'
            if e.validator == 'required' and isinstance(e.instance, dict):
                detail = 'missing required properties: ' + names(
                    k for k in e.validator_value if k not in e.instance)
            elif e.validator == 'additionalProperties':
                detail = 'unexpected properties; allowed properties: ' + names(e.schema.get('properties', {}))
            elif e.validator == 'type':
                detail = 'expected type: ' + str(e.validator_value)
            elif e.validator == 'enum':
                detail = 'expected one of: ' + names(e.validator_value)
            issue = f"{'.'.join(map(str, e.path)) or '$'}: {detail}"
            if issue not in issues:
                issues.append(issue)
        raise ToolError('invalid_arguments', '; '.join(issues))


@dataclass(frozen=True)
class NativeResult:
    data: object
    status: str = 'succeeded'
    pagination: dict | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    application: str
    entity: str
    operation: str
    binding: str
    scope: dict
    description: str
    input_schema: dict
    output_description: str
    read_only: bool
    handler: Callable = field(repr=False, compare=False)
    notes: tuple[str, ...] = ()
    native_contract: dict = field(default_factory=dict)

    def summary(self):
        return {'name': self.name, 'application': self.application, 'entity': self.entity,
                'operation': self.operation, 'description': self.description, 'read_only': self.read_only}

    def describe(self):
        from mac_bench.construction.realize.tools.documentation import usage
        return usage(self)

    def archive(self):
        """Complete owner-side contract; never an agent discovery response."""
        return deepcopy({**self.summary(), 'input_schema': self.input_schema,
                         'output_description': self.output_description, 'notes': list(self.notes),
                         'native_contract': self.native_contract,
                         'retry_policy': 'Never automatically retry writes. Reuse request_id to recover this service receipt; read native state after an unknown outcome.'})
