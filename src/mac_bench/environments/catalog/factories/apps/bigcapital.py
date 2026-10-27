"""Native accounting DTO recipes, explicit endpoint pairs and wire projections."""
import re
from copy import deepcopy
from urllib.parse import quote

from mac_bench.environments.apps.bigcapital.factory import (
    PendingAccountingOperation,
)
from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.factories.native import (
    NativeAdapter,
    object_schema,
    parameter_schema,
)
from mac_bench.environments.catalog.factories.registry import _validate_value

# Reviewed resource ownership from captured controllers, not a route-name
# heuristic. A checkout session is deliberately not a PaymentLink factory.
PAIRS = {
    'account': ('createAccount', 'getAccount'), 'bank_rule': ('createBankRule', 'getBankRule'),
    'bank_transaction': ('createTransaction', 'getTransaction'), 'bill': ('createBill', 'getBill'),
    'bill_payment': ('createBillPayment', 'getBillPayment'), 'branch': ('createBranch', 'getBranch'),
    'credit_note': ('createCreditNote', 'getCreditNote'), 'currency': ('create', 'findOne'),
    'customer': ('createCustomer', 'getCustomer'), 'expense': ('createExpense', 'getExpense'),
    'inventory_adjustment': ('createQuickInventoryAdjustment', 'getInventoryAdjustment'),
    'item': ('createItem', 'getItem'), 'item_category': ('createItemCategory', 'getItemCategory'),
    'manual_journal': ('createManualJournal', 'getManualJournal'),
    'payment_received': ('createPaymentReceived', 'getPaymentReceive'),
    'refund_credit_note': ('createRefundCreditNote', 'getCreditNoteRefunds'),
    'refund_vendor_credit': ('createRefundVendorCredit', 'getVendorCreditRefunds'),
    'sale_estimate': ('createSaleEstimate', 'getSaleEstimate'),
    'sale_invoice': ('createSaleInvoice', 'getSaleInvoice'),
    'sale_receipt': ('createSaleReceipt', 'getSaleReceipt'),
    'tax_rate_model': ('createTaxRate', 'getTaxRate'), 'vendor': ('createVendor', 'getVendor'),
    'vendor_credit': ('createVendorCredit', 'getVendorCredit'), 'warehouse': ('createWarehouse', 'getWarehouse'),
    'warehouse_transfer': ('createWarehouseTransfer', 'getWarehouseTransfer'),
}


def snake(key):
    return '_'.join(piece for piece in re.split(r'(?=[A-Z])', key) if piece).lower()


def _is_optional_predicate(source):
    """Recognize captured native null/undefined guards, not arbitrary conditions."""
    source = source.strip()
    builtin = (r'\(object,\s*value\)\s*=>\s*\{\s*return\s+object\[propertyName\]\s*!==\s*null\s*&&\s*'
               r'object\[propertyName\]\s*!==\s*undefined\s*;\s*\}')
    custom = (r'\(\s*\w+\s*,\s*(?P<value>\w+)\s*\)\s*=>\s*\{\s*return\s+'
              r'(?P=value)\s*!==\s*null\s*&&\s*(?P=value)\s*!==\s*undefined\s*&&\s*'
              r"(?P=value)\s*!==\s*''\s*;\s*\}")
    return bool(re.fullmatch(builtin, source) or re.fullmatch(custom, source))


