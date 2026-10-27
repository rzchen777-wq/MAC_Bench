"""Single registered GraphQL root operations with validated field projections."""
import re
from copy import deepcopy

from graphql import (
    Undefined,
    build_client_schema,
    get_named_type,
    is_enum_type,
    is_input_object_type,
    is_list_type,
    is_non_null_type,
    is_scalar_type,
)

from mac_bench.construction.realize.apps.twenty.application import TwentyGraphQL
from mac_bench.construction.realize.catalog.factories.apps.twenty import SCALARS
from mac_bench.construction.realize.tools.contracts import (
    NativeResult,
    ToolError,
    object_schema,
)


# ConnectedAccount is not a public task resource. Its credential fields must
# also stay private when reached through an otherwise permitted calendar or
# contact relation; restricting root bindings alone does not restrict links.
PRIVATE_ACCOUNT_FIELDS = frozenset({'accessToken', 'refreshToken', 'connectionParameters'})


def captured_schema(context, library):
    if not hasattr(context, '_tool_graphql'):
        context._tool_graphql = {}
    if library not in context._tool_graphql:
        document = {'__schema': {'types': list(context.types(library).values()),
            'queryType': {'name': 'Query'}, 'mutationType': {'name': 'Mutation'},
            'subscriptionType': None, 'directives': []}}
        context._tool_graphql[library] = (document, build_client_schema(document))
    return context._tool_graphql[library]


def argument_schema(arguments):
    definitions = {}
    def typed(t, *, date_filter=False):
        if is_non_null_type(t):
            return core(t.of_type, date_filter=date_filter)
        return {'anyOf': [core(t, date_filter=date_filter), {'type': 'null'}]}
    def core(t, *, date_filter=False):
        if is_list_type(t):
            return {'type': 'array', 'items': typed(t.of_type, date_filter=date_filter)}
        if is_input_object_type(t):
            if t.name not in definitions:
                definitions[t.name] = {}
                definitions[t.name] = object_schema({k: typed(f.type,
                    date_filter=t.name.endswith('FilterInput')) for k, f in t.fields.items()},
                    [k for k, f in t.fields.items() if is_non_null_type(f.type) and f.default_value is Undefined])
            return {'$ref': '#/$defs/' + t.name}
        if is_enum_type(t):
            return {'enum': list(t.values)}
        if date_filter and t.name == 'DateTime':
            # Twenty's introspection exposes DateTime here, but its filter
            # resolver requires a comparison object. Creation/update dates
            # keep their ordinary scalar wire contract.
            stamp = {'type': 'string', 'description': 'ISO 8601 date-time string.'}
            return {**object_schema({
                **{op: deepcopy(stamp) for op in ('eq', 'neq', 'gt', 'gte', 'lt', 'lte')},
                'in': {'type': 'array', 'items': deepcopy(stamp)},
                'is': {'type': 'string', 'enum': ['NULL', 'NOT_NULL']}}),
                'minProperties': 1, 'maxProperties': 1,
                'description': 'Use exactly one date comparison operator per object. '
                    'For a range, combine separate filters using and, for example '
                    '{"and":[{"startsAt":{"gte":"2027-01-01T00:00:00Z"}},'
                    '{"startsAt":{"lt":"2027-04-01T00:00:00Z"}}]}.',
                'examples': [{'gte': '2027-01-01T00:00:00Z'}, {'is': 'NULL'}]}
        return deepcopy({'Int': {'type': 'integer'}, 'Float': {'type': 'number'},
            'Boolean': {'type': 'boolean'}, 'String': {'type': 'string'}, 'ID': {'type': ['string', 'integer']}}
            .get(t.name, SCALARS.get(t.name, {})))
    result = object_schema({k: typed(v.type) for k, v in arguments.items()},
        [k for k, v in arguments.items() if is_non_null_type(v.type) and v.default_value is Undefined])
    return {**result, '$defs': definitions}


def default_fields(type_, prefix='', ancestry=(), depth=0):
    named = get_named_type(type_)
    if is_scalar_type(named) or is_enum_type(named):
        return [prefix] if prefix else []
    if named.name in ancestry or depth > 4 or (depth > 0 and named.name.endswith('Connection')):
        return []
    # A related record is a separate resource, not a compound value such as a
    # person's name or a monetary amount. Do not recursively fetch its graph.
    # The node of a root connection is the requested record itself.
    connection_node = prefix.endswith('.node') and ancestry and ancestry[-1].endswith('Edge')
    if depth > 0 and 'id' in getattr(named, 'fields', {}) and not connection_node:
        return []
    paths = []
    for name, field in getattr(named, 'fields', {}).items():
        if name in PRIVATE_ACCOUNT_FIELDS:
            continue
        # Connection-level scalars in Twenty include computed analytics, not
        # just pagination metadata. Ordinary reads should not implicitly run
        # every aggregate resolver (including aggregates over relation fields).
        # Keep the full captured output contract for explicit selection.
        if depth == 0 and named.name.endswith('Connection') and name not in {'edges', 'pageInfo'}:
            continue
        if field.args:
            continue
        paths.extend(default_fields(field.type, prefix + ('.' if prefix else '') + name, (*ancestry, named.name), depth + 1))
    return paths


