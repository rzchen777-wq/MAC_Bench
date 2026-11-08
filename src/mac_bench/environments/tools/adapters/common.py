"""Native protocol helpers shared by operation builders."""
import json
from copy import deepcopy
from urllib.parse import urlencode

from mac_bench.environments.tools.contracts import (
    NativeResult,
    ToolError,
    object_schema,
)

ID = {'type': ['string', 'integer'], 'minLength': 1}
QUERY = {'type': 'object', 'additionalProperties': {'type': ['string', 'number', 'boolean', 'array', 'null']}}


def query_path(path, query):
    # JSON-shaped filters are native query arguments, never SQL fragments.
    values = {k: json.dumps(v, separators=(',', ':')) if isinstance(v, (dict, list, bool)) else v
              for k, v in query.items() if v is not None}
    return path + ('?' + urlencode(values) if values else '')


def exchange(connection, method, path, body=None, *, rate_limit_key=None):
    options = {'rate_limit_key': rate_limit_key} if rate_limit_key is not None else {}
    response = connection.transport.request_response(method, path.lstrip('/'), body, **options)
    return NativeResult(response.body, 'accepted' if response.status == 202 else 'succeeded')


def merge_schema(properties, required=()):
    """Move each child's local definitions to the actual root without collisions."""
    defs = {}
    def move(value, prefix):
        if isinstance(value, list):
            return [move(v, prefix) for v in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for k, v in value.items():
            if k == '$defs':
                for key, item in v.items():
                    defs[prefix + key] = move(item, prefix)
            elif k == '$ref' and v.startswith('#/$defs/'):
                result[k] = '#/$defs/' + prefix + v[len('#/$defs/'):]
            else:
                result[k] = move(v, prefix)
        return result
    schema = object_schema({k: move(v, k + '_') for k, v in properties.items()}, required)
    if defs:
        schema['$defs'] = defs
    return schema


def fixed_scope(connection, parameters):
    result = dict(parameters)
    for key, value in connection.settings.get('path_parameters', {}).items():
        if key in result and result[key] != value:
            raise ToolError('scope_mismatch', 'The requested object is outside the bound native path scope.')
        result[key] = value
    if any(str(v) in {'.', '..'} for v in result.values()):
        raise ToolError('invalid_locator', 'Native path identities cannot be dot path segments.')
    return result
