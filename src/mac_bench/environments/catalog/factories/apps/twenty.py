"""Workspace-specific native GraphQL recipes with complete input type graphs."""
from copy import deepcopy

from graphql import (
    build_client_schema,
    get_named_type,
    is_enum_type,
    is_list_type,
    is_non_null_type,
    is_scalar_type,
)

from mac_bench.environments.apps.twenty.application import TwentyGraphQL
from mac_bench.environments.apps.twenty.factory import (
    GraphQLOperation,
    TwentyEntityFactory,
)
from mac_bench.environments.apps.twenty.schema import creation_arguments_schema
from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.factories.native import NativeAdapter

# Custom scalar constraints are enforced by the deployed GraphQL scalar, not
# invented from a SQL type. JSON may be any JSON value, including a scalar.
SCALARS = {'UUID': {'type': 'string'}, 'DateTime': {'type': 'string'}, 'Date': {'type': 'string'},
           'BigInt': {'type': ['integer', 'string']}, 'BigFloat': {'type': ['number', 'string']},
           'Position': {'type': 'number'}, 'JSON': {}, 'TSVector': {},
           'ConnectionCursor': {'type': 'string'}, 'Cursor': {'type': 'string'}}


def scalar_schema(type_):
    if is_enum_type(type_):
        return {'type': 'string', 'enum': list(type_.values)}
    types = {'Int': {'type': 'integer'}, 'Float': {'type': 'number'}, 'String': {'type': 'string'},
             'ID': {'type': 'string'}, 'Boolean': {'type': 'boolean'}, **SCALARS}
    if type_.name not in types:
        raise CatalogError('twenty_scalar_contract_missing', scalar=type_.name)
    return deepcopy(types[type_.name])


