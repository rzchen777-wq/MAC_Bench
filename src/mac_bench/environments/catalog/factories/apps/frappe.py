"""Effective DocType recipes, including child aggregates and native submission."""
from copy import deepcopy

from mac_bench.environments.apps.frappe.client import (
    FrappeClient,
    MetadataCapture,
)
from mac_bench.environments.apps.frappe.factory import (
    _TABLE,
    _TEXT,
    FrappeDocumentFactory,
)
from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.entities.storage import digest
from mac_bench.environments.catalog.factories.native import (
    NativeAdapter,
    identity_output,
    object_schema,
    parameter_schema,
)


def recipes(context, binding):
    contract = context.contracts[binding['id']]
    if 'create' not in contract['operations']:
        return [], {'status': 'aggregate_component' if contract['identity'].get('component') else 'not_independently_creatable',
                    'reason': 'Native DocType has no independent insert operation',
                    'ownership': contract['identity'].get('component')}
    raw = context.entities.evidence(binding['source'])
    if raw['native'].get('is_virtual'):
        return [], {'status': 'native_virtual_view', 'reason': 'Virtual DocType; no stored document population', 'source': binding['source']}
    documents = {}

    def document(native):
        name = native['native_name']
        if name in documents:
            return
        meta = {key: deepcopy(native['native'].get(key)) for key in
                ('autoname', 'is_submittable', 'istable', 'issingle', 'is_virtual')}
        meta['name'] = name
        field_keys = {'fieldname', 'fieldtype', 'read_only', 'reqd', 'options', 'default',
                      'fetch_from', 'mandatory_depends_on', 'non_negative', 'length', 'unique'}
        meta['fields'] = [{k: deepcopy(v) for k, v in f.items() if k in field_keys}
                          for f in native['fields'].values() if not f.get('declaration_origin')]
        documents[name] = meta
        for field in meta['fields']:
            if field['fieldtype'] in _TABLE:
                target = next(b for b in context.bindings.values()
                              if b['native_resource'] == field['options'] and b['scope'] == binding['scope'])
                document(context.entities.evidence(target['source']))
    document(raw)
    name = raw['native_name']
    definitions = {}
    for doctype, meta in documents.items():
        properties = {}
        for f in meta['fields']:
            if f.get('read_only'):
                continue
            kind = f['fieldtype']
            if kind in _TABLE:
                schema = {'type': 'array', 'items': {'$ref': '#/$defs/' + f['options'].replace('~', '~0').replace('/', '~1')}}
            elif kind in _TEXT or kind in {'HTML Editor', 'Signature', 'Geolocation'}:
                schema = {'type': 'string'}
                if kind == 'Select':
                    schema['enum'] = (f.get('options') or '').split('\n')
            elif kind == 'Check':
                schema = {'enum': [0, 1, False, True]}
            elif kind == 'Int':
                schema = {'type': 'integer'}
            elif kind in {'Float', 'Currency', 'Percent', 'Duration', 'Rating'}:
                schema = {'type': ['number', 'string']}
            elif kind in {'Date', 'Datetime', 'Time'}:
                schema = {'type': 'string'}
            elif kind == 'JSON':
                schema = {'x-native-type': 'JSON'}
            else:
                raise CatalogError('frappe_factory_field_type_unimplemented', field=f['fieldname'], type=kind)
            if not f.get('reqd'):
                schema = {'anyOf': [schema, {'type': 'null'}]}
            properties[f['fieldname']] = schema
        # Defaults, fetches and before_validate hooks run on the server. Imposing
        # raw reqd as a caller obligation would reject valid native creations.
        definitions[doctype] = object_schema(properties)
    input_schema = {**deepcopy(definitions[name]), '$defs': definitions}
    parents, projections = {}, {}
    for field, definition in contract['fields'].items():
        reference = definition.get('reference', {})
        if (field not in definitions[name]['properties'] or reference.get('kind') != 'association'
                or len(reference.get('targets', [])) != 1 or reference.get('cardinality') != 'one'):
            continue
        target = context.target(reference['targets'][0], binding['id'])
        parents[field] = {'entity': target['entity'], 'binding': target['id'], 'required': False, 'many': False}
        projections[field] = {'identity': 'name', 'path': ['values', field], 'read_field': field}
    parameters = parameter_schema(input_schema, name={'type': 'string', 'minLength': 1}, submitted={'type': 'boolean'})
    recipe = {'kind': 'frappe', 'doctype': name, 'documents': documents,
              'identity_fields': ['name'], 'input_schema': input_schema, 'parent_projection': projections}
    spec = context.spec(binding, 'create', 'read', recipe=recipe, parameters=parameters,
                        output=identity_output(['name']), parents=parents)
    if 'submit' in contract['operations']:
        spec['operations']['submit'] = {'entity': binding['entity'], 'binding': binding['id'], 'operation': 'submit'}
    return [spec], {'status': 'factory', 'creation': 'native aggregate insert; optional native submit'}


class CatalogFrappeClient(FrappeClient):
    """Use the exact whitelisted operations captured in EntityCatalog."""
    def read(self, doctype, name):
        response = self.transport.request('POST', 'api/method/frappe.client.get', {'doctype': doctype, 'name': name})
        return self._document(response, 'message', doctype, name)

    def create(self, doctype, values, *, name=None):
        payload = {**deepcopy(values), 'doctype': doctype}
        if name is not None:
            payload['name'] = name
        response = self.transport.request('POST', 'api/method/frappe.client.insert', {'doc': payload})
        return self._document(response, 'message', doctype)


class FrappeAdapter(NativeAdapter):
    def __init__(self, spec, connection, runtime):
        super().__init__(spec, connection, runtime)
        self.client = CatalogFrappeClient(self.transport)
        documents = tuple(self.recipe['documents'].values())
        metadata = MetadataCapture('catalog:' + spec['entity_revision'], digest(documents), documents)
        self.factory = FrappeDocumentFactory(self.client, metadata, self.recipe['doctype'])

    def validate(self, payload, parents):
        super().validate(payload, parents)
        self.factory.validate(payload['values'])
        if payload.get('submitted') and 'submit' not in self.spec['operations']:
            raise CatalogError('frappe_factory_not_submittable')
        prompted = (self.factory.schema.get('autoname') or '').lower() == 'prompt'
        if prompted != ('name' in payload):
            raise CatalogError('frappe_factory_name_must_match_native_rule')

    def create(self, payload, parents):
        return self.factory.create(payload['values'], submitted=payload.get('submitted', False), name=payload.get('name'))

    def read(self, identity, parents):
        return self.client.read(self.recipe['doctype'], identity['name'])

    def verify(self, payload, record, parents):
        super().verify(payload, record, parents)
        if record.get('doctype') != self.recipe['doctype'] or record.get('docstatus') != int(payload.get('submitted', False)):
            raise CatalogError('frappe_factory_native_state_mismatch')
        if payload.get('name') is not None and record.get('name') != payload['name']:
            raise CatalogError('frappe_factory_prompted_name_mismatch')


adapter = FrappeAdapter
