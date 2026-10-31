"""Recover safe field-specific guidance, not native tracebacks or input values."""

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
