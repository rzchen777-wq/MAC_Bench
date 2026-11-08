"""HRMS document operations with native lifecycle and optimistic version checks."""
from copy import deepcopy
from urllib.parse import quote

from mac_bench.environments.catalog.factories.apps.frappe import (
    CatalogFrappeClient,
)
from mac_bench.environments.tools.adapters.common import (
    exchange,
    merge_schema,
    query_path,
)
from mac_bench.environments.tools.contracts import (
    NativeResult,
    ToolError,
    object_schema,
)

MANAGED = {'doctype', 'name', 'docstatus', 'modified', 'modified_by', 'creation', 'owner',
           'flags', 'ignore_permissions', 'ignore_links', 'ignore_mandatory', 'parent', 'parenttype', 'parentfield'}


def output_description(operation):
    if operation == 'list':
        return ('data is a list of rows containing the requested fields (name only by default). '
                'Use name to read a full document, including child tables. '
                'Continue with pagination.next_offset and the same filters, fields and limit; '
                'a null next_offset ends the listing.')
    if operation == 'delete':
        return ('The receipt confirms deletion of the requested document; data is the native '
                'deletion response, not the deleted document. To check absence, list this entity '
                'with filters.name equal to the submitted name.')
    prefix = {
        'read': 'data is the current document, or the requested fields with name, modified and docstatus retained when present. ',
        'create': 'data is the document returned by insertion, including its assigned name. ',
        'update': 'data is the document read back after saving the supplied changes. ',
        'submit': 'data is the document read back after submission (docstatus=1). ',
        'cancel': 'data is the document read back after cancellation (docstatus=2). ',
    }[operation]
    return prefix + ('name is the document identity; modified is its version for the next write. '
                     'docstatus is the document lifecycle (0=draft, 1=submitted, 2=cancelled), '
                     'separate from any business field named status.')


def values_schema(context, binding):
    definitions = {}
    def document(selected, root=False):
        key = selected['entity']
        if key in definitions:
            return {'$ref': '#/$defs/' + key}
        definitions[key] = {}
        properties = {} if root else {
            'name': {'type': 'string', 'description': 'Preserve this native child-row identity when editing an existing row.'},
            'doctype': {'const': selected['native_resource']},
        }
        for name, f in context.contracts[selected['id']]['fields'].items():
            if name in MANAGED or f.get('write', {}).get('read_only'):
                continue
            kind = f['native_type']
            constraints = f.get('constraints', {})
            refs = f.get('reference', {})
            if refs.get('kind') == 'owned_component':
                targets = refs.get('targets', [])
                if len(targets) != 1:
                    raise ToolError('ambiguous_child_type', 'A child table has no unique native document type.')
                schema = {'type': 'array', 'items': document(context.target(targets[0], selected['id']))}
            elif kind == 'Check':
                schema = {'enum': [0, 1, False, True]}
            elif kind in {'Int', 'Long Int'}:
                schema = {'type': 'integer'}
            elif kind in {'Float', 'Currency', 'Percent', 'Duration', 'Rating'}:
                schema = {'type': ['number', 'string']}
            elif kind == 'JSON':
                schema = {}
            else:
                schema = {'type': 'string'}
            if kind == 'Date':
                schema['format'] = 'date'
            elif kind == 'Datetime':
                schema['description'] = 'Native date-time string; use YYYY-MM-DD HH:MM:SS in the application timezone.'
                schema['examples'] = ['2027-01-01 09:00:00']
            elif kind == 'Time':
                schema['examples'] = ['09:00:00']
            if 'enum' in constraints:
                schema['enum'] = constraints['enum']
            if schema.get('type') == 'string' and constraints.get('max_length'):
                schema['maxLength'] = constraints['max_length']
            if schema.get('type') == 'integer' and constraints.get('non_negative'):
                schema['minimum'] = 0
            description = f.get('description') or f.get('label') or name
            if schema.get('description'):
                description += '. ' + schema['description']
            if not constraints.get('required') and not constraints.get('not_nullable'):
                schema = {'anyOf': [schema, {'type': 'null'}]}
            properties[name] = {**schema, 'description': description,
                                'x-native-constraints': constraints, 'x-native-write': f.get('write', {}),
                                'x-native-conditions': f.get('native_conditions', {})}
            if refs:
                properties[name]['x-reference'] = refs
        definitions[key] = object_schema(properties)
        definitions[key]['description'] = (
            'Document fields. Omitted fields are preserved on update. Supplied child arrays replace the collection. '
            'x-native-constraints.required describes the final document: defaults, fetches and native hooks may '
            'supply omitted fields. The server checks the resulting document; omission is not the same as null.')
        return {'$ref': '#/$defs/' + key}
    root_schema = document(binding, root=True)
    return {**root_schema, '$defs': definitions}


