"""Serializer-backed Pretix factories, including nested and order lifecycle entities."""
import re
from copy import deepcopy
from urllib.parse import quote

from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.factories.apps.drf import schema_for
from mac_bench.environments.catalog.factories.native import (
    NativeAdapter,
    identity_output,
    object_schema,
    parameter_schema,
)


def route(binding):
    path = binding['path']
    if '(?P<format>' in path:
        return None
    path = re.sub(r'\(\?P<([^>]+)>[^)]+\)', lambda m: '{' + m[1] + '}', path)
    path = path.replace('^', '').removesuffix('$')
    if any(c in path for c in ('(', ')', '[', ']', '\\', '?')):
        raise CatalogError('pretix_factory_route_unimplemented', path=binding['path'])
    return path


def routes(binding, operation):
    return [(row, path) for row in binding['operation_bindings'][operation] if (path := route(row))]


def render(path, parameters):
    def replace(match):
        key = match[1]
        if key not in parameters or not isinstance(parameters[key], (str, int)) or isinstance(parameters[key], bool):
            raise CatalogError('pretix_factory_scope_missing', parameter=key)
        return quote(str(parameters[key]), safe='')
    return re.sub(r'\{([^}]+)\}', replace, path)


def recipes(context, binding):
    contract = context.contracts[binding['id']]
    ops = contract['operations']
    if binding['entity'] in {'pretix.checkin', 'pretix.gift_card_transaction'}:
        from mac_bench.environments.catalog.factories.apps.pretix_lifecycle import (
            recipes as lifecycle_recipes,
        )
        return lifecycle_recipes(context, binding)
    if binding['entity'] == 'pretix.invoice':
        return invoice_recipe(context, binding)
    specs = []
    for create_name, create in ops.items():
        if create.get('action') != 'create' or create['contract_status'] != 'declared':
            continue
        create_routes = routes(binding, create_name)
        if len(create_routes) != 1:
            raise CatalogError('pretix_factory_create_route_ambiguous', operation=create_name)
        native_create, create_path = create_routes[0]
        create_scope = set(re.findall(r'\{([^}]+)\}', create_path))
        candidates = []
        for read_name, read in ops.items():
            if read.get('action') != 'retrieve' or read['contract_status'] != 'declared':
                continue
            for native_read, read_path in routes(binding, read_name):
                for locator in contract['identity'].get('locators', []):
                    if locator['view'] == native_read['view'] and locator.get('field'):
                        path_keys = list(dict.fromkeys(re.findall(r'\{([^}]+)\}', read_path)))
                        if locator.get('parameter') not in path_keys:
                            continue
                        # A view may expose both organizer-wide and event-scoped
                        # routes. Its locator scope is not a scope for every URL.
                        read_scope = [key for key in path_keys if key != locator['parameter']]
                        bound_locator = {**locator, 'scope': read_scope}
                        score = (native_read['view'] != native_create['view'],
                                 len(set(read_scope) - create_scope),
                                 not read_path.startswith(create_path))
                        candidates.append((score, len(read_scope), read_name,
                                           native_read, read_path, bound_locator))
        if not candidates:
            raise CatalogError('pretix_factory_read_locator_missing', entity=binding['entity'], operation=create_name)
        _, _, read_name, native_read, read_path, locator = sorted(candidates, key=lambda v: v[:3])[0]
        # CloneEventViewSet inherits synthetic detail routes but EventSerializer
        # is the canonical reader for the newly created event.
        if 'CloneEvent' in native_create['view']:
            choice = [c for c in candidates if c[3]['view'].endswith('.EventViewSet')]
            if not choice:
                raise CatalogError('pretix_clone_read_binding_missing')
            _, _, read_name, native_read, read_path, locator = choice[0]
        serializers = create['input'].get('serializers', [])
        if len(serializers) != 1 and not (binding['entity'] == 'pretix.order_position' and len(serializers) == 2):
            raise CatalogError('pretix_factory_input_serializer_ambiguous', entity=binding['entity'], operation=create_name)
        types = context.types(create['input']['library'])
        primary = next((name for name in serializers if name.endswith('OrderPositionCreateForExistingOrderSerializer')), serializers[0])
        input_schema = schema_for(types, primary, writing=True)
        root = input_schema['$defs'][primary]
        for secondary in serializers:
            if secondary == primary:
                continue
            patch_schema = schema_for(types, secondary, writing=True)
            patch_root = patch_schema['$defs'][secondary]
            input_schema['$defs'].update({k: v for k, v in patch_schema['$defs'].items() if k not in input_schema['$defs']})
            root['properties'].update(patch_root['properties'])
        for field in create['input'].get('inline_fields', {}):
            root['properties'].setdefault(field, {'x-native-inline-input': True})
        parent_slots, projections = {}, {}
        scopes = list(dict.fromkeys(re.findall(r'\{([^}]+)\}', create_path)))
        scope_targets = {'event': ('pretix.event', 'slug'), 'item': ('pretix.item', 'id'),
                         'order': ('pretix.order', 'code'), 'question': ('pretix.question', 'id'),
                         'subevent': ('pretix.sub_event', 'id'), 'team': ('pretix.team', 'id')}
        for scope in scopes:
            if scope not in scope_targets:
                continue
            target_entity, identity_field = scope_targets[scope]
            target = context.target(target_entity, binding['id'])
            parent_slots[scope] = {'entity': target_entity, 'binding': target['id'], 'required': False, 'many': False}
            projections[scope] = {'identity': identity_field, 'path': ['scope', scope]}
        for field, declaration in contract['fields'].items():
            ref = declaration.get('reference', {})
            if (field not in root['properties'] or len(ref.get('targets', [])) != 1
                    or ref.get('cardinality') != 'one' or ref.get('kind') == 'owned_component'):
                continue
            if types.get(primary, {}).get('fields', {}).get(field, {}).get('schema'):
                continue  # A nested serializer accepts a body, not a foreign-key ID.
            target = context.target(ref['targets'][0], binding['id'])
            fields = context.contracts[target['id']]['identity']['fields']
            if len(fields) != 1:
                continue
            slot = 'values.' + field
            parent_slots[slot] = {'entity': target['entity'], 'binding': target['id'], 'required': False, 'many': False}
            projections[slot] = {'identity': fields[0], 'path': ['values', field]}
        parameters = parameter_schema(input_schema, scope={'type': 'object',
            'properties': {key: {'type': ['string', 'integer']} for key in scopes}, 'additionalProperties': False})
        response_names = ops[read_name]['response'].get('serializers', [])
        output = schema_for(types, response_names[0], writing=False) if len(response_names) == 1 else identity_output([locator['field']])
        recipe = {'kind': 'pretix', 'create_path': create_path, 'read_path': read_path,
                  'identity_fields': [locator['field']], 'identity_parameter': locator['parameter'],
                  'scope_fields': scopes, 'read_scope_fields': locator.get('scope', []),
                  'input_schema': input_schema, 'parent_projection': projections, 'record_schema': deepcopy(output)}
        output = object_schema({'record': output, 'scope': {'type': 'object'}}, ['record', 'scope'])
        output['$defs'] = output['properties']['record'].pop('$defs', {})
        spec = context.spec(binding, create_name, read_name, recipe=recipe, parameters=parameters,
                            output=output, parents=parent_slots)
        specs.append(spec)
    if specs:
        return specs, {'status': 'factory', 'creation': 'native serializer create endpoint'}
    return [], {'status': 'aggregate_component' if contract['identity'].get('component') else 'native_derived_or_read_only',
                'reason': 'No independent native create endpoint', 'ownership': contract['identity'].get('component')}