def projection(paths):
    tree = {}
    for path in paths:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*', path):
            raise ToolError('invalid_projection', 'Fields must be dotted native field paths, without GraphQL expressions.')
        if PRIVATE_ACCOUNT_FIELDS.intersection(path.split('.')):
            raise ToolError('private_account_projection', 'Account credentials are not available through business tools.')
        node = tree
        for segment in path.split('.'):
            node = node.setdefault(segment, {})
    def render(node):
        return ' '.join(key + (' { ' + render(child) + ' }' if child else '') for key, child in sorted(node.items()))
    return render(tree)


def operation(context, binding, name, contract):
    native = binding['operation_bindings'][name]
    if len(native) != 1:
        raise ToolError('ambiguous_graphql_root', 'No unique root operation binding.')
    native = native[0]
    document, schema = captured_schema(context, contract['input']['library'])
    read_only = native['root'] == 'Query'
    root = schema.query_type if read_only else schema.mutation_type
    field = root.fields[native['name']]
    defaults = default_fields(field.type)
    args_schema = argument_schema(field.args)
    if 'fields' in field.args:
        raise ToolError('projection_argument_conflict', 'Native root uses the reserved fields argument.')
    tool_schema = args_schema
    tool_schema['properties']['fields'] = {
        'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 200,
        'description': 'Dotted response paths, e.g. id or edges.node.id for a connection. '
                       'Defaults return records and pagination. Linked records and connection statistics '
                       '(including totalCount) require explicit selection.'}
    def execute(connection, args):
        supplied = {k: v for k, v in args.items() if k != 'fields'}
        variables = ', '.join(f'${k}: {field.args[k].type}' for k in supplied)
        arguments = ', '.join(f'{k}: ${k}' for k in supplied)
        selected = args.get('fields', defaults)
        suffix = ' { ' + projection(selected) + ' }' if defaults else ''
        kind = 'query' if read_only else 'mutation'
        query = f'{kind} AgentOperation' + ('(' + variables + ')' if variables else '') + ' { ' + native['name']
        query += ('(' + arguments + ')' if arguments else '') + suffix + ' }'
        client = TwentyGraphQL(connection.transport, 'graphql', document)
        try:
            client.operation(kind, query, 'AgentOperation')
        except Exception:
            raise ToolError('invalid_projection', 'The selected fields are not valid for this registered native operation.') from None
        data = (client.query if read_only else client.mutate)(query, supplied, operation_name='AgentOperation')
        result = data[native['name']]
        page = result.get('pageInfo') if isinstance(result, dict) else None
        return NativeResult(result, pagination=page)
    notes = (
        'Only this registered root operation executes. Fields are dotted response paths; raw GraphQL is not accepted.',
        'Account credentials and connection parameters remain private, including through linked records.',
        'Use native UUIDs and relation IDs. Related objects have distinct native identities.',
        'Connection defaults return edges/node and pageInfo. Pass after/first or other declared pagination arguments. '
        'Statistics such as totalCount are opt-in through fields; listing records does not run aggregate queries.',
        'Default fields include the requested record and compound values, not linked records. Request relation paths explicitly when needed.',
    )
    entity = binding.get('entity', '')
    if entity == 'twenty.calendar_event':
        notes += ('Calendar events must be linked through calendarChannelEventAssociation to a visible calendarChannel. '
                  'Creation alone does not make an event available to list queries. Use a visible calendar ID supplied by the task or found through calendar-channel reads.',)
    elif entity == 'twenty.calendar_channel_event_association':
        notes += ('Supply calendarEventId and calendarChannelId to link an existing event to its calendar. '
                  'A SHARE_EVERYTHING channel makes event details visible to the workspace. A stable local eventExternalId '
                  'and empty recurringEventExternalId work for a manual, non-recurring appointment.',)
    elif entity == 'twenty.calendar_channel':
        notes += ('A calendar channel belongs to an existing connectedAccountId. SHARE_EVERYTHING exposes event details; '
                  'METADATA restricts title and description for users who do not own the account.',)
    return tool_schema, execute, read_only, notes


def response_contract(context, binding, name, contract):
    """Navigable output types; never dump this type graph in the usage card."""
    native = binding['operation_bindings'][name][0]
    _, schema = captured_schema(context, contract['input']['library'])
    root = schema.query_type if native['root'] == 'Query' else schema.mutation_type
    field = root.fields[native['name']]
    if not hasattr(context, '_tool_output_types'):
        context._tool_output_types = {}
    types = context._tool_output_types.setdefault(contract['input']['library'], {})
    def collect(type_):
        named = get_named_type(type_)
        if named.name in types:
            return
        types[named.name] = {}
        if is_scalar_type(named):
            types[named.name] = {'kind': 'scalar', 'name': named.name}
        elif is_enum_type(named):
            types[named.name] = {'kind': 'enum', 'values': list(named.values)}
        elif hasattr(named, 'fields'):
            types[named.name] = {'kind': 'object', 'fields': {
                k: {'type': str(v.type), 'description': v.description or '',
                    'selection_arguments': list(v.args)} for k, v in named.fields.items()}}
            for v in named.fields.values():
                collect(v.type)
        else:
            types[named.name] = {'kind': 'union', 'possible_types': [t.name for t in getattr(named, 'types', ())]}
            for t in getattr(named, 'types', ()):
                collect(t)
    collect(field.type)
    return {'root_type': str(field.type), 'types': types, 'default_fields': default_fields(field.type),
            'selection': 'fields contains dotted paths starting at the root result. Connections default to edges.node and pageInfo. '
                         'Connection statistics remain available through explicit selection and are evaluated by the native app.'}
