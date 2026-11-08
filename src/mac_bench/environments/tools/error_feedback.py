"""Expose business rejection reasons and record locators without server internals."""

from html import unescape
import json
import re

from mac_bench.environments.errors import NativeGraphQLFailure, NativeHTTPFailure
from mac_bench.environments.tools.contracts import encoded

RULES = {
    'unsupported_operation': ('refund not available for this payment method',
                              'automatic refunds are not supported',
                              'operation is not supported'),
    'required': ('required', 'mandatory', 'missing value', 'must not be empty'),
    'invalid_choice': ('valid choice', 'not one of', 'invalid enum', 'must be one of'),
    'invalid_reference': ('does not exist', 'not found', 'invalid link'),
    'invalid_type': ('valid integer', 'valid number', 'valid date', 'expected type'),
    'conflict': ('already exists', 'duplicate', 'changed since', 'overlap'),
}

FILTER_OPERATORS = frozenset(('eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'in', 'is',
                             'like', 'ilike', 'startsWith', 'endsWith', 'regex', 'iregex'))

# Bigcapital CreditNotes / CreditNotesApplyInvoice / PaymentReceived validators.
# Interpret the stable service code, never an arbitrary error message or stack.
CREDIT_ERRORS = {
    'EXPENSE_HAS_ASSOCIATED_LANDED_COST': (
        'id', 'invalid_state', 'This expense is referenced by an allocated landed cost and cannot be deleted.',
        'Keep the expense while the allocation exists; deletion requires removing that allocation first.'),
    'BILL_ENTRIES_IDS_NOT_FOUND': (
        'billId', 'invalid_reference', 'One or more bills could not be found for this vendor.',
        'Check the bill IDs and vendor IDs on the bills and vendor credit.'),
    'BILLS_NOT_OPENED_YET': (
        'billId', 'invalid_state', 'One or more selected bills have not been opened.',
        'Read the bill lifecycle state before applying vendor credit.'),
    'BILLS_HAS_NO_REMAINING_AMOUNT': (
        'amount', 'insufficient_balance', 'The requested credit exceeds the outstanding amount on one or more bills.',
        'Use each bill\'s current due_amount as the upper limit for its additional allocation.'),
    'VENDOR_CREDIT_HAS_NO_REMAINING_AMOUNT': (
        'amount', 'insufficient_balance', 'The requested amount exceeds the vendor credit\'s remaining unused credit.',
        'Read credits_remaining and existing bill allocations before choosing additional amounts.'),
    'CREDIT_NOTE_APPLY_TO_INVOICES_NOT_FOUND': (
        'creditNoteAppliedInvoiceId', 'invalid_reference',
        'The selected customer credit allocation does not exist or has already been removed.',
        'List appliedCreditNoteToInvoices for the credit note and use the allocation row id, not the invoice or credit note id.'),
    'VENDOR_CREDIT_APPLY_TO_BILLS_NOT_FOUND': (
        'vendorCreditAppliedBillId', 'invalid_reference',
        'The selected vendor credit allocation does not exist or has already been removed.',
        'List getAppliedBillsToVendorCredit and use the allocation row id. Customer credit allocations use deleteAppliedInvoiceToCreditNote.'),
    'INVOICES_HAS_NO_REMAINING_AMOUNT': (
        'amount', 'insufficient_balance',
        'The requested credit exceeds the outstanding amount on one or more invoices.',
        'Read the selected invoices and their payment/credit allocations. Apply credit only up to '
        'the outstanding amount; applying credit and refunding a received payment are different operations.'),
    'CREDIT_NOTE_HAS_NO_REMAINING_AMOUNT': (
        'amount', 'insufficient_balance',
        'The requested amount exceeds the credit note\'s remaining unused credit.',
        'Read the credit note, existing invoice applications and refunds before choosing the amount.'),
    'INVOICES_IDS_NOT_FOUND': (
        'invoiceId', 'invalid_reference',
        'One or more invoices could not be found for this customer.',
        'Check the invoice IDs and customer IDs on both the invoices and the credit/payment document.'),
    'INVOICES_NOT_DELIVERED_YET': (
        'invoiceId', 'invalid_state',
        'One or more selected invoices have not been marked as delivered.',
        'Read each invoice\'s current lifecycle state and the available lifecycle operations.'),
    'CREDIT_NOTE_NOT_FOUND': (
        'creditNoteId', 'invalid_reference', 'The selected credit note does not exist.',
        'Find the existing credit note and use its returned native ID.'),
    'CREDIT_NOTE_AMOUNT_SMALLER_THAN_USED': (
        'amount', 'insufficient_balance',
        'The new credit amount is below the amount already applied or refunded.',
        'Read the existing applications and refunds before editing this credit note.'),
    'CREDIT_NOTE_CUSTOMER_HAS_SETTLEMENTS': (
        'customerId', 'invalid_state',
        'A credit note with applied or refunded credits cannot change customer.',
        'Keep its original customer and inspect the existing applications and refunds.'),
    'VENDOR_CREDIT_AMOUNT_SMALLER_THAN_USED': (
        'amount', 'insufficient_balance',
        'The new credit amount is below credits already applied or refunded.',
        'Read the vendor credit and its allocations/refunds before editing.'),
    'VENDOR_CREDIT_VENDOR_HAS_SETTLEMENTS': (
        'vendorId', 'invalid_state',
        'A settled vendor credit cannot change vendor.',
        'Keep the original vendor and inspect existing settlements.'),
    'INVOICE_AMOUNT_SMALLER_THAN_SETTLED': (
        'entries', 'insufficient_balance',
        'The invoice total is below its payments, credits and write-offs.',
        'Read the invoice and retain its existing settlements when editing.'),
    'INVOICE_CUSTOMER_HAS_SETTLEMENTS': (
        'customerId', 'invalid_state',
        'A settled invoice cannot change customer.',
        'Keep the original customer and inspect existing settlements.'),
    'BILL_AMOUNT_SMALLER_THAN_SETTLED': (
        'entries', 'insufficient_balance',
        'The bill total is below its payments and applied credits.',
        'Read the bill and retain its existing settlements when editing.'),
    'BILL_VENDOR_HAS_SETTLEMENTS': (
        'vendorId', 'invalid_state',
        'A settled bill cannot change vendor.',
        'Keep the original vendor and inspect existing settlements.'),
    'INVALID_VENDOR_CREDIT_ALLOCATION': (
        'entries', 'invalid_value',
        'Allocations need distinct positive bill IDs and positive amounts.',
        'Use one entry per bill and read each bill balance before allocating.'),
    'VENDOR_CREDIT_HAS_NO_CREDITS_REMAINING': (
        'amount', 'insufficient_balance',
        'The refund exceeds the vendor credit remaining balance.',
        'Read the vendor credit and its existing refunds and allocations.'),
    'CREDIT_NOTE_ALREADY_OPENED': (
        'creditNoteId', 'invalid_state', 'This credit note is already open.',
        'Read its current state instead of repeating the open operation.'),
}