def invoice_recipe(context, binding):
    """Create an invoice through Order.create_invoice, never direct row insertion."""
    owner = context.target('pretix.order', binding['id'])
    owner_ops = context.contracts[owner['id']]['operations']
    create_name = 'create_invoice@organizer.event.code'
    read_name = 'retrieve@organizer.event.number'
    create_route, create_path = routes(owner, create_name)[0]
    _, read_path = routes(binding, read_name)[0]
    types = context.types(owner_ops[create_name]['input']['library'])
    native_output = schema_for(types, 'pretix.api.serializers.order.InvoiceSerializer', writing=False)
    input_schema = object_schema({})
    parameters = parameter_schema(input_schema, scope=object_schema({
        'organizer': {'type': 'string'}, 'event': {'type': 'string'}, 'code': {'type': 'string'}}))
    projection = {'order': {'identity': 'code', 'path': ['scope', 'code']}}
    parents = {'order': {'entity': owner['entity'], 'binding': owner['id'], 'required': True, 'many': False}}
    recipe = {'kind': 'pretix', 'create_path': create_path, 'read_path': read_path,
              'identity_fields': ['number'], 'identity_parameter': 'number',
              'scope_fields': ['organizer', 'event', 'code'], 'read_scope_fields': ['organizer', 'event'],
              'input_schema': input_schema, 'parent_projection': projection,
              'record_schema': deepcopy(native_output),
              'locator_basis': 'InvoiceSerializer.number and InvoiceViewSet number route; independent readback required'}
    output = object_schema({'record': native_output, 'scope': {'type': 'object'}}, ['record', 'scope'])
    output['$defs'] = output['properties']['record'].pop('$defs', {})
    spec = context.spec(owner, create_name, 'retrieve@organizer.event.code', recipe=recipe,
                        parameters=parameters, output=output, parents=parents)
    spec.update(id=binding['id'] + '/from_order', entity=binding['entity'], binding=binding['id'],
                description='Create a native invoice from an existing eligible order')
    spec['operations']['read'] = {'entity': binding['entity'], 'binding': binding['id'], 'operation': read_name}
    spec['sources'].extend(binding['operation_sources'][read_name])
    return [spec], {'status': 'factory', 'creation': 'native order invoice generation lifecycle'}


