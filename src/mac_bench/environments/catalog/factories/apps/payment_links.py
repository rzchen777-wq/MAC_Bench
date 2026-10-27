"""Persist a PaymentLink through the invoice action, not a Stripe checkout call."""
from copy import deepcopy
from urllib.parse import unquote, urlsplit

from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.factories.apps.bigcapital import (
    json_schema,
    render,
    route,
)
from mac_bench.environments.catalog.factories.native import (
    NativeAdapter,
    object_schema,
    parameter_schema,
)


def recipe(context, binding):
    # PaymentLink lives in the application database while invoices live in a
    # tenant database. Generate an explicit recipe per captured tenant; never
    # silently choose one tenant as the global resource's parent.
    owners = [b for b in context.bindings.values() if b['entity'] == 'bigcapital.sale_invoice']
    if not owners:
        raise CatalogError('payment_link_invoice_binding_missing')
    specs = [_tenant_recipe(context, binding, owner) for owner in owners]
    return specs, {'status': 'factory', 'creation': 'native invoice payment-link generation and public metadata read'}


def _tenant_recipe(context, binding, owner):
    create, read = 'generateSaleInvoiceSharableLink', 'getPaymentLinkPublicMeta'
    op = context.contracts[binding['id']]['operations'][read]
    types = context.types(op['response']['library'])
    native_output = json_schema(op['response']['native_schema'], types, wire=True)
    values = object_schema({})
    parents = {'invoice': {'entity': owner['entity'], 'binding': owner['id'], 'required': True, 'many': False}}
    spec = context.spec(owner, create, 'getSaleInvoice', recipe={
        'kind': 'payment_link', 'identity_fields': ['payment_link_id'], 'input_schema': values,
        'parent_projection': {'invoice': {'identity': 'id', 'path': ['scope', 'id']}},
        'create_path': route(owner, create)['path_template'], 'read_path': route(binding, read)['path_template'],
        'public_link_path': '/payment/{PAYMENT_LINK_ID}', 'record_schema': deepcopy(native_output)},
        parameters=parameter_schema(values, scope=object_schema({'id': {'type': ['integer', 'string']}})),
        output=object_schema({'payment_link_id': {'type': 'string'}, 'metadata': native_output},
                             ['payment_link_id', 'metadata']), parents=parents)
    spec['output']['$defs'] = spec['output']['properties']['metadata'].pop('$defs', {})
    spec.update(id=binding['id'] + '/from_invoice@' + owner['scope']['database'], entity=binding['entity'], binding=binding['id'],
                description='Generate a persistent payment link for an existing invoice')
    spec['operations']['read'] = {'entity': binding['entity'], 'binding': binding['id'], 'operation': read}
    spec['sources'].extend(binding['operation_sources'][read])
    source = {'application': 'bigcapital', 'sha256': context.entities.application('bigcapital')['evidence_sha256'],
              'pointer': '/definitions/source~1modules~1SaleInvoices~1constants.js'}
    if '"{BASE_URL}/payment/{PAYMENT_LINK_ID}"' not in context.entities.evidence(source)['source']:
        raise CatalogError('payment_link_native_url_contract_changed')
    spec['sources'].append(source)
    return spec


class PaymentLinkAdapter(NativeAdapter):
    def create(self, payload, parents):
        receipt = self.transport.request('POST', render(self.recipe['create_path'], payload['scope']))
        if not isinstance(receipt, dict) or not isinstance(receipt.get('link'), str):
            raise CatalogError('payment_link_native_receipt_invalid')
        path = urlsplit(receipt['link']).path
        prefix = self.recipe['public_link_path'].split('{', 1)[0]
        if not path.startswith(prefix) or not path[len(prefix):] or '/' in path[len(prefix):]:
            raise CatalogError('payment_link_native_locator_invalid')
        return {'payment_link_id': unquote(path[len(prefix):]), 'receipt': receipt}

    def read(self, identity, parents):
        path = render(self.recipe['read_path'], {'paymentLinkId': identity['payment_link_id']})
        metadata = self.transport.request('GET', path)
        return {'payment_link_id': identity['payment_link_id'], 'metadata': metadata}

    def verify(self, payload, record, parents):
        super().verify(payload, record, parents)
        # The metadata belongs to the associated invoice, not the PaymentLink DB
        # row. Preserve that distinction in the returned projection.
        invoice = record['metadata'].get('data', {})
        if invoice.get('id') is not None and invoice['id'] != payload['scope']['id']:
            raise CatalogError('payment_link_invoice_readback_mismatch')