def business_rejection(payload, tool, status):
    """Recognize public business errors; preserve the facts needed to correct them."""
    if not isinstance(payload, dict):
        return None
    application = getattr(tool, 'application', None)
    if application == 'pretix':
        from .adapters.pretix_actions import state_rejection
        if result := state_rejection(tool, payload, status):
            return result
    if application == 'bigcapital' and isinstance(payload.get('errors'), list):
        for error in payload['errors'][:30]:
            kind = error.get('type') if isinstance(error, dict) else None
            if isinstance(kind, str) and kind in CREDIT_ERRORS:
                field, rule, message, guidance = CREDIT_ERRORS[kind]
                return {'code': 'native_business_rule_rejected', 'http_status': status,
                        'native_error_type': kind,
                        'field_errors': [{'field': field, 'rule': rule}],
                        'message': message,
                        'retry_guidance': guidance + ' Read current state before retrying a write.'}
    if application != 'hrms':
        return None

    # Frappe's _server_messages is a JSON list of JSON-encoded message objects.
    # Read only that user-facing channel, not `exc`, headers, or SQL diagnostics.
    def messages(value, depth=0):
        if depth > 5:
            return
        if isinstance(value, dict):
            for key in ('_server_messages', 'message'):
                if key in value:
                    yield from messages(value[key], depth + 1)
        elif isinstance(value, list):
            for child in value[:12]:
                yield from messages(child, depth + 1)
        elif isinstance(value, str) and len(value) <= 8000:
            if value.startswith(('[', '{', '"')):
                try:
                    yield from messages(json.loads(value), depth + 1)
                    return
                except ValueError:
                    pass
            yield ' '.join(unescape(re.sub(r'<[^>]*>', '', value)).split())

    for message in messages(payload):
        match = re.fullmatch(
            r'Employee (.{1,160}?) already has an active Shift (.{1,160}?): '
            r'([A-Za-z0-9_./-]{1,160}) that overlaps within this period\.', message)
        if match:
            employee, shift_type, record = match.groups()
            return {'code': 'native_shift_overlap', 'http_status': status,
                    'native_error_type': 'OverlappingShiftError',
                    'field_errors': [{'field': 'employee', 'rule': 'conflict'}],
                    'conflict': {'employee': employee, 'shift_type': shift_type,
                                 'doctype': 'Shift Assignment', 'name': record},
                    'message': f'Employee {employee} already has an active overlapping shift '
                               f'{record} ({shift_type}).',
                    'retry_guidance': 'Read the conflicting Shift Assignment and both Shift Types '
                                      'to compare dates and daily start/end times. Reuse an existing '
                                      'assignment when it represents the same duty; otherwise plan '
                                      'non-overlapping work. Changing request_id does not remove the conflict.'}
    return None


