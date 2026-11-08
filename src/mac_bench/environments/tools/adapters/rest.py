"""Catalog-declared Pretix and BigCapital endpoints, including native actions."""
from urllib.parse import parse_qs, urlsplit
from copy import deepcopy

from mac_bench.environments.catalog.factories.apps import bigcapital, pretix
from mac_bench.environments.errors import NativeHTTPFailure
from mac_bench.environments.catalog.factories.apps import drf
from mac_bench.environments.tools.adapters import pretix_actions
from mac_bench.environments.tools.adapters.common import (
    ID,
    QUERY,
    exchange,
    fixed_scope,
    merge_schema,
    query_path,
)
from mac_bench.environments.tools.contracts import ToolError, object_schema
from mac_bench.environments.tools.navigation import resolve


def credit_refund_date_schema(body):
    """Reviewed native refund insert uses a SQL DATE, not an ISO timestamp."""
    root = resolve(body, body['$ref']) if '$ref' in body else body
    date = root.get('properties', {}).get('date')
    if not isinstance(date, dict):
        raise ToolError('native_date_contract_missing', 'Credit refund input is missing its date contract.')
    date.update(type='string', format='date', pattern=r'^\d{4}-\d{2}-\d{2}$',
                examples=['2027-01-01'],
                description='Native refund banking date. Use YYYY-MM-DD only; timestamps are rejected by the native SQL DATE column.')


POSITION_PATCH_SERIALIZERS = (
    'pretix.api.serializers.orderchange.OrderPositionChangeSerializer',
    'pretix.api.serializers.orderchange.OrderPositionInfoPatchSerializer',
)


POSITION_CREATE_SERIALIZERS = (
    'pretix.api.serializers.orderchange.OrderPositionCreateForExistingOrderSerializer',
    'pretix.api.serializers.orderchange.OrderPositionInfoPatchSerializer',
)

def pretix_output_description(operation):
    response = operation.get('response', {})
    shapes = response.get('native_shapes', [])
    if response.get('collection'):
        return ('data.results contains this page of records; data.count is the filtered total. '
                'Continue with pagination.next_query and next_raw_query, keeping the same path scope. '
                'data.next=null ends the listing. Native paging and saved-receipt paging are separate.')
    if response.get('empty') or (shapes and all(s.get('kind') == 'empty' for s in shapes)):
        return ('The receipt reports the operation outcome; the native response has no record body. '
                'Use the original path identifiers to retrieve the affected object or check its absence after deletion.')
    if response.get('serializers') and not shapes:
        records = {
            'OrderSerializer': 'an order record, identified by code within its organizer and event',
            'OrderPositionSerializer': 'an order-position record; id identifies the position and order identifies its parent order',
            'OrderRefundSerializer': 'a refund record, including its order-local local_id and processing state',
            'OrderPaymentSerializer': 'a payment record, including its order-local local_id and payment state',
        }
        serializers = response['serializers']
        record = records.get(serializers[0].rsplit('.', 1)[-1]) if len(serializers) == 1 else None
        return (('data is ' + record + '. ') if record else
                'data is the serialized native response; its record type is specified in output.native_response. ') + (
                'Identifiers remain scoped to the parent path. Use retrieve for a fresh observation after subsequent changes.')
    return 'data preserves this operation\'s native return value. See output.native_response for its fields and shape.'


def refine_pretix_contract(binding, operation):
    """Use the pinned native handlers' JSON response and serializer contracts."""
    from ..pretix_queries import LIBRARY
    operation = pretix_actions.refine_contract(binding, operation)
    if operation.get('input', {}).get('library') != LIBRARY:
        return operation
    result = deepcopy(operation)
    entity, action = binding.get('entity'), operation.get('action')
    if entity == 'pretix.item' and action == 'list':
        # ConditionalListView uses HttpResponse only for conditional 304/412.
        # Tools send no conditional headers; ordinary GET returns a JSON page.
        response = result['response']
        response['native_shapes'] = [shape for shape in response.get('native_shapes', [])
            if not (shape.get('kind') == 'http_content'
                    and shape.get('native_constructor') == 'HttpResponse'
                    and shape.get('content_type') is None)]
    if entity == 'pretix.order_position' and action in {'partial_update', 'create'}:
        patch = action == 'partial_update'
        result['input'].update(
            serializers=list(POSITION_PATCH_SERIALIZERS if patch else POSITION_CREATE_SERIALIZERS),
            partial=patch,
            native_handler='pretix.api.views.order.OrderPositionViewSet.' + ('update' if patch else 'create'))
    return result


