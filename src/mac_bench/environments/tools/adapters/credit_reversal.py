"""Customer-credit reversal exposed by the native accounting runtime."""
from mac_bench.environments.catalog.factories.apps.bigcapital import route
from mac_bench.environments.tools.adapters.common import exchange
from mac_bench.environments.tools.contracts import Tool, object_schema
from mac_bench.environments.tools.examples import example


def reversal_tool(binding, identity, catalog_revision):
    operation = 'deleteAppliedInvoiceToCreditNote'
    # Keep the same API prefix as this deployment's other credit operations.
    applied_path = route(binding, 'appliedCreditNoteToInvoices')['path_template']
    prefix = applied_path.split('/:creditNoteId/')[0]
    endpoint = prefix + '/applied-invoices/:creditNoteAppliedInvoiceId'
    schema = object_schema({'path': object_schema({
        'creditNoteAppliedInvoiceId': {
            'type': 'integer', 'minimum': 1,
            'description': 'Allocation row id returned by appliedCreditNoteToInvoices or applyCreditNoteToInvoices; not the invoice id or credit note id.',
        }}, ['creditNoteAppliedInvoiceId'])}, ['path'])

    def execute(connection, args):
        allocation = args['path']['creditNoteAppliedInvoiceId']
        return exchange(connection, 'DELETE', endpoint.replace(':creditNoteAppliedInvoiceId', str(allocation)),
                        rate_limit_key=endpoint)

    return Tool(binding['entity'] + '.' + operation, 'bigcapital', binding['entity'],
        operation, identity, binding['scope'],
        'Unapply one customer credit allocation from an invoice. Restores unused credit and the invoice outstanding amount; retains the credit note and invoice.',
        schema, 'data confirms deleted=true and credit_note_applied_invoice_id. Read the invoice and credit note to inspect the updated balances.',
        False, execute,
        ('Find the allocation id with bigcapital.credit_note_applied_invoice.appliedCreditNoteToInvoices.',
         'This reverses a customer credit allocation, not a payment, refund, or vendor credit.'),
        {'catalog_revision': catalog_revision,
         'operation_contract': {'method': 'DELETE', 'path': endpoint,
             'implementation': 'accounting_patch.patch_credit_unapply_controller'},
         'output': {'type': 'object', 'properties': {
             'deleted': {'type': 'boolean'},
             'credit_note_applied_invoice_id': {'type': 'integer'}}},
         'examples': example(schema)})
