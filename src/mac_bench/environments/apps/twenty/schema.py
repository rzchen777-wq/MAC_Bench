"""Derive a response projection from a validated query and captured schema.

Only unconditional selections are promised. Aliases, merged fragments,
nullability and ordered lists survive extraction. Custom scalar wire formats
require explicit bindings; GraphQL introspection alone does not describe them.
"""
from copy import deepcopy

from graphql import (
    Undefined,
    is_enum_type,
    is_input_object_type,
    is_list_type,
    is_non_null_type,
    is_scalar_type,
    parse,
)

from mac_bench.environments.errors import CompileFailure


def creation_arguments_schema(input_type, mapping, scalar_schemas):
    """Translate captured GraphQL input types, including recursive inputs.

    Response selections cannot describe mutation inputs. Keep the definitions
    at the operation-schema root so local references resolve after wrapping.
    Native GraphQL coercion remains the final authority before dispatch.
    """
    definitions = {}
    builtins = {'Int': {'type': 'integer', 'minimum': -(2**31), 'maximum': 2**31 - 1},
                'Float': {'type': 'number'}, 'String': {'type': 'string'},
                'ID': {'type': ['string', 'integer']}, 'Boolean': {'type': 'boolean'}}

    def convert(type_):
        non_null = is_non_null_type(type_)
        core = type_.of_type if non_null else type_
        if is_list_type(core):
            item = convert(core.of_type)
            # GraphQL input coercion also accepts a singleton for a list.
            result = {'anyOf': [{'type': 'array', 'items': item}, item]}
        elif is_input_object_type(core):
            if core.name not in definitions:
                definitions[core.name] = {}  # Break legitimate recursive input types.
                definitions[core.name] = {
                    'type': 'object', 'additionalProperties': False,
                    'properties': {name: convert(spec.type) for name, spec in core.fields.items()},
                    'required': [name for name, spec in core.fields.items()
                                 if is_non_null_type(spec.type) and spec.default_value is Undefined]}
            result = {'$ref': '#/$defs/' + core.name}
        elif is_enum_type(core):
            result = {'type': 'string', 'enum': list(core.values)}
        elif is_scalar_type(core):
            result = deepcopy(builtins.get(core.name, scalar_schemas.get(core.name)))
            if result is None:
                raise CompileFailure(f'twenty_input_scalar_wire_schema_required:{core.name}')
        else:
            raise CompileFailure('twenty_unsupported_graphql_input_type')
        if non_null:
            # A custom scalar contract, or singleton list coercion, can admit
            # null internally. NonNull always rejects null at this level.
            return {'allOf': [result, {'not': {'type': 'null'}}]}
        return {'anyOf': [result, {'type': 'null'}]}

    core = input_type.of_type if is_non_null_type(input_type) else input_type
    if not is_input_object_type(core):
        raise CompileFailure('twenty_catalog_create_requires_input_object')
    values = {'type': 'object', 'additionalProperties': False,
              'properties': {name: convert(core.fields[native].type) for name, native in mapping.items()},
              'required': [name for name, native in mapping.items()
                           if is_non_null_type(core.fields[native].type)
                           and core.fields[native].default_value is Undefined]}
    return {'type': 'object', 'properties': {'values': values}, 'required': ['values'],
            'additionalProperties': False, '$defs': definitions}


def response_schema(client, binding, scalar_schemas):
    operation = client.operation('query', binding.document, binding.operation_name)
    ast = parse(binding.document)
    fragments = {item.name.value: item for item in ast.definitions if item.kind == 'fragment_definition'}

    def applicable(condition, parent):
        return condition is None or condition.name.value in {
            parent.name, *(item.name for item in getattr(parent, 'interfaces', ())) }

    def fields(selection, parent, visiting=frozenset()):
        for node in selection.selections:
            if node.directives:
                continue
            if node.kind == 'field':
                yield node
            elif node.kind == 'inline_fragment' and applicable(node.type_condition, parent):
                yield from fields(node.selection_set, parent, visiting)
            elif node.kind == 'fragment_spread':
                name = node.name.value
                fragment = fragments[name]
                if name not in visiting and not fragment.directives and applicable(fragment.type_condition, parent):
                    yield from fields(fragment.selection_set, parent, visiting | {name})

    def typed(type_, selections):
        nullable = not is_non_null_type(type_)
        core = type_ if nullable else type_.of_type
        if is_list_type(core):
            result = {'type': 'array', 'items': typed(core.of_type, selections)}
        elif is_enum_type(core):
            result = {'type': 'string', 'enum': list(core.values)}
        elif is_scalar_type(core):
            builtins = {'String': {'type': 'string'}, 'ID': {'type': 'string'},
                        'Int': {'type': 'integer', 'minimum': -2147483648, 'maximum': 2147483647},
                        'Float': {'type': 'number'}, 'Boolean': {'type': 'boolean'}}
            result = deepcopy(builtins.get(core.name, scalar_schemas.get(core.name)))
            if not isinstance(result, dict) or not isinstance(result.get('type'), str):
                raise CompileFailure(f'twenty_scalar_wire_schema_required:{core.name}')
        else:
            result = object_schema(core, selections)
        if nullable:
            result['type'] = [result['type'], 'null']
            if 'enum' in result:
                result['enum'].append(None)
        return result

    def object_schema(parent, selections):
        grouped = {}
        for selection in selections:
            for node in fields(selection, parent):
                grouped.setdefault((node.alias or node.name).value, []).append(node)
        properties = {}
        for alias, nodes in grouped.items():
            name = nodes[0].name.value
            if name == '__typename':
                # Interface/union typename has a set of possible values, not
                # necessarily the declared parent's name.
                properties[alias] = {'type': 'string'}
                continue
            field = getattr(parent, 'fields', {}).get(name)
            if field is None:
                raise CompileFailure('twenty_selected_field_not_in_schema')
            properties[alias] = typed(field.type, [node.selection_set for node in nodes
                                                   if node.selection_set is not None])
            properties[alias]['x-graphql-field'] = name
            properties[alias]['x-graphql-parent'] = parent.name
        if not properties:
            raise CompileFailure('twenty_no_unconditional_object_projection')
        # Conditional and subtype-only fields may appear, but do not become
        # guaranteed fields in this projection.
        return {'type': 'object', 'properties': properties, 'required': list(properties),
                'additionalProperties': False}

    return object_schema(client.schema.query_type, [operation.selection_set])


def project_record(schema, value):
    """Select only schema-covered fields without fabricating missing/null data."""
    if value is None:
        return None
    kind = schema.get('type')
    if isinstance(kind, list):
        kind = next(item for item in kind if item != 'null')
    if kind == 'object':
        if not isinstance(value, dict) or any(name not in value for name in schema['properties']):
            raise CompileFailure('twenty_catalog_projection_missing_field')
        return {name: project_record(child, value[name]) for name, child in schema['properties'].items()}
    if kind == 'array':
        if not isinstance(value, list):
            raise CompileFailure('twenty_catalog_projection_requires_array')
        return [project_record(schema['items'], item) for item in value]
    return deepcopy(value)
