"""Schema-defined record views; the journal retains the full native result."""
from copy import deepcopy

from jsonschema import Draft202012Validator

from .contracts import ToolError


def _project(schema, value):
    if isinstance(value, dict) and 'properties' in schema:
        return {key: _project(spec, value[key])
                for key, spec in schema['properties'].items() if key in value}
    return deepcopy(value)


def overview_example(schema):
    """Use the field examples from the same definition as the runtime view."""
    if 'properties' in schema:
        return {key: overview_example(spec) for key, spec in schema['properties'].items()}
    return deepcopy(schema['examples'][0])


def record_overview(tool, data):
    specification = tool.native_contract.get('output', {}).get('record_overview')
    if not specification:
        return None
    result = _project(specification, data)
    if not Draft202012Validator(specification).is_valid(result):
        raise ToolError('response_schema_mismatch',
                        'The native detail does not match the documented overview schema. '
                        'The complete native response is preserved in this receipt.')
    return result if result != data else None
