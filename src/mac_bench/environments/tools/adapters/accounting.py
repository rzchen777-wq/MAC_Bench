"""Reviewed accounting semantics over the native BigCapital operations.

Amounts and effects follow the deployed models and command validators, not task
answers. The native payload stays intact. Known pre-write failures are identified
by operation AND error code; transport and unreviewed failures remain uncertain.
"""
from copy import deepcopy
from dataclasses import replace

from mac_bench.environments.errors import NativeHTTPFailure
from mac_bench.environments.tools.contracts import ToolError, object_schema
from mac_bench.environments.tools.navigation import resolve


ALLOCATION = {
    ('bigcapital.credit_note_applied_invoice', 'applyCreditNoteToInvoices'):
        ('creditNoteId', 'invoiceId', 'customer', 'invoice', 'appliedCreditNoteToInvoices'),
    ('bigcapital.vendor_credit_applied_bill', 'applyVendorCreditToBills'):
        ('vendorCreditId', 'billId', 'vendor', 'bill', 'getAppliedBillsToVendorCredit'),
}

# These guards run before mutation (or before mutation inside the patched
# transaction). Never infer this property from HTTP 400 alone.
NO_WRITE_ERRORS = {
    ('bigcapital.expense', 'deleteExpense'): {'EXPENSE_HAS_ASSOCIATED_LANDED_COST'},
    ('bigcapital.credit_note_applied_invoice', 'applyCreditNoteToInvoices'): {
        'INVOICES_IDS_NOT_FOUND', 'INVOICES_NOT_DELIVERED_YET',
        'INVOICES_HAS_NO_REMAINING_AMOUNT', 'CREDIT_NOTE_HAS_NO_REMAINING_AMOUNT'},
    ('bigcapital.vendor_credit_applied_bill', 'applyVendorCreditToBills'): {
        'INVALID_VENDOR_CREDIT_ALLOCATION', 'BILL_ENTRIES_IDS_NOT_FOUND',
        'BILLS_NOT_OPENED_YET', 'BILLS_HAS_NO_REMAINING_AMOUNT',
        'VENDOR_CREDIT_HAS_NO_REMAINING_AMOUNT'},
    ('bigcapital.credit_note_applied_invoice', 'deleteAppliedInvoiceToCreditNote'): {
        'CREDIT_NOTE_APPLY_TO_INVOICES_NOT_FOUND'},
    ('bigcapital.vendor_credit_applied_bill', 'deleteAppliedBillToVendorCredit'): {
        'VENDOR_CREDIT_APPLY_TO_BILLS_NOT_FOUND'},
    ('bigcapital.sale_invoice', 'editSaleInvoice'): {
        'INVOICE_AMOUNT_SMALLER_THAN_SETTLED', 'INVOICE_CUSTOMER_HAS_SETTLEMENTS'},
    ('bigcapital.bill', 'editBill'): {
        'BILL_AMOUNT_SMALLER_THAN_SETTLED', 'BILL_VENDOR_HAS_SETTLEMENTS'},
    ('bigcapital.credit_note', 'editCreditNote'): {
        'CREDIT_NOTE_AMOUNT_SMALLER_THAN_USED', 'CREDIT_NOTE_CUSTOMER_HAS_SETTLEMENTS'},
    ('bigcapital.vendor_credit', 'editVendorCredit'): {
        'VENDOR_CREDIT_AMOUNT_SMALLER_THAN_USED', 'VENDOR_CREDIT_VENDOR_HAS_SETTLEMENTS'},
}