def filter_restriction(payload, fields):
    """Parse only a known native validation shape; never forward arbitrary text."""
    native_errors = payload.get('errors')
    if not isinstance(native_errors, (tuple, list)):
        return None
    for error in native_errors[:30]:
        if not isinstance(error, dict) or not isinstance(error.get('extensions'), dict):
            continue
        if error['extensions'].get('subCode') != 'INVALID_ARGS_FILTER':
            continue
        message = error.get('message')
        if not isinstance(message, str) or len(message) > 1000:
            continue
        match = re.fullmatch(r'Operator "([A-Za-z]+)" is not valid for field "([A-Za-z_][A-Za-z0-9_]*)" of type [A-Z_]+ - Allowed operators: ([A-Za-z, ]+)', message)
        if not match:
            continue
        operator, field, allowed = match.groups()
        allowed = [word.strip() for word in allowed.split(',')]
        if operator in FILTER_OPERATORS and field in fields and allowed and all(word in FILTER_OPERATORS for word in allowed):
            return {'field': field, 'rule': 'unsupported_filter_operator',
                    'operator': operator, 'allowed_operators': allowed}
    return None


def feedback(exc, tool):
    if not isinstance(exc, (NativeHTTPFailure, NativeGraphQLFailure)):
        return None
    payload = exc.native_payload if isinstance(exc, NativeHTTPFailure) else {'errors': exc.native_errors}
    if detail := business_rejection(payload, tool, getattr(exc, 'status', None)):
        if isinstance(exc, NativeHTTPFailure) and exc.outcome == 'not_started':
            detail['message'] += ' No changes were made by this request.'
            detail['retry_guidance'] = detail['retry_guidance'].removesuffix(
                ' Read current state before retrying a write.')
        return detail
    # This is a fixed native business error identifier, not arbitrary server
    # text or submitted values. Hiding it makes a recoverable item prerequisite
    # indistinguishable from malformed JSON or an invalid financial document.
    if (isinstance(exc, NativeHTTPFailure) and isinstance(payload, dict)
            and isinstance(payload.get('errors'), list)
            and any(isinstance(error, dict) and error.get('type') == 'NOT_SELL_ABLE_ITEMS'
                    for error in payload['errors'][:30])):
        return {'code': 'native_item_capability_rejected', 'http_status': exc.status,
                'native_error_type': 'NOT_SELL_ABLE_ITEMS', 'field_errors': [],
                'message': 'The native application reports NOT_SELL_ABLE_ITEMS. Inspect the line-item '
                           'capabilities and the operation requirements. Preserve the task-required '
                           'original documents and amounts. Read current state before another write; '
                           'this receipt does not establish rollback.'}
    fields = set(tool.native_contract.get('fields', {})) | set(tool.input_schema.get('properties', {}))
    if isinstance(exc, NativeGraphQLFailure) and (restriction := filter_restriction(payload, fields)):
        return {'code': 'unsupported_filter_operator', 'http_status': None,
                'field_errors': [restriction],
                'message': 'The native app does not support this filter operator for the named field, '
                           'even when its GraphQL input schema lists it. Use a listed allowed operator '
                           'with its own semantics; do not assume operators are interchangeable.'
                           + ('' if tool.read_only else ' Read current state before another write; rollback is not established.')}
    errors = []
    schema_mismatch = False
    def visit(value, field=None, depth=0):
        nonlocal schema_mismatch
        if depth > 8 or len(errors) >= 12:
            return
        if isinstance(value, dict):
            # Common DTO/GraphQL field paths; only captured field names survive.
            location = value.get('property', value.get('field'))
            path = value.get('path', [])
            if isinstance(location, str) and location in fields:
                field = location
            if isinstance(path, list):
                field = next((x for x in reversed(path) if isinstance(x, str) and x in fields), field)
            for k, v in value.items():
                if k not in {'stack', 'stacktrace', 'traceback', 'exception', 'exc', 'headers'}:
                    visit(v, k if k in fields else field, depth + 1)
        elif isinstance(value, list):
            for v in value[:30]:
                visit(v, field, depth + 1)
        elif isinstance(value, str):
            # Frappe server messages are sometimes JSON encoded twice.
            if value.startswith(('[', '{')):
                try:
                    visit(json.loads(value), field, depth + 1)
                except ValueError:
                    pass
            lowered = value.casefold()
            if re.search(r'\b(column|relation|table)\b[^\n]{1,200}\bdoes not exist\b', lowered):
                schema_mismatch = True
                return
            rule = next((k for k, words in RULES.items() if any(w in lowered for w in words)), None)
            if rule:
                item = {'rule': rule}
                if field:
                    item['field'] = field
                if item not in errors:
                    errors.append(item)
    visit(payload)
    # Enrich native choice failures with the published contract, not an opaque
    # native payload or a guess about the correct business decision.
    def choices_for(field):
        values = []
        def scan(schema):
            if isinstance(schema, dict):
                definition = schema.get('properties', {}).get(field, {})
                choices = definition.get('enum', []) if isinstance(definition, dict) else []
                if choices and choices not in values:
                    values.append(choices)
                for child in schema.values():
                    scan(child)
            elif isinstance(schema, list):
                for child in schema:
                    scan(child)
        scan(tool.input_schema)
        return values[0] if len(values) == 1 and len(encoded(values[0])) <= 500 else None
    for item in errors:
        if item.get('field') and item['rule'] == 'invalid_choice':
            choices = choices_for(item['field'])
            if choices:
                item['allowed_values'] = choices
    status = getattr(exc, 'status', None)
    if schema_mismatch:
        return {'code': 'native_schema_mismatch', 'http_status': status, 'field_errors': [],
                'message': 'The native application could not resolve a database field or relation required by this operation. '
                           'This is not evidence of an invalid record ID. For reads, try an explicit selection of basic '
                           'record fields. For writes, inspect current state before another attempt; rollback is not established.'}
    code = {401: 'authentication_failed', 403: 'permission_denied', 404: 'record_or_route_not_found',
            409: 'native_conflict', 422: 'native_validation_failed', 400: 'native_request_rejected'}.get(status, 'native_validation_failed' if errors else 'native_call_failed')
    if any(item['rule'] == 'unsupported_operation' for item in errors):
        return {'code': 'unsupported_native_operation', 'http_status': status,
                'field_errors': errors,
                'message': 'The current native provider does not support this operation. '
                           'Inspect available alternative lifecycle operations; changing the amount '
                           'does not establish support. Read current state before a different write; '
                           'this receipt does not establish rollback.'}
    return {'code': code, 'http_status': status, 'field_errors': errors,
            'message': 'The native app rejected or failed this request. Inspect the listed fields and input/field contracts. '
                       + ('Read current state before another write; this receipt does not establish rollback.' if not tool.read_only else 'Correct the request or check access before a fresh read.')}
