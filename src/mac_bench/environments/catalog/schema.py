"""Resolve captured schema references without inventing or dropping constraints."""
import re
from copy import deepcopy

from mac_bench.environments.catalog.intersection import intersect_schemas
from mac_bench.environments.errors import CompileFailure


def _pointer(document, pointer):
    node = document
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise CompileFailure('catalog_requires_local_json_pointer')
    try:
        for token in pointer[1:].split('/'):
            if re.search(r'~(?![01])', token):
                raise ValueError('invalid pointer escape')
            token = token.replace('~1', '/').replace('~0', '~')
            if isinstance(node, list):
                if not re.fullmatch(r'0|[1-9][0-9]*', token):
                    raise ValueError('invalid array index')
                node = node[int(token)]
            else:
                node = node[token]
    except (KeyError, TypeError, IndexError, ValueError):
        raise CompileFailure(f'catalog_source_pointer_missing:{pointer}') from None
    return node



def _resolve(document, schema):
    seen = set()
    while '$ref' in schema:
        ref = schema['$ref']
        if not ref.startswith('#/') or ref in seen:
            raise CompileFailure('external_or_cyclic_schema_reference')
        seen.add(ref)
        schema = _pointer(document, ref[1:])
    return schema



def _structural_schema(document, schema, ancestry=()):
    if not isinstance(schema, dict):
        raise CompileFailure('catalog_field_requires_schema_object')
    schema = deepcopy(schema)
    if '$ref' in schema:
        ref = schema['$ref']
        if not isinstance(ref, str) or not ref.startswith('#/') or ref in ancestry:
            raise CompileFailure('external_or_recursive_aggregate_schema_requires_binding')
        if set(schema) - {'$ref', 'description', 'title'}:
            raise CompileFailure('schema_reference_siblings_require_explicit_binding')
        return _structural_schema(document, _pointer(document, ref[1:]), (*ancestry, ref))
    if 'allOf' in schema:
        branches = schema.pop('allOf')
        if not isinstance(branches, list) or not branches:
            raise CompileFailure('schema_allof_requires_nonempty_branches')
        # Resolve each branch without redefining properties introduced elsewhere.
        # The intersection keeps additionalProperties scoped to its own branch.
        return intersect_schemas([_structural_schema(document, branch, ancestry)
                                  for branch in [schema, *branches]])
    if schema.pop('nullable', False):
        kind = schema.get('type')
        if isinstance(kind, str):
            schema['type'] = [kind, 'null']
        elif isinstance(kind, list):
            schema['type'] = list(dict.fromkeys([*kind, 'null']))
        else:
            raise CompileFailure('nullable_field_requires_explicit_type')
    if 'properties' in schema:
        schema['properties'] = {name: _structural_schema(document, child, ancestry)
                                for name, child in schema['properties'].items()}
    for key in ('items', 'additionalProperties'):
        if isinstance(schema.get(key), dict):
            schema[key] = _structural_schema(document, schema[key], ancestry)
    return schema