FIELDS = {
    'bigcapital.sale_invoice': {
        'total': 'Invoice total in currency_code, before settlement.',
        'balance': 'Stored invoice amount before settlement; not the outstanding balance. Use total and due_amount.',
        'payment_amount': 'Cumulative received payments allocated to this invoice.',
        'credited_amount': 'Cumulative customer credit allocated to this invoice.',
        'writtenoff_amount': 'Cumulative amount written off on this invoice.',
        'balance_amount': 'Settled amount: payment_amount + credited_amount + writtenoff_amount; not the amount due.',
        'due_amount': 'Outstanding amount: max(total - balance_amount, 0).',
    },
    'bigcapital.bill': {
        'total': 'Bill total in currency_code, before settlement.',
        'amount': 'Stored bill amount before settlement; not the outstanding balance.',
        'payment_amount': 'Cumulative payments allocated to this bill.',
        'credited_amount': 'Cumulative vendor credit allocated to this bill.',
        'balance': 'Settled amount: payment_amount + credited_amount; not the amount due.',
        'due_amount': 'Outstanding amount: max(total - balance, 0).',
    },
    'bigcapital.credit_note': {
        'amount': 'Total customer credit in currency_code, before applications and refunds.',
        'invoices_amount': 'Cumulative credit applied to customer invoices.',
        'refunded_amount': 'Cumulative refund amount recorded against this credit note.',
        'credits_used': 'Total used credit: invoices_amount + refunded_amount.',
        'credits_remaining': 'Unused credit: max(amount - invoices_amount - refunded_amount, 0).',
    },
    'bigcapital.vendor_credit': {
        'amount': 'Total vendor credit in currency_code, before applications and refunds.',
        'invoiced_amount': 'Cumulative vendor credit applied to bills.',
        'refunded_amount': 'Cumulative refund amount recorded against this vendor credit.',
        'credits_remaining': 'Unused credit: max(amount - invoiced_amount - refunded_amount, 0).',
    },
}

# These are the native model getters, including settlement through credits.
# In particular is_paid is not a claim that the entire amount was paid in cash.
SETTLEMENT_STATE = {
    'is_paid': 'True when partially or fully settled; does not mean fully paid in cash. Read due_amount for the outstanding amount.',
    'is_partially_paid': 'True when due_amount differs from total and is greater than zero. Settlement may include credits, not only payments.',
    'is_fully_paid': 'True when due_amount is zero. Settlement may include credits, not only payments.',
}

DOCUMENTS = {
    'bigcapital.sale_invoice': ('getSaleInvoice', 'total', 'customer', (
        'invoice_no', 'reference_no', 'invoice_date', 'due_date', 'total',
        'payment_amount', 'credited_amount', 'writtenoff_amount', 'due_amount',
        'is_delivered', 'is_fully_paid', 'is_writtenoff')),
    'bigcapital.bill': ('getBill', 'total', 'vendor', (
        'bill_number', 'reference_no', 'bill_date', 'due_date', 'total',
        'payment_amount', 'credited_amount', 'due_amount', 'is_open', 'is_fully_paid')),
    'bigcapital.credit_note': ('getCreditNote', 'amount', 'customer', (
        'credit_note_number', 'reference_no', 'credit_note_date', 'amount',
        'invoices_amount', 'refunded_amount', 'credits_used', 'credits_remaining', 'is_open')),
    'bigcapital.vendor_credit': ('getVendorCredit', 'amount', 'vendor', (
        'vendor_credit_number', 'reference_no', 'vendor_credit_date', 'amount',
        'invoiced_amount', 'refunded_amount', 'credits_remaining', 'is_open')),
}

# Only these operations return document records. Other operations on the same
# entity return acknowledgements, transaction rows, configuration or HTML.
DOCUMENT_COLLECTIONS = {
    'bigcapital.sale_invoice': {'getSaleInvoices', 'getReceivableSaleInvoices'},
    'bigcapital.bill': {'getBills', 'getDueBills'},
    'bigcapital.credit_note': {'getCreditNotes'},
    'bigcapital.vendor_credit': {'getVendorCredits'},
}