def pretix_operation(context, binding, name, operation):
    choices = pretix.routes(binding, name)
    if len(choices) != 1:
        raise ToolError('ambiguous_route', 'Operation does not have a unique JSON route.')
    native, path = choices[0]
    method = native['method'].upper()
    contract = operation['input']
    serializers = contract.get('serializers', [])
    position_patch = (binding.get('entity') == 'pretix.order_position'
                      and method == 'PATCH' and tuple(serializers) == POSITION_PATCH_SERIALIZERS)
    position_create = (binding.get('entity') == 'pretix.order_position'
                       and method == 'POST' and native.get('action') == 'create'
                       and tuple(serializers) == POSITION_CREATE_SERIALIZERS)
    if len(serializers) > 1 and not (position_patch or position_create):
        raise ToolError('ambiguous_input', 'Multiple input serializers need an explicit deployment binding.')
    body = drf.schema_for(context.types(contract['library']), serializers[0], writing=True) if serializers else object_schema()
    if position_patch or position_create:
        other = drf.schema_for(context.types(contract['library']), serializers[1], writing=True)
        primary = resolve(body, body['$ref'])
        secondary = resolve(other, other['$ref'])
        # The second serializer is partial; creation keeps its required fields.
        primary['properties'].update(secondary['properties'])
        body['$defs'].update({k: v for k, v in other['$defs'].items() if k not in body['$defs']})
        # Both native serializers have a fixed set of writable field names;
        # dynamic querysets only constrain which referenced records are valid.
        primary['additionalProperties'] = False
        if position_create:
            # This endpoint has event context, not the order context that lets
            # the native serializer omit its order field in nested uses.
            primary['required'] = sorted(set(primary.get('required', [])) | {'order', 'item'})
        primary['description'] = ('Add a ticket position to an existing order, with optional attendee details. '
            'order is the existing order code; item is the product ID within the event. '
            'The server validates availability and recalculates the order total.'
            if position_create else 'Change supplied booking and attendee fields. price is the new position price; '
            'subevent is the occurrence ID within this event. Read the parent order for the updated total; '
            'existing payments are retained and refunds are separate operations.')
    if serializers and contract.get('partial'):
        root = body['$ref'].removeprefix('#/$defs/').replace('~1', '/').replace('~0', '~')
        body['$defs'][root]['required'] = []
    inline = {field: deepcopy(spec.get('public_schema',
              {'description': 'Native action argument; server validates its value.'}))
                              for field, spec in contract.get('inline_fields', {}).items() if spec.get('location') == 'body'}
    inline_required = [field for field, spec in contract.get('inline_fields', {}).items()
                       if spec.get('location') == 'body' and spec.get('access') == 'required']
    if not serializers:
        body = object_schema(inline, inline_required)
    else:
        root = resolve(body, body['$ref']) if '$ref' in body else body
        # Handler-level controls (e.g. send_email) are not serializer fields.
        # Keep serializer validation for existing fields and add reviewed controls.
        for field, spec in contract.get('inline_fields', {}).items():
            if spec.get('public_schema') and field not in root['properties']:
                root['properties'][field] = deepcopy(spec['public_schema'])
    locators = contract.get('locator_fields', [])
    parameters = object_schema({k: ID for k in locators}, locators)
    properties = {'path': parameters}
    required = ['path'] if locators else []
    policy, known_query = None, {}
    if method == 'GET':
        known_query = {field: {'description': 'Captured native query argument; the server validates its value.'}
                       for field, spec in contract.get('inline_fields', {}).items() if spec.get('location') == 'query'}
        from ..pretix_queries import LIBRARY, POLICIES
        policy = POLICIES.get(native.get('view')) if contract['library'] == LIBRARY and native.get('action') == 'list' else None
        if policy:
            known_query.update({key: {'type': ['string', 'number', 'boolean', 'null']}
                                for key in policy['keys']})
        if native.get('action') == 'list':
            known_query.update(page={'type': 'integer', 'minimum': 1},
                               page_size={'type': 'integer', 'minimum': 1, 'maximum': 50})
        properties['query'] = object_schema(known_query)
        properties['query']['description'] = ('Declared native query parameters. Unknown names are rejected before the request, '
            'so a misspelled filter is not silently accepted. Use raw_query only for an intentional, unverified native extension. '
            'For paging reuse pagination.next_query and, if present, next_raw_query.')
        properties['raw_query'] = {**QUERY, 'description': 'Advanced native extension parameters not covered by this contract. '
            'Their filtering effect is unverified and the server may ignore them. Do not duplicate query keys. '
            'Prefer declared query parameters; receipts identify which raw keys were used.'}
    elif method != 'DELETE':
        properties['body'] = body
        required.append('body')
    schema = merge_schema(properties, required)
    shapes = operation.get('response', {}).get('native_shapes', [])
    if any(s.get('kind') in {'stream', 'http_content'} for s in shapes):
        raise ToolError('binary_endpoint', 'Binary downloads require a file delivery adapter.')
    def execute(connection, args):
        url = pretix.render(path, fixed_scope(connection, args.get('path', {})))
        query, raw = args.get('query', {}), args.get('raw_query', {})
        if set(query) & set(raw):
            raise ToolError('query_key_conflict', 'A parameter cannot appear in both query and raw_query.')
        if method == 'GET' and policy and 'ordering' in query and policy.get('ordering_fields'):
            ordering = query['ordering']
            if not isinstance(ordering, str) or any(k.strip().removeprefix('-') not in policy['ordering_fields'] for k in ordering.split(',')):
                raise ToolError('invalid_ordering', 'Use only these ordering fields: ' + ', '.join(policy['ordering_fields']))
        try:
            result = exchange(connection, method, query_path(url, {**query, **raw}), args.get('body'))
        except NativeHTTPFailure as exc:
            if pretix_actions.no_write_rejection(binding, operation, exc):
                exc.outcome = 'not_started'
            raise
        if method == 'GET' and isinstance(result.data, dict) and 'results' in result.data:
            from mac_bench.environments.tools.contracts import NativeResult
            # Never fetch a server-supplied URL. Agent reuses this operation's page/filter arguments.
            next_url = result.data.get('next')
            next_query = None
            if isinstance(next_url, str) and urlsplit(next_url).path.lstrip('/') == urlsplit(url).path.lstrip('/'):
                next_query = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(urlsplit(next_url).query).items()}
                for key in ('page', 'page_size'):
                    if key in next_query and str(next_query[key]).isdigit():
                        next_query[key] = int(next_query[key])
            next_raw = {k: v for k, v in (next_query or {}).items() if k not in known_query}
            if next_query is not None:
                next_query = {k: v for k, v in next_query.items() if k in known_query}
            return NativeResult(result.data, result.status, {'kind': 'native_page', 'next_query': next_query,
                'next_raw_query': next_raw or None, 'unverified_query_keys': sorted(raw),
                'next': result.data.get('next'), 'previous': result.data.get('previous'), 'count': result.data.get('count')})
        if raw:
            from mac_bench.environments.tools.contracts import NativeResult
            return NativeResult(result.data, result.status, {'unverified_query_keys': sorted(raw)})
        return result
    notes = list(operation.get('usage_notes', ()))
    entity = binding['entity']
    if entity == 'pretix.order_position' and native.get('action') == 'redeem':
        schema['properties']['path']['properties']['pk'] = {'type': 'string', 'minLength': 1,
            'description': 'Ticket barcode or secret passed to the native check-in handler, not a numeric position ID.'}
    elif entity == 'pretix.order_position' and 'pk' in locators:
        notes.append('pk is the position id returned by the API, not its order-relative positionid.')
    if position_patch:
        notes.append('PATCH changes supplied price/session and attendee fields. Read the parent order for its updated total; '
                     'existing payments are retained and refunds are separate operations.')
    if position_create:
        notes.append('Creates one position in an existing order. Read the parent order for its updated total; '
                     'existing payments are retained. This does not create a new order or a payment.')
    if entity in {'pretix.order_payment', 'pretix.order_refund'}:
        notes.append('local_id identifies a payment or refund within its parent order; keep organizer, event and order with this ID.')
    if entity in {'pretix.order', 'pretix.order_position'} and (
            method == 'GET' or native.get('action') == 'mark_canceled'):
        notes.append('The parent order status governs whole-order cancellation; individual position canceled flags may remain false.')
    if entity == 'pretix.order_refund':
        notes.append('Creating a refund record is distinct from processing it. Read state to check progress; '
                     'a recorded refund does not verify external bank settlement.')
    if method == 'GET':
        notes.append('Use declared query parameters for filtering. raw_query passes additional native parameters; '
                     'the server may ignore unsupported ones.')
    return schema, execute, method == 'GET', tuple(notes)


