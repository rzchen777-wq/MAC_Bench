"""Catalog-declared Pretix and BigCapital endpoints, including native actions."""
from urllib.parse import parse_qs, urlsplit

from mac_bench.construction.realize.catalog.factories.apps import bigcapital, pretix
from mac_bench.construction.realize.tools.adapters import drf
from mac_bench.construction.realize.tools.adapters.common import (
    ID,
    QUERY,
    exchange,
    fixed_scope,
    merge_schema,
    query_path,
)
from mac_bench.construction.realize.tools.contracts import ToolError, object_schema
from mac_bench.construction.realize.tools.navigation import resolve


def credit_refund_date_schema(body):
    """Reviewed native refund insert uses a SQL DATE, not an ISO timestamp."""
    root = resolve(body, body['$ref']) if '$ref' in body else body
    date = root.get('properties', {}).get('date')
    if not isinstance(date, dict):
        raise ToolError('native_date_contract_missing', 'Credit refund input is missing its date contract.')
    date.update(type='string', format='date', pattern=r'^\d{4}-\d{2}-\d{2}$',
                examples=['2027-01-01'],
                description='Native refund banking date. Use YYYY-MM-DD only; timestamps are rejected by the native SQL DATE column.')


def pretix_operation(context, binding, name, operation):
    choices = pretix.routes(binding, name)
    if len(choices) != 1:
        raise ToolError('ambiguous_route', 'Operation does not have a unique JSON route.')
    native, path = choices[0]
    method = native['method'].upper()
    contract = operation['input']
    serializers = contract.get('serializers', [])
    if len(serializers) > 1:
        raise ToolError('ambiguous_input', 'Multiple input serializers need an explicit deployment binding.')
    body = drf.schema_for(context.types(contract['library']), serializers[0], writing=True) if serializers else object_schema()
    if serializers and contract.get('partial'):
        root = body['$ref'].removeprefix('#/$defs/').replace('~1', '/').replace('~0', '~')
        body['$defs'][root]['required'] = []
    if not serializers:
        body = object_schema({field: {'description': 'Native action argument; server validates its value.'}
                              for field, spec in contract.get('inline_fields', {}).items() if spec.get('location') == 'body'},
                             [field for field, spec in contract.get('inline_fields', {}).items()
                              if spec.get('location') == 'body' and spec.get('access') == 'required'])
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
        result = exchange(connection, method, query_path(url, {**query, **raw}), args.get('body'))
        if method == 'GET' and isinstance(result.data, dict) and 'results' in result.data:
            from mac_bench.construction.realize.tools.contracts import NativeResult
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
            from mac_bench.construction.realize.tools.contracts import NativeResult
            return NativeResult(result.data, result.status, {'unverified_query_keys': sorted(raw)})
        return result
    return schema, execute, method == 'GET', (
        'Path parameters include parent scope: payment/refund local_id is unique only within its order.',
        'A whole-order cancellation uses the parent order lifecycle; child position flags may remain unchanged.',
        'Creating a refund record is distinct from processing it. Native accepted/recorded state does not prove external bank settlement.',
        'Declared query keys are validated; raw_query explicitly opts into unverified extensions. Inspect returned rows; a successful request is not proof of a filter effect.',
    )


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
        if binding['entity'] == 'bigcapital.refund_credit_note' and name == 'createRefundCreditNote':
            credit_refund_date_schema(body)
        sections['body'] = body
    schema = merge_schema(sections, [k for k in ['path', 'query'] if required[k]] + (['body'] if body is not None else []))
    def execute(connection, args):
        url = bigcapital.render(path, fixed_scope(connection, args.get('path', {})))
        return exchange(connection, method, query_path(url, args.get('query', {})), args.get('body'))
    return schema, execute, method == 'GET', (
        'Body keys use the captured native DTO names. Responses retain actual application wire keys.',
        'Authentication and organization headers are injected by the bound native connection.',
        'HTTP 202 means accepted, not completed. Read the native object before relying on the outcome.',
        'Internal ledger posting does not prove external bank settlement.',
        *(['Refund date is YYYY-MM-DD; omit branchId when no branch applies.']
          if binding['entity'] == 'bigcapital.refund_credit_note' and name == 'createRefundCreditNote' else []),
    )