def json_schema(root, types, *, wire=False):
    definitions = {}
    def convert(value, mapping=wire):
        if isinstance(value, list):
            return [convert(v, mapping) for v in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, val in value.items():
            if key == '$ref' and val.startswith('#/components/schemas/'):
                name = val.removeprefix('#/components/schemas/')
                alias = ('wire:' if mapping else 'native:') + name
                if alias not in definitions:
                    definitions[alias] = {}
                    definitions[alias] = convert(types[name], mapping)
                result[key] = '#/$defs/' + alias.replace('~', '~0').replace('/', '~1')
            elif key == 'properties':
                result[key] = {(snake(k) if mapping else k): convert(v, mapping) for k, v in val.items()}
            elif key == 'required':
                result[key] = [snake(k) if mapping else k for k in val]
            elif key == 'items':
                # The deployed deepdash interceptor skips children of arrays.
                result[key] = convert(val, False)
            elif key.startswith('x-'):
                result[key] = deepcopy(val)
            elif key == 'example':
                result['examples'] = [deepcopy(val)]
            elif key not in {'nullable', 'discriminator'}:
                result[key] = convert(val, mapping)
        validators = value.get('x-native-validators', [])
        props = result.get('properties', {})
        if validators and props and not mapping:
            result['additionalProperties'] = False
        for rule in validators:
            name = snake(rule['propertyName']) if mapping else rule['propertyName']
            if name not in props or mapping:
                continue
            native = rule.get('name')
            target = props[name].setdefault('items', {}) if rule.get('each') else props[name]
            if native in {'isDateString', 'isString', 'isUUID', 'isEmail'}:
                target['type'] = 'string'
            if native == 'isNotEmpty' and target.get('type') == 'string':
                target['minLength'] = 1
        # Swagger metadata can mark inherited DTO fields required even though
        # class-validator's IsOptional skips every validator for null/undefined.
        # Recognize that exact captured predicate; arbitrary ValidateIf code
        # does not justify weakening a field's contract.
        if not mapping:
            optional = {
                rule['propertyName'] for rule in validators
                if rule.get('type') == 'conditionalValidation'
                and any(isinstance(c, dict) and _is_optional_predicate(c.get('source', ''))
                    for c in rule.get('constraints', []))
            }
            if optional:
                result['required'] = [name for name in result.get('required', []) if name not in optional]
                for name in optional & props.keys():
                    props[name] = {'anyOf': [props[name], {'type': 'null'}]}
        if value.get('nullable'):
            result = {'anyOf': [result, {'type': 'null'}]}
        return result
    result = convert(root)
    return {**result, '$defs': definitions}


def route(binding, operation):
    endpoints = binding['operation_bindings'][operation]
    if len(endpoints) != 1:
        raise CatalogError('bigcapital_factory_route_ambiguous', operation=operation)
    return endpoints[0]


def render(path, parameters):
    def substitute(match):
        if match[1] not in parameters:
            raise CatalogError('bigcapital_factory_scope_missing', parameter=match[1])
        return quote(str(parameters[match[1]]), safe='')
    return re.sub(r':(\w+)', substitute, path)


def recipes(context, binding):
    contract = context.contracts[binding['id']]
    entity = binding['entity'].split('.', 1)[1]
    if entity == 'payment_link':
        from mac_bench.environments.catalog.factories.apps.payment_links import (
            recipe,
        )
        return recipe(context, binding)
    if entity == 'document_model':
        from mac_bench.environments.catalog.factories.apps.attachments import (
            recipe,
        )
        return recipe(context, binding)
    if entity not in PAIRS:
        return [], {'status': 'aggregate_component' if contract['identity'].get('component') else 'native_derived_or_read_only',
                    'reason': 'No independent entity-creating controller operation; lifecycle/aggregate output',
                    'ownership': contract['identity'].get('component')}
    create_name, read_name = PAIRS[entity]
    create, read = contract['operations'][create_name], contract['operations'][read_name]
    create_route, read_route = route(binding, create_name), route(binding, read_name)
    types = context.types(create['input']['library'])
    body = [p for p in create['input']['parameters'] if p['in'] == 'body']
    if len(body) != 1 or body[0]['name'] is not None:
        raise CatalogError('bigcapital_factory_body_contract_unsupported', entity=binding['entity'])
    input_schema = json_schema(body[0]['schema'], types)
    input_root = body[0]['schema']
    properties = types[input_root['$ref'].split('/')[-1]].get('properties', {}) if '$ref' in input_root else input_root.get('properties', {})
    scope_fields = re.findall(r':(\w+)', create_route['path_template'])
    read_fields = re.findall(r':(\w+)', read_route['path_template'])
    list_read = entity in {'refund_credit_note', 'refund_vendor_credit'}
    identity_field = 'currency_code' if entity == 'currency' else 'id'
    identity_parameter = None if list_read else read_fields[-1]
    response = read['response'].get('native_schema')
    if response is None:
        # Content-negotiated JSON branches use these captured native transformers.
        transformers = {'credit_note': 'CreditNoteTransformer', 'payment_received': 'PaymentReceiveTransfromer', 'sale_estimate': 'SaleEstimateTransfromer',
                        'sale_invoice': 'SaleInvoiceTransformer', 'sale_receipt': 'SaleReceiptTransformer'}
        if entity not in transformers:
            raise CatalogError('bigcapital_factory_read_schema_missing', entity=entity)
        response = {'$ref': '#/components/schemas/NativeProjection.' + transformers[entity]}
    record_pointer = ['data'] if entity == 'warehouse_transfer' else []
    record_schema = response
    for key in record_pointer:
        record_schema = record_schema['properties'][key]
    if list_read:
        record_schema = response['items']
    output_record = json_schema(record_schema, types, wire=not list_read)
    parents, projections = {}, {}
    for field, declaration in contract['fields'].items():
        ref = declaration.get('reference', {})
        if len(ref.get('targets', [])) != 1 or ref.get('cardinality') != 'one' or field not in properties:
            continue
        target = context.target(ref['targets'][0], binding['id'])
        if not context.contracts[target['id']]['operations'] or target['entity'] == 'bigcapital.contact':
            # These IDs are native DTO inputs, but captured APIs do not provide
            # an exact independent entity read. Do not promise a parent handle
            # reader; the native controller still validates supplied IDs.
            continue
        parents[field] = {'entity': target['entity'], 'binding': target['id'], 'required': False, 'many': False}
        projections[field] = {'identity': 'id', 'path': ['values', field], 'read_field': snake(field)}
    for scope in scope_fields:
        target_entity = {'creditNoteId': 'bigcapital.credit_note', 'vendorCreditId': 'bigcapital.vendor_credit'}.get(scope)
        if target_entity:
            target = context.target(target_entity, binding['id'])
            parents[scope] = {'entity': target_entity, 'binding': target['id'], 'required': False, 'many': False}
            projections[scope] = {'identity': 'id', 'path': ['scope', scope]}
    parameters = parameter_schema(input_schema, scope=object_schema({k: {'type': ['integer', 'string']} for k in scope_fields}))
    recipe = {'kind': 'bigcapital', 'input_schema': input_schema, 'parent_projection': projections,
              'create_path': create_route['path_template'], 'create_method': create_route['method'],
              'read_path': read_route['path_template'], 'identity_parameter': identity_parameter,
              'identity_fields': [identity_field], 'scope_fields': scope_fields, 'list_read': list_read,
              'record_pointer': record_pointer, 'record_schema': output_record,
              'empty_create': entity == 'currency'}
    output = object_schema({'record': deepcopy(output_record), 'scope': {'type': 'object'}}, ['record', 'scope'])
    # Hoist local definitions for the record schema's references.
    output['$defs'] = output['properties']['record'].pop('$defs', {})
    spec = context.spec(binding, create_name, read_name, recipe=recipe, parameters=parameters, output=output, parents=parents)
    return [spec], {'status': 'factory', 'creation': 'native accounting DTO and independent native read'}


class BigCapitalAdapter(NativeAdapter):
    def validate(self, payload, parents):
        super().validate(payload, parents)
        render(self.recipe['create_path'], payload.get('scope', {}))

    def create(self, payload, parents):
        scope = payload.get('scope', {})
        response = self.transport.request_response(self.recipe['create_method'],
            render(self.recipe['create_path'], scope), payload['values'])
        if response.status == 202:
            raise PendingAccountingOperation(response)
        if self.recipe['empty_create']:
            # Currency creation returns no entity. Its native unique code is
            # caller supplied, and success still requires independent readback.
            return {'record': {'currency_code': payload['values']['currencyCode']}, 'scope': scope,
                    'native_response': deepcopy(response.body), 'locator_basis': 'caller supplied native unique currency code'}
        if not isinstance(response.body, dict):
            raise CatalogError('bigcapital_factory_create_receipt_not_object')
        return {'record': deepcopy(response.body), 'scope': deepcopy(scope)}

    def identify(self, value):
        record = value['record']
        return {**value['scope'], self.recipe['identity_fields'][0]: record[self.recipe['identity_fields'][0]]}

    def read(self, identity, parents):
        scope = {key: identity[key] for key in self.recipe['scope_fields']}
        arguments = {**scope}
        if self.recipe['identity_parameter']:
            arguments[self.recipe['identity_parameter']] = identity[self.recipe['identity_fields'][0]]
        record = self.transport.request('GET', render(self.recipe['read_path'], arguments))
        for key in self.recipe['record_pointer']:
            record = record[key]
        if self.recipe['list_read']:
            if not isinstance(record, list):
                raise CatalogError('bigcapital_factory_expected_complete_native_list')
            matches = [row for row in record if row.get('id') == identity['id']]
            if len(matches) != 1:
                raise CatalogError('bigcapital_factory_readback_not_unique')
            record = matches[0]
        return {'record': record, 'scope': scope}

    def verify(self, payload, record, parents):
        _validate_value(self.recipe['record_schema'], record['record'], section='bigcapital_readback')
        # Readback transforms may rename or derive fields. Only source-backed
        # direct foreign-key projections are checked for equality.
        super().verify(payload, record, parents)


def adapter(spec, connection, runtime):
    if spec['recipe']['kind'] == 'payment_link':
        from mac_bench.environments.catalog.factories.apps.payment_links import (
            PaymentLinkAdapter,
        )
        return PaymentLinkAdapter(spec, connection, runtime)
    if spec['recipe']['kind'] == 'attachment':
        from mac_bench.environments.catalog.factories.apps.attachments import (
            AttachmentAdapter,
        )
        return AttachmentAdapter(spec, connection, runtime)
    return BigCapitalAdapter(spec, connection, runtime)
