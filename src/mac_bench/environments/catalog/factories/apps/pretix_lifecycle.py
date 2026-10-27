"""Creation of transaction/check-in entities through their owning native actions."""
from copy import deepcopy
from decimal import Decimal

from mac_bench.environments.apps.pretix.client import PretixClient
from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.factories.apps.drf import schema_for
from mac_bench.environments.catalog.factories.apps.pretix import (
    PretixAdapter,
    render,
    routes,
)
from mac_bench.environments.catalog.factories.native import (
    object_schema,
    parameter_schema,
)


def recipes(context, binding):
    checkin = binding['entity'] == 'pretix.checkin'
    owner_entity = 'pretix.order_position' if checkin else 'pretix.gift_card'
    owner = context.target(owner_entity, binding['id'])
    create_name = 'redeem@organizer.event.list.pk' if checkin else 'transact@organizer.pk'
    owner_read = 'retrieve@organizer.event.list.pk' if checkin else 'retrieve@organizer.pk'
    read_name = 'retrieve@organizer.event.pk' if checkin else 'retrieve@organizer.giftcard.pk'
    _, create_path = routes(owner, create_name)[0]
    _, read_path = routes(binding, read_name)[0]
    _, owner_read_path = routes(owner, owner_read)[0]
    if checkin:
        values = object_schema({'datetime': {'type': 'string'}, 'type': {'enum': ['entry', 'exit']},
            'force': {'type': 'boolean'}, 'ignore_unpaid': {'type': 'boolean'},
            'questions_supported': {'type': 'boolean'}, 'canceled_supported': {'type': 'boolean'},
            'nonce': {'type': 'string'}, 'answers': {'type': 'array'}})
        scopes = ['organizer', 'event', 'list', 'pk']
        output_scopes = ['organizer', 'event']
        parent_defs = {'position': (owner_entity, 'pk'), 'checkin_list': ('pretix.checkin_list', 'list')}
    else:
        values = object_schema({'value': {'type': ['number', 'string']},
                                'text': {'type': ['string', 'null']}, 'info': {}}, ['value'])
        scopes, output_scopes = ['organizer', 'pk'], ['organizer', 'giftcard']
        parent_defs = {'giftcard': (owner_entity, 'pk')}
    parents, projections = {}, {}
    for slot, (entity, parameter) in parent_defs.items():
        target = context.target(entity, binding['id'])
        parents[slot] = {'entity': entity, 'binding': target['id'], 'required': True, 'many': False}
        projections[slot] = {'identity': 'id', 'path': ['scope', parameter]}
    read = context.contracts[binding['id']]['operations'][read_name]
    types = context.types(read['response']['library'])
    native_output = schema_for(types, read['response']['serializers'][0], writing=False)
    recipe = {'kind': 'pretix', 'lifecycle': 'checkin' if checkin else 'giftcard_transaction',
              'create_path': create_path, 'read_path': read_path, 'owner_read_path': owner_read_path,
              'identity_fields': ['id'], 'identity_parameter': 'pk',
              'scope_fields': scopes, 'read_scope_fields': output_scopes,
              'input_schema': values, 'parent_projection': projections, 'record_schema': deepcopy(native_output)}
    if not checkin:
        _, recipe['list_path'] = routes(binding, 'list@organizer.giftcard')[0]
    output = object_schema({'record': native_output, 'scope': {'type': 'object'}}, ['record', 'scope'])
    output['$defs'] = output['properties']['record'].pop('$defs', {})
    parameters = parameter_schema(values, scope=object_schema({k: {'type': ['string', 'integer']} for k in scopes}))
    spec = context.spec(owner, create_name, owner_read, recipe=recipe, parameters=parameters,
                        output=output, parents=parents)
    spec.update(id=binding['id'] + '/from_' + recipe['lifecycle'], entity=binding['entity'], binding=binding['id'],
                description='Create ' + binding['entity'] + ' through native ' + create_name)
    spec['operations']['read'] = {'entity': binding['entity'], 'binding': binding['id'], 'operation': read_name}
    spec['operations']['read_owner'] = {'entity': owner_entity, 'binding': owner['id'], 'operation': owner_read}
    if not checkin:
        spec['operations']['list'] = {'entity': binding['entity'], 'binding': binding['id'], 'operation': 'list@organizer.giftcard'}
    spec['sources'].extend(binding['operation_sources'][read_name])
    return [spec], {'status': 'factory', 'creation': 'native owner action, exact new identity and independent readback'}


class LifecycleAdapter(PretixAdapter):
    def _rows(self, scope):
        if self.recipe['lifecycle'] == 'checkin':
            parent = self.transport.request('GET', render(self.recipe['owner_read_path'], scope))
            if not isinstance(parent, dict) or not isinstance(parent.get('checkins'), list):
                raise CatalogError('pretix_checkin_parent_readback_missing')
            return parent['checkins']
        client = PretixClient(self.transport, scope['organizer'])
        return client.listing(render(self.recipe['list_path'], scope), identity='id')

    def create(self, payload, parents):
        scope = self._scope(payload, parents)
        if self.recipe['lifecycle'] == 'giftcard_transaction':
            scope['giftcard'] = scope['pk']
        before = {row['id'] for row in self._rows(scope)}
        response = self.transport.request('POST', render(self.recipe['create_path'], scope), payload['values'])
        if self.recipe['lifecycle'] == 'checkin' and response.get('status') != 'ok':
            raise CatalogError('pretix_checkin_native_action_not_successful')
        after = self._rows(scope)
        created = [row for row in after if row['id'] not in before]
        if len(created) != 1:
            raise CatalogError('pretix_lifecycle_created_identity_not_unique', count=len(created))
        return self._scoped(created[0], scope)

    def verify(self, payload, record, parents):
        super().verify(payload, record, parents)
        native = record['record']
        if self.recipe['lifecycle'] == 'checkin':
            if native.get('list') != payload['scope']['list']:
                raise CatalogError('pretix_checkin_list_mismatch')
            if 'type' in payload['values'] and native.get('type') != payload['values']['type']:
                raise CatalogError('pretix_checkin_type_mismatch')
        elif Decimal(str(native['value'])) != Decimal(str(payload['values']['value'])):
            raise CatalogError('pretix_giftcard_transaction_value_mismatch')