def document_schema(entity):
    """One public definition drives the overview fields, types and example."""
    _, total, party, names = DOCUMENTS[entity]

    def field(kind, description, sample, nullable=False):
        return {'type': [kind, 'null'] if nullable else kind,
                'description': description, 'examples': [sample]}

    properties = {
        'id': field('integer', 'Native document ID used by the detail and update operations.', 42),
        party+'_id': field('integer', f'Native {party} ID; distinct from the document ID.', 7),
        'currency_code': field('string', 'Currency of the document amounts.', 'USD'),
        'exchange_rate': field('number', 'Native exchange rate to the accounting base currency.', 1),
    }
    counters = {'payment_amount', 'credited_amount', 'writtenoff_amount',
                'invoices_amount', 'invoiced_amount', 'refunded_amount'}
    samples = {'total': 1200, 'amount': 1200, 'payment_amount': 200, 'credited_amount': 100,
               'writtenoff_amount': None, 'due_amount': 900, 'invoices_amount': 300,
               'invoiced_amount': 300, 'refunded_amount': 100, 'credits_used': 400,
               'credits_remaining': 800}
    states = {**SETTLEMENT_STATE,
              'is_delivered': 'Whether the invoice has been marked as delivered.',
              'is_open': 'Whether the document is open in the native application.',
              'is_writtenoff': 'Whether the invoice has been written off.'}
    for name in names:
        if name in FIELDS[entity]:
            meaning = FIELDS[entity][name]
            if name in counters:
                meaning += ' null means no recorded amount and counts as zero in settlement formulas.'
            properties[name] = field('number', meaning, samples[name], name in counters)
        elif name in states:
            properties[name] = field('boolean', states[name], name in {'is_open', 'is_delivered'})
        elif name.endswith('_date'):
            properties[name] = field('string', 'Native business date; returned as a date or ISO timestamp.',
                                     '2027-01-01T00:00:00.000Z', True)
        else:
            properties[name] = field('string', 'Business reference, distinct from the native numeric ID.'
                if name == 'reference_no' else 'Human-readable document number, distinct from the native numeric ID.',
                'EXAMPLE-42', True)
    properties[party] = {**object_schema({
        'id': field('integer', f'Native {party} ID.', 7),
        'display_name': field('string', f'{party.capitalize()} display name.', 'Example account', True),
        'currency_code': field('string', 'Counterparty account currency; document amounts use the outer currency_code.', 'USD', True),
    }), 'type': ['object', 'null'], 'description': 'Counterparty identity. Full contact details are in the saved record.'}
    schema = object_schema(properties, ('id', 'currency_code', total))
    schema['description'] = ('Document overview. Optional fields are included when present in the native record; '
                             'null values remain null. Line items and other native fields are available in the complete saved record.')
    return schema


def _no_write(exc, key):
    if exc.status != 400 or not isinstance(exc.native_payload, dict):
        return False
    errors = exc.native_payload.get('errors')
    allowed = NO_WRITE_ERRORS.get(key, set())
    return bool(isinstance(errors, list) and errors and all(
        isinstance(error, dict) and isinstance(error.get('type'), str)
        and error['type'] in allowed for error in errors))


