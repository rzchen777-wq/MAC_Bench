"""Lossless structural fields and reference traversal for native aggregates.

Paths are tuples of property names and array indices; arrays retain order and
duplicates unless the source schema explicitly requires uniqueness. References
are converted only at declared leaves, never by searching arbitrary strings.
"""
import json
from copy import deepcopy

from jsonschema import Draft202012Validator

from mac_bench.environments.catalog.codecs import (
    convert_scalar,
    map_scalar_codecs,
)
from mac_bench.environments.errors import CompileFailure


def extract_field(schema, hint=None):
    hint = dict(hint or {})
    if set(hint) - {'target', 'unit', 'semantic_type', 'properties', 'items', 'codec'}:
        raise CompileFailure('unsupported_structural_field_annotation')
    if 'unit' in hint and (not isinstance(hint['unit'], str) or not hint['unit'].strip()):
        raise CompileFailure('field_unit_requires_explicit_nonempty_name')
    semantic = hint.get('semantic_type')
    if semantic is not None and (not isinstance(semantic, str) or semantic not in {
            'identity', 'reference', 'quantity', 'money', 'time', 'status', 'boolean', 'text'}):
        raise CompileFailure('unsupported_thin_semantic_type')
    schema = deepcopy(schema)
    if any(key in schema for key in ('$ref', 'oneOf', 'anyOf', 'allOf', 'if', 'patternProperties')):
        raise CompileFailure('structural_field_requires_resolved_unambiguous_schema')
    kind = schema.get('type')
    nullable = isinstance(kind, list) and 'null' in kind
    if isinstance(kind, list):
        nonnull = [item for item in kind if item != 'null']
        if len(nonnull) != 1:
            raise CompileFailure('structural_field_union_requires_binding')
        kind = nonnull[0]
    target = schema.get('x-reference-entity', hint.get('target'))
    dynamic = schema.get('x-reference-targets')
    discriminator = schema.get('x-reference-discriminator')
    if dynamic is not None or discriminator is not None:
        if (target or kind not in {'string', 'integer', 'number', 'object'} or not isinstance(dynamic, dict) or not dynamic
                or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                       for k, v in dynamic.items()) or not isinstance(discriminator, str) or not discriminator):
            raise CompileFailure('dynamic_reference_requires_discriminator_and_targets')
    if schema.get('x-reference-entity') and hint.get('target', target) != target:
        raise CompileFailure('reference_annotation_conflicts_with_schema')
    field = {'nullable': nullable, 'mutable': False, 'schema': schema}
    field.update({key: hint[key] for key in ('unit', 'semantic_type') if key in hint})
    # Native OpenAPI Number can describe a foreign ID (e.g. NestJS DTOs).
    # An explicit target still supplies the reference meaning; identity maps,
    # target readers and the original native schema constrain actual values.
    if kind in {'string', 'integer', 'number', 'object'} and (target or dynamic):
        if (target and (not isinstance(target, str) or not target)
                or 'properties' in hint or 'items' in hint):
            raise CompileFailure('invalid_reference_annotation')
        field['kind'] = 'reference'
        if dynamic:
            field.update(targets=deepcopy(dynamic), discriminator=discriminator)
        else:
            field['target'] = target
        if 'x-empty-reference' in schema:
            if schema['x-empty-reference'] != '':
                raise CompileFailure('unsupported_native_empty_reference')
            field['empty_reference'] = ''
    elif kind == 'object':
        if target or 'items' in hint:
            raise CompileFailure('object_reference_requires_leaf_binding')
        properties = schema.get('properties', {})
        annotations = hint.get('properties', {})
        if not isinstance(properties, dict) or not isinstance(annotations, dict) or set(annotations) - set(properties):
            raise CompileFailure('structural_property_annotation_missing_source')
        field.update(kind='object', properties={name: extract_field(value, annotations.get(name))
                                               for name, value in properties.items()})
        for child in field['properties'].values():
            selector = child.get('discriminator')
            if selector is not None and field['properties'].get(selector, {}).get('kind') != 'text':
                raise CompileFailure('dynamic_reference_selector_requires_sibling_text_field')
        additional = schema.get('additionalProperties', True)
        field['additional_properties'] = (extract_field(additional) if additional
                                           else True) if isinstance(additional, dict) else additional
    elif kind == 'array':
        if 'properties' in hint or 'prefixItems' in schema or not isinstance(schema.get('items'), dict):
            raise CompileFailure('structural_array_requires_homogeneous_item_schema')
        item_hint = dict(hint.get('items', {}))
        if target:
            if item_hint.get('target', target) != target:
                raise CompileFailure('array_reference_annotation_conflict')
            item_hint['target'] = target
        item = extract_field(schema['items'], item_hint)
        # Keep existing set operations only where the native schema proves set
        # semantics. Ordinary native child rows remain ordered arrays.
        is_set = schema.get('uniqueItems') is True and item['kind'] in {'text', 'reference'}
        field.update(kind='set' if is_set else 'array', items=item)
        if is_set and item.get('target'):
            field['target'] = item['target']
    elif kind in {'integer', 'number', 'boolean', 'string'}:
        if target or 'properties' in hint or 'items' in hint:
            raise CompileFailure('invalid_scalar_structural_annotation')
        field['kind'] = {'integer': 'number', 'number': 'decimal', 'boolean': 'boolean', 'string': 'text'}[kind]
        for key in ('minimum', 'maximum'):
            if key in schema:
                field[key] = schema[key]
    else:
        raise CompileFailure(f'unsupported_structural_field_type:{kind}')
    if 'enum' in schema:
        field['values'] = deepcopy(schema['enum'])
    if 'codec' in hint:
        codec = hint['codec']
        if (not isinstance(codec, dict) or set(codec) != {'kind', 'scale'}
                or codec['kind'] != 'decimal_string_minor_units'
                or type(codec['scale']) is not int or not 0 <= codec['scale'] <= 18
                or field['kind'] != 'text' or semantic != 'money' or 'unit' not in hint):
            raise CompileFailure('invalid_explicit_money_codec')
        field.update(kind='number', codec=deepcopy(codec))
        if 'values' in field:
            field['values'] = [convert_scalar(field, item, to_native=False) for item in field['values']]
    # Thin typing describes the observed representation; it never converts
    # amounts, timestamps or identities, and cannot override their wire type.
    compatible = {
        'identity': {'text', 'number', 'reference'},
        'reference': {'reference'},
        'quantity': {'number', 'decimal'},
        'money': {'number', 'decimal'},
        'time': {'text', 'number', 'decimal'},
        'status': {'text', 'number', 'boolean'},
        'boolean': {'boolean'},
        'text': {'text'},
    }
    if semantic is not None:
        if field['kind'] not in compatible[semantic]:
            raise CompileFailure('semantic_annotation_conflicts_with_native_type')
        if semantic == 'money' and 'unit' not in hint:
            raise CompileFailure('monetary_field_requires_explicit_unit')
        if semantic == 'time' and field['kind'] in {'number', 'decimal'} and 'unit' not in hint:
            raise CompileFailure('numeric_time_requires_explicit_unit')
    elif field['kind'] in {'reference', 'boolean', 'text'}:
        field['semantic_type'] = ('time' if field['kind'] == 'text'
                                  and schema.get('format') in {'date', 'date-time', 'time'}
                                  else field['kind'])
    return field