class PretixAdapter(NativeAdapter):
    def _scope(self, payload, parents):
        result = {'organizer': self.connection.settings.get('organizer')}
        result.update(payload.get('scope', {}))
        for slot, parent in parents.items():
            if not self.recipe.get('parent_projection', {}).get(slot, {}).get('path', [None])[0] == 'scope':
                continue
            for key in ('organizer', 'event'):
                if key in parent.identity:
                    if result.get(key) is not None and result[key] != parent.identity[key]:
                        raise CatalogError('pretix_factory_parent_scope_conflict', parameter=key)
                    result[key] = parent.identity[key]
        return result

    def validate(self, payload, parents):
        super().validate(payload, parents)
        render(self.recipe['create_path'], self._scope(payload, parents))

    def create(self, payload, parents):
        scope = self._scope(payload, parents)
        record = self.transport.request('POST', render(self.recipe['create_path'], scope), payload['values'])
        if not isinstance(record, dict):
            raise CatalogError('pretix_factory_create_receipt_not_object')
        return self._scoped(record, scope)

    def _scoped(self, record, scope):
        # Scope is part of a handle, not a fabricated business field.
        return {'record': deepcopy(record), 'scope': {key: scope[key] for key in self.recipe['read_scope_fields']}}

    def identify(self, value):
        record = value.get('record', value)
        return {**value.get('scope', {}), self.recipe['identity_fields'][0]: record[self.recipe['identity_fields'][0]]}

    def read(self, identity, parents):
        scope = {**identity, self.recipe['identity_parameter']: identity[self.recipe['identity_fields'][0]]}
        value = self.transport.request('GET', render(self.recipe['read_path'], scope))
        return self._scoped(value, scope)

    def verify(self, payload, record, parents):
        from mac_bench.environments.catalog.factories.registry import (
            _validate_value,
        )
        _validate_value(self.recipe['record_schema'], record['record'], section='pretix_readback')


def adapter(spec, connection, runtime):
    if spec['recipe'].get('lifecycle'):
        from mac_bench.environments.catalog.factories.apps.pretix_lifecycle import (
            LifecycleAdapter,
        )
        return LifecycleAdapter(spec, connection, runtime)
    return PretixAdapter(spec, connection, runtime)