def bigcapital_operation(context, binding, name, operation):
    native = bigcapital.route(binding, name)
    method, path = native['method'].upper(), native['path_template']
    types = context.types(operation['input']['library'])
    props, required = {'path': {}, 'query': {}}, {'path': [], 'query': []}
    body = None
    whole_query = None
    for p in operation['input'].get('parameters', []):
        schema = bigcapital.json_schema(p.get('schema', {}), types)
        location = p['in']
        if location == 'body':
            if body is not None:
                raise ToolError('ambiguous_input', 'Multiple native request bodies.')
            body = schema
        elif location in props and p.get('name'):
            props[location][p['name']] = schema
            if location == 'path' or p.get('required'):
                required[location].append(p['name'])
        elif location == 'query' and not p.get('name'):
            whole_query = schema
    sections = {}
    for key in ['path', 'query']:
        sections[key] = whole_query if key == 'query' and whole_query is not None else object_schema(props[key], required[key])
    if body is not None:
        if binding['entity'] == 'bigcapital.vendor_credit_applied_bill' and name == 'applyVendorCreditToBills':
            root = resolve(body, body['$ref']) if '$ref' in body else body
            entries = root['properties']['entries']
            entries['minItems'] = 1
            entry = entries['items']
            if '$ref' in entry:
                entry = resolve(body, entry['$ref'])
            entry['properties']['billId']['minimum'] = 1
            entry['properties']['amount']['exclusiveMinimum'] = 0
        if (binding['entity'], name) in {
            ('bigcapital.refund_credit_note', 'createRefundCreditNote'),
            ('bigcapital.refund_vendor_credit', 'createRefundVendorCredit'),
        }:
            credit_refund_date_schema(body)
        sections['body'] = body
    schema = merge_schema(sections, [k for k in ['path', 'query'] if required[k]] + (['body'] if body is not None else []))
    def execute(connection, args):
        url = bigcapital.render(path, fixed_scope(connection, args.get('path', {})))
        return exchange(connection, method, query_path(url, args.get('query', {})), args.get('body'),
                        rate_limit_key=path)
    return schema, execute, method == 'GET', (
        'Body keys use the captured native DTO names. Responses retain actual application wire keys.',
        'Authentication and organization headers are injected by the bound native connection.',
        'HTTP 202 means accepted, not completed. Read the native object before relying on the outcome.',
        'Internal ledger posting does not prove external bank settlement.',
        *(['Refund date is YYYY-MM-DD; omit branchId when no branch applies.']
          if (binding['entity'], name) in {
              ('bigcapital.refund_credit_note', 'createRefundCreditNote'),
              ('bigcapital.refund_vendor_credit', 'createRefundVendorCredit'),
          } else []),
    )