def symbolic_schema(field):
    """Keep source constraints, replacing native reference IDs with symbols."""
    schema = deepcopy(field['schema'])
    if 'codec' in field:
        result = {'type': ['integer', 'null'] if field.get('nullable') else 'integer'}
        if 'enum' in schema:
            result['enum'] = [convert_scalar(field, item, to_native=False) for item in schema['enum']]
        return result
    if field['kind'] == 'reference':
        # Native ID ranges/enum values describe allocated native identities,
        # not compiler symbols. Referential membership is checked separately.
        return {'type': ['string', 'null'] if field.get('nullable') else 'string',
                'minLength': 0 if 'empty_reference' in field else 1}
    if field['kind'] == 'object':
        schema['properties'] = {name: symbolic_schema(child) for name, child in field['properties'].items()}
        additional = field['additional_properties']
        if isinstance(additional, dict):
            schema['additionalProperties'] = symbolic_schema(additional)
    elif field['kind'] in {'array', 'set'}:
        schema['items'] = symbolic_schema(field['items'])
    return schema


def validate_structure(field, value):
    error = next(Draft202012Validator(symbolic_schema(field)).iter_errors(value), None)
    if error:
        raise CompileFailure(f'invalid_structural_value:{list(error.path)}:{error.validator}')
    map_scalar_codecs(field, value, to_native=True)


