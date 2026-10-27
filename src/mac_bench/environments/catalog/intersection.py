"""Exact intersection for the homogeneous structural schema vocabulary.

Used after reference resolution. Unsupported applicators and contradictory
shapes require an explicit binding; they never become permissive replacements.
The captured source schema remains in catalog evidence unchanged.
"""
from copy import deepcopy

from mac_bench.environments.errors import CompileFailure

_LOWER = {'minimum', 'exclusiveMinimum', 'minLength', 'minItems', 'minProperties'}
_UPPER = {'maximum', 'exclusiveMaximum', 'maxLength', 'maxItems', 'maxProperties'}
_SPECIAL = {'type', 'properties', 'items', 'additionalProperties', 'required', 'enum'}
_SAME = {'const', 'multipleOf', 'pattern', 'format', 'default', 'contentEncoding', 'contentMediaType'}
_OR = {'uniqueItems', 'readOnly', 'writeOnly', 'deprecated'}
_ANNOTATIONS = {'title', 'description', 'example', 'examples'}
_TYPES = {'null', 'boolean', 'integer', 'number', 'string', 'object', 'array'}


def _equal(left, right):
    """JSON Schema equality: 1 == 1.0, but True != 1."""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _combine(values, *, allow_false=False):
    if any(value is False for value in values):
        if allow_false:
            return False
        raise CompileFailure('schema_intersection_forbids_structural_value')
    schemas = [value for value in values if value is not True]
    if not schemas:
        return True
    return intersect_schemas(schemas)


def intersect_schemas(schemas):
    """Keep every constraint, including per-branch additionalProperties scope.

    This is not OpenAPI inheritance/override: two data properties constrain the
    same value. A branch's additionalProperties schema also applies to properties
    introduced by another branch. Simply unioning properties loses that rule.
    """
    if not isinstance(schemas, list) or not schemas or any(not isinstance(v, dict) for v in schemas):
        raise CompileFailure('schema_intersection_requires_schema_objects')
    allowed = _LOWER | _UPPER | _SPECIAL | _SAME | _OR | _ANNOTATIONS
    for schema in schemas:
        if any(key not in allowed and not key.startswith('x-') for key in schema):
            raise CompileFailure('schema_intersection_applicator_requires_explicit_binding')
    result = {}
    keys = set().union(*(schema.keys() for schema in schemas))
    for key in sorted(keys - _SPECIAL):
        values = [schema[key] for schema in schemas if key in schema]
        if key in _LOWER | _UPPER:
            if any(type(value) not in (int, float) for value in values):
                raise CompileFailure('schema_intersection_numeric_constraint_invalid')
            result[key] = (max if key in _LOWER else min)(values)
        elif key in _OR:
            if any(type(value) is not bool for value in values):
                raise CompileFailure('schema_intersection_boolean_annotation_invalid')
            result[key] = any(values)
        elif key in {'title', 'description'}:
            if any(not isinstance(value, str) for value in values):
                raise CompileFailure('schema_intersection_text_annotation_invalid')
            result[key] = '\n\n'.join(dict.fromkeys(values))
        elif key in {'example', 'examples'}:
            # Non-validation annotations; preserve all supplied examples.
            for value in values:
                additions = value if key == 'examples' else [value]
                if not isinstance(additions, list):
                    raise CompileFailure('schema_intersection_examples_invalid')
                result.setdefault('examples', []).extend(deepcopy(additions))
        else:
            if any(not _equal(value, values[0]) for value in values[1:]):
                raise CompileFailure(f'schema_intersection_conflicting_constraint:{key}')
            result[key] = deepcopy(values[0])
    if 'type' in keys:
        candidates = set(_TYPES)
        for schema in schemas:
            if 'type' not in schema:
                continue
            values = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
            if not values or any(not isinstance(value, str) or value not in _TYPES for value in values):
                raise CompileFailure('schema_intersection_type_invalid')
            accepted = set(values)
            if 'number' in accepted:
                accepted.add('integer')
            candidates &= accepted
        if not candidates:
            raise CompileFailure('schema_intersection_incompatible_types')
        if 'number' in candidates:
            candidates.discard('integer')
        result['type'] = next(iter(candidates)) if len(candidates) == 1 else sorted(candidates)
    if 'required' in keys:
        required = []
        for schema in schemas:
            names = schema.get('required', [])
            if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                raise CompileFailure('schema_intersection_required_invalid')
            required.extend(names)
        result['required'] = sorted(set(required))
    if 'enum' in keys:
        enumerations = [schema['enum'] for schema in schemas if 'enum' in schema]
        if any(not isinstance(values, list) or not values for values in enumerations):
            raise CompileFailure('schema_intersection_enum_invalid')
        retained = [value for value in enumerations[0]
                    if all(any(_equal(value, candidate) for candidate in values)
                           for values in enumerations[1:])]
        if not retained:
            raise CompileFailure('schema_intersection_incompatible_enums')
        result['enum'] = deepcopy(retained)
    if 'properties' in keys:
        properties = [schema.get('properties', {}) for schema in schemas]
        if any(not isinstance(value, dict) for value in properties):
            raise CompileFailure('schema_intersection_properties_invalid')
        result['properties'] = {}
        for name in sorted(set().union(*(value.keys() for value in properties))):
            # Scope additionalProperties to each original branch, before union.
            constraints = [schema.get('properties', {}).get(name, schema.get('additionalProperties', True))
                           for schema in schemas]
            result['properties'][name] = _combine(constraints)
    if 'additionalProperties' in keys:
        result['additionalProperties'] = _combine(
            [schema.get('additionalProperties', True) for schema in schemas], allow_false=True)
    if 'items' in keys:
        result['items'] = _combine([schema['items'] for schema in schemas if 'items' in schema])
    return result
