"""Read existing reference objects even when they have no independent factory."""
from copy import deepcopy

from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.factories.contracts import NativeObject


def read_existing(runtime, obj):
    entities = runtime.registry.entities
    binding = entities.binding(obj.binding)
    if binding['entity'] != obj.entity:
        raise CatalogError('factory_parent_entity_binding_mismatch')
    app = entities.entity(obj.entity)['application']
    matches = [c for c in runtime.connections if c.application == app and c.scope == binding['scope']]
    if len(matches) != 1:
        raise CatalogError('factory_parent_connection_missing', binding=obj.binding)
    connection = matches[0]
    contract = entities.resolve(obj.entity, binding=obj.binding)['contract']
    if app == 'hrms':
        from mac_bench.environments.catalog.factories.apps.frappe import (
            CatalogFrappeClient,
        )
        data = CatalogFrappeClient(connection.transport).read(binding['native_resource'], obj.identity['name'])
    elif app == 'pretix':
        from mac_bench.environments.catalog.factories.apps.pretix import (
            render,
            routes,
        )
        candidates = []
        for name, operation in contract['operations'].items():
            if operation.get('action') != 'retrieve' or operation['contract_status'] != 'declared':
                continue
            for endpoint, path in routes(binding, name):
                for locator in contract['identity'].get('locators', []):
                    if (endpoint['view'] == locator['view'] and locator.get('field') in obj.identity
                            and set(locator.get('scope', [])) <= obj.identity.keys()):
                        candidates.append((path, locator))
        if not candidates:
            raise CatalogError('factory_parent_native_locator_missing', binding=obj.binding)
        path, locator = sorted(candidates, key=lambda x: x[0])[0]
        parameters = {**obj.identity, locator['parameter']: obj.identity[locator['field']]}
        native = connection.transport.request('GET', render(path, parameters))
        if native.get(locator['field']) != obj.identity[locator['field']]:
            raise CatalogError('factory_parent_identity_mismatch')
        data = {'record': native, 'scope': {k: obj.identity[k] for k in locator.get('scope', [])}}
    elif app == 'twenty':
        from graphql import get_named_type, is_enum_type, is_scalar_type

        from mac_bench.environments.apps.twenty.application import TwentyGraphQL
        from mac_bench.environments.catalog.relations.catalog import pages
        operation = contract['operations'].get('findOne')
        if operation is None:
            raise CatalogError('factory_parent_native_reader_missing', binding=obj.binding)
        library = operation['input']['library']
        # Parent projection needs identity only, but fetch all native scalar
        # fields so existing reference records remain useful to the caller.
        types = {r['name']: entities.type_definition(library, r['name'])['types'][r['name']]
                 for r in pages(entities.types, library=library)}
        document = {'__schema': {'types': list(types.values()), 'queryType': {'name': 'Query'},
                                'mutationType': {'name': 'Mutation'}, 'directives': []}}
        client = TwentyGraphQL(connection.transport, 'graphql', document)
        query_name = operation['native_name']
        query = client.schema.query_type.fields[query_name]
        result = get_named_type(query.type)
        fields = [name for name, field in result.fields.items()
                  if not field.args and (is_scalar_type(get_named_type(field.type)) or is_enum_type(get_named_type(field.type)))]
        filter_type = get_named_type(query.args['filter'].type)
        scalar = get_named_type(filter_type.fields['id'].type).fields['eq'].type
        query_text = f'query Parent($id: {str(scalar).rstrip("!")}!) {{ {query_name}(filter: {{id: {{eq: $id}}}}) {{ {" ".join(fields)} }} }}'
        data = client.query(query_text, {'id': obj.identity['id']})[query_name]
        if not isinstance(data, dict) or data.get('id') != obj.identity['id']:
            raise CatalogError('factory_parent_identity_mismatch')
    elif app == 'bigcapital' and obj.entity == 'bigcapital.uncategorized_bank_transaction':
        from mac_bench.environments.catalog.factories.apps.bigcapital import (
            render,
            route,
        )
        endpoint = route(binding, 'getUncategorizedTransaction')
        native = connection.transport.request('GET', render(endpoint['path_template'],
                    {'uncategorizedTransactionId': obj.identity['id']}))
        if not isinstance(native, dict) or native.get('id') != obj.identity['id']:
            raise CatalogError('factory_parent_identity_mismatch')
        data = {'record': native, 'scope': {}}
    else:
        raise CatalogError('factory_parent_native_reader_missing', binding=obj.binding)
    return NativeObject(obj.entity, obj.binding, deepcopy(obj.identity), data)