def map_references(field, value, resolve, path=(), *, record=None):
    """resolve(target_entity, identity, path); also usable to collect parents."""
    if value is None:
        return None
    kind = field['kind']
    if kind == 'reference':
        if 'empty_reference' in field and value == field['empty_reference']:
            return value
        target = field.get('target')
        if 'targets' in field:
            selector = record.get(field['discriminator']) if isinstance(record, dict) else None
            if not isinstance(selector, str) or selector not in field['targets']:
                raise CompileFailure(f'dynamic_reference_target_not_bound:{path}')
            target = field['targets'][selector]
        return resolve(target, value, path)
    if kind in {'array', 'set'}:
        child = field.get('items')
        if child is None:  # Existing hand-authored symbolic set contracts.
            child = {'kind': 'reference', 'target': field['target']} if field.get('target') else {'kind': 'text'}
        return [map_references(child, item, resolve, (*path, index), record=record)
                for index, item in enumerate(value)]
    if kind == 'object':
        result = {}
        for name, item in value.items():
            child = field['properties'].get(name, field.get('additional_properties'))
            result[name] = (map_references(child, item, resolve, (*path, name), record=value)
                            if isinstance(child, dict) else deepcopy(item))
        return result
    return deepcopy(value)


def reference_leaves(field, path=()):
    """Schema paths use '*' for each collection element, never row identities."""
    if field['kind'] == 'reference':
        if 'targets' in field:
            for target in sorted(set(field['targets'].values())):
                yield path, {**field, 'target': target}
        else:
            yield path, field
    elif field['kind'] in {'array', 'set'}:
        child = field.get('items')
        if child is not None:
            yield from reference_leaves(child, (*path, '*'))
        elif field.get('target'):
            yield path, field
    elif field['kind'] == 'object':
        for name, child in field['properties'].items():
            yield from reference_leaves(child, (*path, name))
        if isinstance(field.get('additional_properties'), dict):
            yield from reference_leaves(field['additional_properties'], (*path, '*'))


def relation_path(name, nested):
    return name if not nested else '/' + '/'.join(
        str(part).replace('~', '~0').replace('/', '~1') for part in (name, *nested))


def canonical_value(field, value):
    if value is None:
        return None
    if field['kind'] in {'array', 'set'}:
        child = field.get('items', {'kind': 'text'})
        result = [canonical_value(child, item) for item in value]
        return sorted(result, key=lambda item: json.dumps(item, sort_keys=True)) if field['kind'] == 'set' else result
    if field['kind'] == 'object':
        result = {}
        for name, item in value.items():
            child = field['properties'].get(name, field.get('additional_properties'))
            result[name] = canonical_value(child, item) if isinstance(child, dict) else deepcopy(item)
        return result
    return deepcopy(value)


def canonical_record(fields, row):
    return {'id': row['id'], **{name: canonical_value(spec, row[name]) for name, spec in fields.items()}}