def recipes(context, binding):
    contract = context.contracts[binding['id']]
    ops = contract['operations']
    if 'createOne' not in ops or 'findOne' not in ops:
        return [], {'status': 'not_independently_creatable', 'reason': 'Native createOne/findOne pair absent'}
    library = ops['createOne']['input']['library']
    types = context.types(library)
    document = {'__schema': {'types': list(types.values()), 'queryType': {'name': 'Query'},
                            'mutationType': {'name': 'Mutation'}, 'subscriptionType': None, 'directives': []}}
    if not hasattr(context, '_graphql_schemas'):
        context._graphql_schemas = {}
    if library not in context._graphql_schemas:
        context._graphql_schemas[library] = build_client_schema(document)
    schema = context._graphql_schemas[library]
    create_name, read_name = ops['createOne']['native_name'], ops['findOne']['native_name']
    create = schema.mutation_type.fields[create_name]
    read = schema.query_type.fields[read_name]
    if 'data' not in create.args or 'filter' not in read.args:
        raise CatalogError('twenty_factory_native_argument_shape_changed', binding=binding['id'])
    input_type = create.args['data'].type
    root_input = get_named_type(input_type)
    parameters = creation_arguments_schema(input_type, {key: key for key in root_input.fields}, SCALARS)
    native_input = {**parameters['properties']['values'], '$defs': parameters.get('$defs', {})}
    result_type = get_named_type(read.type)
    relation_fields = {name for name, field in contract['fields'].items() if field.get('reference')}

    def selection(type_, ancestry=(), root=False):
        properties, selections = {}, []
        for name, field in type_.fields.items():
            core = get_named_type(field.type)
            if field.args or (root and name in relation_fields and not is_scalar_type(core)):
                continue
            if is_scalar_type(core) or is_enum_type(core):
                subschema, suffix = scalar_schema(core), ''
            elif core.name in ancestry or core.name.endswith('Connection') or not hasattr(core, 'fields'):
                continue
            else:
                suffix, subschema = selection(core, (*ancestry, type_.name))
                if not suffix:
                    continue
                suffix = ' { ' + suffix + ' }'
            def wrap(wrapped):
                if is_non_null_type(wrapped):
                    return body(wrapped.of_type)
                return {'anyOf': [body(wrapped), {'type': 'null'}]}
            def body(wrapped):
                if is_list_type(wrapped):
                    return {'type': 'array', 'items': wrap(wrapped.of_type)}
                return deepcopy(subschema)
            properties[name] = wrap(field.type)
            selections.append(name + suffix)
        return ' '.join(selections), {'type': 'object', 'properties': properties, 'required': list(properties)}
    selected, output = selection(result_type, root=True)
    identity_fields = contract['identity']['fields']
    if identity_fields != ['id'] or 'id' not in output['properties']:
        raise CatalogError('twenty_factory_identity_contract_changed')
    filter_type = get_named_type(read.args['filter'].type)
    id_filter = get_named_type(filter_type.fields['id'].type)
    if 'eq' not in id_filter.fields:
        raise CatalogError('twenty_factory_identity_filter_missing')
    id_type = id_filter.fields['eq'].type
    id_type_text = str(id_type).rstrip('!') + '!'
    create_doc = f'mutation Create($values: {input_type}) {{ {create_name}(data: $values) {{ id }} }}'
    read_doc = f'query Read($id: {id_type_text}) {{ {read_name}(filter: {{id: {{eq: $id}}}}) {{ {selected} }} }}'
    parents, projections = {}, {}
    for name, field in contract['fields'].items():
        ref = field.get('reference', {})
        if len(ref.get('targets', [])) != 1 or ref.get('cardinality') != 'one':
            continue
        # GraphQL exposes explicit foreign-key inputs; don't invent connect DSLs.
        native_id = name + 'Id'
        if native_id not in root_input.fields:
            continue
        target = context.target(ref['targets'][0], binding['id'])
        parents[name] = {'entity': target['entity'], 'binding': target['id'], 'required': False, 'many': False}
        projections[name] = {'identity': 'id', 'path': ['values', native_id], 'read_field': native_id}
    recipe = {'kind': 'twenty', 'library': library,
              'create_document': create_doc, 'read_document': read_doc,
              'create_result': create_name, 'read_result': read_name,
              'read_fields': list(output['properties']), 'identity_fields': ['id'],
              'input_schema': native_input, 'parent_projection': projections}
    spec = context.spec(binding, 'createOne', 'findOne', recipe=recipe,
                        parameters=parameters, output=output, parents=parents)
    return [spec], {'status': 'factory', 'creation': 'native workspace GraphQL createOne'}


class TwentyAdapter(NativeAdapter):
    def __init__(self, spec, connection, runtime):
        super().__init__(spec, connection, runtime)
        key = ('twenty_client', self.recipe['library'], id(connection.transport))
        if not hasattr(runtime, 'clients'):
            runtime.clients = {}
        self.client = runtime.clients.setdefault(key, None)
        if self.client is None:
            from mac_bench.environments.catalog.relations.catalog import pages
            entities = runtime.registry.entities
            library = self.recipe['library']
            types = [entities.type_definition(library, row['name'])['types'][row['name']]
                     for row in pages(entities.types, library=library)]
            document = {'__schema': {'types': types, 'queryType': {'name': 'Query'},
                                    'mutationType': {'name': 'Mutation'}, 'directives': []}}
            self.client = TwentyGraphQL(self.transport, 'graphql', document)
            runtime.clients[key] = self.client
        self.factory = TwentyEntityFactory(self.client,
            create=GraphQLOperation(self.recipe['create_document'], (self.recipe['create_result'],)),
            read=GraphQLOperation(self.recipe['read_document'], (self.recipe['read_result'],)),
            required_read_paths=tuple((name,) for name in self.recipe['read_fields']))

    def create(self, payload, parents):
        op = self.factory.create_operation
        # Preserve the actual mutation receipt before the facade independently
        # reads it; no hidden read inside the create phase can lose its identity.
        return op.result(self.client.mutate(op.document, {'values': payload['values']}))

    def read(self, identity, parents):
        return self.factory.read(identity['id'])


adapter = TwentyAdapter