def refine(tool):
    """Use one reviewed contract for discovery, validation and execution."""
    if tool.application != 'bigcapital':
        return tool
    key = tool.entity, tool.operation
    schema, native = deepcopy(tool.input_schema), deepcopy(tool.native_contract)
    notes, description, output = list(tool.notes), tool.description, tool.output_description
    allocation = ALLOCATION.get(key)
    if allocation:
        credit_id, document_id, party, document, listing = allocation
        schema['properties']['path']['properties'][credit_id].update(
            type='integer', minimum=1,
            description=f'Native {party} credit document id; distinct from {document} and allocation row ids.')
        body = schema['properties']['body']
        root = resolve(schema, body['$ref']) if '$ref' in body else body
        entries = root['properties']['entries']
        entries.update(minItems=1, description=f'New allocations, one entry per {document}. All entries are checked before any allocation is written.')
        entry = entries['items']
        if '$ref' in entry:
            entry = resolve(schema, entry['$ref'])
        entry['properties'][document_id].update(type='integer', minimum=1,
            description=f'Native {document} id belonging to the same {party} as the credit document.')
        entry['properties']['amount'].update(type='number', exclusiveMinimum=0,
            description=f'Additional credit to apply to this {document} in this call, in the credit document currency. '
                        'Not a cumulative target. Must not exceed its current due_amount; '
                        'the sum of entries must not exceed credits_remaining.')
        description = f'Apply additional {party} credit to one or more {document}s. Each amount adds to existing allocations.'
        notes.insert(0, f'Reads: get{"SaleInvoice" if document == "invoice" else "Bill"} for due_amount; '
                     f'get{"CreditNote" if party == "customer" else "VendorCredit"} for credits_remaining; {listing} for existing allocations.')
        notes.insert(1, f'Applying credit reduces both unused credit and the {document} outstanding amount. '
                     'It does not refund or change received/made payments. '
                     'A new successful call adds another allocation; reuse request_id only to retrieve the same receipt.')
        notes.insert(2, f'{"Invoices must be delivered" if document == "invoice" else "Bills must be open"}. '
                     f'Reverse an allocation with {"deleteAppliedInvoiceToCreditNote" if document == "invoice" else "deleteAppliedBillToVendorCredit"}, using its allocation row id.')
    document = DOCUMENTS.get(tool.entity)
    returns_document = document and (tool.operation == document[0]
        or tool.operation in DOCUMENT_COLLECTIONS[tool.entity])
    fields = FIELDS.get(tool.entity) if returns_document else None
    if fields:
        native.setdefault('output', {})['amount_fields'] = fields
        output += (' Monetary fields use the document currency_code. '
                   'A null settlement counter means no recorded amount and counts as zero in these formulas. ') + ' '.join(
            f'{name}: {meaning}' for name, meaning in fields.items()
            if name in {'due_amount', 'balance_amount', 'credits_remaining', 'balance'})
    if returns_document and tool.entity in {'bigcapital.sale_invoice', 'bigcapital.bill'}:
        native.setdefault('output', {})['state_fields'] = dict(SETTLEMENT_STATE)
        output += (' is_paid includes partial settlement; is_fully_paid means due_amount is zero. '
                   'These flags include settlement by credits; payment_amount records actual allocated payments.')
    if document and tool.operation == document[0]:
        native['output']['record_overview'] = document_schema(tool.entity)
        output = ('data shows the document identity, counterparty, dates and settlement amounts. '
                  'data_complete=false marks an overview: read_result(result_ref, path="/data") '
                  'retrieves the complete saved record; path="/data/entries" retrieves line items. '
                  'See output.record_overview for field types and meanings. '
                  'Amounts use currency_code; null settlement counters count as zero. '
                  'due_amount is the outstanding amount; credits_remaining is unused credit, where applicable. '
                  'is_fully_paid means due_amount is zero, including settlement by credits, where present.')
    elif document and not returns_document:
        # Do not imply that a mutation, payment lookup or configuration read
        # returns the document amounts and flags from a detail query.
        native.setdefault('output', {})['document_read'] = {
            'tool_name': f'{tool.entity}.{document[0]}',
            'description': 'Read the document separately for its current amounts, dates and business state.'}
        output += f' For current document details use {tool.entity}.{document[0]}.'
        response = native['output'].get('native_response', {}).get('native_schema', {})
        readback = native['output']['document_read']
        if not tool.read_only and response.get('x-native-model'):
            # These commands return an ORM record, not the transformed detail
            # response. Its ID connects the acknowledgement to a fresh read.
            readback.update(id_source='response.data.id', id_parameter='path.id')
            output = (f'data is the native write response, including the document id. '
                      f'Use data.id as path.id in {readback["tool_name"]} '
                      'to read full details and current computed amounts.')
        elif not tool.read_only and response.get('x-native-empty-body'):
            output = 'The receipt status confirms the operation; no document is returned in data.'
            if 'id' in schema.get('properties', {}).get('path', {}).get('properties', {}):
                readback.update(id_source='arguments.path.id', id_parameter='path.id')
                if tool.operation.startswith('delete'):
                    readback['description'] = 'Read the requested ID to check that the deleted document is no longer found.'
                output += f' Use the requested path.id with {readback["tool_name"]} to check the resulting state.'
    if key in {('bigcapital.sale_invoice', 'getSaleInvoiceState'),
               ('bigcapital.credit_note', 'getCreditNoteState')}:
        # Get*State reads application defaults, not an individual document.
        description = 'Read the default PDF template configuration for this document type.'
        output = ('data contains application defaults. '
                  f'Read an individual document with {tool.entity}.{document[0]}.')
    if tool.entity == 'bigcapital.expense' and tool.operation in {
            'deleteExpense', 'bulkDeleteExpenses', 'validateBulkDeleteExpenses'}:
        notes.append('An expense referenced by an allocated landed cost cannot be deleted. '
                     'validateBulkDeleteExpenses identifies deletable and non-deletable expense IDs without retaining deletions.')
        if tool.operation == 'bulkDeleteExpenses':
            notes.append('Bulk deletion processes expenses individually; earlier deletions may remain if a later expense is rejected. '
                         'With skipUndeletable=true, linked expenses are kept and other deletable expenses are removed.')
    if key in NO_WRITE_ERRORS:
        native['no_write_errors'] = sorted(NO_WRITE_ERRORS[key])

    def execute(connection, arguments):
        if allocation:
            document_id = allocation[1]
            ids = [entry[document_id] for entry in arguments['body']['entries']]
            if len(ids) != len(set(ids)):
                raise ToolError('duplicate_allocation_target',
                    f'Use one entry per {document_id}; combine the intended additional amounts for the same document.')
        try:
            return tool.handler(connection, arguments)
        except NativeHTTPFailure as exc:
            if _no_write(exc, key):
                exc.outcome = 'not_started'
            raise

    return replace(tool, input_schema=schema, native_contract=native, description=description,
                   output_description=output, notes=tuple(notes),
                   handler=execute if key in NO_WRITE_ERRORS or allocation else tool.handler)