def operation(context, binding, name, contract):
    doctype = binding['native_resource']
    fields = context.contracts[binding['id']]['fields']
    properties, required = {}, []
    if name != 'create':
        properties['name'] = {'type': 'string', 'minLength': 1, 'description': 'Native document name, not a person display name.'}
        required.append('name')
        if name == 'read' and contract.get('input', {}).get('read_locator') == 'singleton':
            properties['name'] = {'const': doctype, 'description': 'Singleton document identity; omit to read the site settings document.'}
            required.remove('name')
    else:
        properties['name'] = {'type': 'string', 'minLength': 1,
                              'description': 'Explicit native document name for resources with caller-supplied naming. Otherwise omit and let the app name it.'}
    if name in {'update', 'submit', 'cancel', 'delete'}:
        properties['modified'] = {'type': 'string', 'minLength': 1, 'description': 'modified timestamp from your last read; stale versions are rejected.'}
        required.append('modified')
    if name in {'create', 'update'}:
        properties['values'] = values_schema(context, binding)
        required.append('values')
    if name not in {'read', 'create', 'update', 'submit', 'cancel', 'delete'}:
        raise ToolError('unsupported_document_action', 'No native document adapter for this action.')
    schema = merge_schema(properties, required)
    if name == 'read':
        schema['properties']['fields'] = {'type': 'array', 'minItems': 1, 'maxItems': 100,
            'items': {'enum': sorted(fields)},
            'description': 'Optional top-level fields. Child arrays remain intact; name, modified and docstatus are retained when present.'}
    def execute(connection, args):
        client = CatalogFrappeClient(connection.transport)
        if name == 'read':
            document = client.read(doctype, args.get('name', doctype))
            if 'fields' in args:
                selected = set(args['fields']) | {'name', 'modified', 'docstatus'}
                document = {k: v for k, v in document.items() if k in selected}
            return NativeResult(document)
        if name == 'create':
            return NativeResult(client.create(doctype, args['values'], name=args.get('name')))
        before = client.read(doctype, args['name'])
        if before.get('modified') != args['modified']:
            raise ToolError('stale_version', 'Document changed since your last read. Read it again before editing.')
        if name == 'update':
            if before.get('docstatus') == 2:
                raise ToolError('cancelled_document', 'A cancelled document cannot be edited through save.')
            if before.get('docstatus') == 1 and any(not fields[k].get('write', {}).get('allow_on_submit') for k in args['values']):
                raise ToolError('submitted_field_read_only', 'The supplied field cannot be changed after submission.')
            return NativeResult(client.save(before, args['values']))
        if name in {'submit', 'cancel'}:
            expected = 0 if name == 'submit' else 1
            if before.get('docstatus') != expected:
                raise ToolError('invalid_document_state', f'{name} requires docstatus={expected}.')
            return NativeResult(getattr(client, name)(before))
        return exchange(connection, 'POST', 'api/method/frappe.client.delete', {'doctype': doctype, 'name': args['name']})
    notes = ['Use the native document name from read/list results, not a display label.']
    if name == 'create':
        notes.append('Creates a draft. Defaults and document validation run on the server. Submittable documents require a separate submit action.')
    if name == 'update':
        notes.extend(['Unspecified fields are preserved. A supplied child array replaces that collection; retain rows you want to keep.',
                      'Use modified from your last read. Submitted fields may be immutable; cancelled documents cannot be saved.'])
    if name in {'submit', 'cancel'}:
        notes.append('Submit changes a draft (docstatus=0) to a submitted document (docstatus=1).'
                     if name == 'submit' else
                     'Cancel changes a submitted document (docstatus=1) to a cancelled document (docstatus=2).')
    if name == 'delete':
        notes.append('Deletes the document subject to native permission and link checks. '
                     'Deletion and cancellation are separate operations; use cancel for a submitted document before deletion when required by its lifecycle.')
    if name in {'cancel', 'delete'}:
        notes.append('Version is checked by pre-read; this is not atomic against external native writers.')
    return schema, execute, name == 'read', tuple(notes)


def listing(context, binding):
    doctype = binding['native_resource']
    # Child tables live in separate rows; they are returned by document reads,
    # not selectable as columns of the parent's list SQL query.
    columns = sorted(name for name, field in context.contracts[binding['id']]['fields'].items()
                     if field.get('reference', {}).get('kind') != 'owned_component'
                     and field.get('native_type') not in {'Table', 'Table MultiSelect'})
    schema = object_schema({'filters': {'type': ['object', 'array'],
        'description': 'Native filters: {"name":"ID"} or [["docstatus","=",1], ["name","in",["ID1","ID2"]]].'},
        'fields': {'type': 'array', 'items': {'enum': columns}, 'minItems': 1, 'maxItems': 100},
        'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}})
    def execute(connection, args):
        offset, limit = args.get('offset', 0), args.get('limit', 20)
        path = query_path('api/resource/' + quote(doctype, safe=''), {
            'filters': args.get('filters', {}), 'fields': args.get('fields', ['name']),
            'limit_start': offset, 'limit_page_length': limit, 'order_by': 'name asc'})
        result = exchange(connection, 'GET', path)
        if not isinstance(result.data, dict) or not isinstance(result.data.get('data'), list):
            raise ValueError('Invalid Frappe list response')
        rows = result.data['data']
        return NativeResult(deepcopy(rows), pagination={'offset': offset, 'limit': limit,
            'next_offset': offset + len(rows) if len(rows) == limit else None,
            'consistency': 'Live native pagination; concurrent changes can move records.'})
    return schema, execute, True, (
        'Native permission-filtered resource listing. Filters use Frappe syntax; fields are exact catalog names.',
        'Child tables are available through the document read operation, not as list columns.',
    )
