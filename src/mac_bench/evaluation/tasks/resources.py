"""Read-only public world documents; large material stays outside initial prompts."""
import json
import re

from mac_bench.environments.tools.contracts import ToolError, validate
from mac_bench.environments.tools.navigation import page
from mac_bench.environments.tools.query import (QUERY_PROPERTIES, QUERY_DESCRIPTION,
    query_document, cursor_start)
from .protocol import ToolDefinition

LIST_RESOURCES = ToolDefinition('list_resources',
    'List public task documents and supporting files, with their sizes. No file contents.',
    {'type': 'object', 'properties': {}, 'additionalProperties': False})
READ_RESOURCE = ToolDefinition('read_resource',
    'Read a public file returned by list_resources. Text uses offset (characters) and length '
    '(default 8000, maximum 12000); continue from next_offset. JSON uses path (JSON Pointer), '
    'cursor and limit; follow returned paths/next_cursor for exact values. Files are read-only '
    'task materials, not current application state.',
    {'type': 'object', 'properties': {
        'resource': {'type': 'string', 'minLength': 1},
        'offset': {'type': 'integer', 'minimum': 0},
        'length': {'type': 'integer', 'minimum': 1, 'maximum': 12000},
        'path': {'type': 'string'}, 'cursor': {'type': 'string'},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
     'required': ['resource'], 'additionalProperties': False})
QUERY_RESOURCE = ToolDefinition('query_resource',
    'Find/project/aggregate records in a public JSON file without reading it all. ' + QUERY_DESCRIPTION,
    {'type': 'object', 'properties': {'resource': {'type': 'string', 'minLength': 1}, **QUERY_PROPERTIES},
     'required': ['resource'], 'additionalProperties': False})
SEARCH_RESOURCE = ToolDefinition('search_resource',
    'Find literal text in a public text file, case-insensitively. Returns bounded excerpts with exact '
    'offsets for read_resource. query is literal text, not a regex. Repeat the same query/context/limit '
    'with next_cursor to continue. For JSON records use query_resource instead.',
    {'type': 'object', 'properties': {'resource': {'type': 'string', 'minLength': 1},
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 200},
        'context': {'type': 'integer', 'minimum': 0, 'maximum': 1000},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'cursor': {'type': 'string'}},
     'required': ['resource', 'query'], 'additionalProperties': False})


class PublicResources:
    def __init__(self, bundle):
        self.bundle = bundle
        self.names = tuple(bundle.manifest['public_files'])

    def list(self):
        descriptions = {'public/records.json': 'Recommended initial record directory: search business labels/IDs; native locators are directly readable. Not live state.',
                        'public/facts.json': 'Source-linked initial evidence tables and labelled facts. Query /tables or /fields to find data; not live application state or task answers.',
                        'public/registers.json': 'Public register titles, field schemas and source passages. Use matching registers.* tools; formatting checks do not grade business decisions.',
                        'public/data_guide.md': 'SQL data-workspace examples, result references, register schemas and application-state boundaries.',
                        'public/identities.json': 'Original internal-key identity map, retained for compatibility.',
                        'public/environment.md': 'Application objects, identifier namespaces and short resource-query examples.',
                        'public/recording.md': 'Required business record formats and identifier meanings.',
                        'public/record_forms.json': 'Public typed administrative forms: field schemas and storage mappings, without task solutions.',
                        'public/task.txt': 'Task objective, requirements and supporting-material locations.',
                        'public/tool_tutorial.md': 'General worked tool-use example, independent of this task.',
                        'public/business_policy.md': 'Business rules, constraints and supplied authorizations for this task.',
                        'public/tool_operations.md': 'Shared structured-record and calendar operations, identifiers and operation effects.',
                        'public/materials.json': 'Searchable business-evidence index with original note locators and readable-excerpt locations. Not live state.',
                        'public/business_materials.md': 'Readable initial business evidence with original-note provenance. Not a live application view.'}
        return {'resources': [{'resource': name, 'bytes': (self.bundle.root / name).stat().st_size,
                               'format': 'json' if name.endswith('.json') else 'text',
                               'description': descriptions.get(name, 'Supporting public task material.')}
                              for name in self.names]}

    def instruction(self):
        extra = (' Read public/environment.md for record-directory and tool conventions.'
                 if 'public/environment.md' in self.names else '')
        return ('Complete the task in public/task.txt. '
                'Start by reading it with read_resource(resource="public/task.txt"). '
                'The task requirements and supporting files remain available throughout the episode.' + extra)

    def _text(self, resource):
        # Exact manifest membership, never an arbitrary host path or directory.
        if resource not in self.names:
            raise ToolError('unknown_resource', 'Use an exact resource name from list_resources.')
        return (self.bundle.root / resource).read_text()

    def read(self, resource, *, offset=0, length=8000, path='', cursor=None, limit=20):
        text = self._text(resource)
        if resource.endswith('.json'):
            if offset or length != 8000:
                raise ToolError('invalid_arguments', 'JSON files use path/cursor/limit, not offset/length.')
            return {'resource': resource, **page(json.loads(text),
                    identity=[self.bundle.manifest['revision'], resource], path=path, cursor=cursor, limit=limit, compact=True)}
        if path or cursor or limit != 20:
            raise ToolError('invalid_arguments', 'Text files use offset/length, not path/cursor/limit.')
        if not 0 <= offset <= len(text):
            raise ToolError('invalid_offset', 'offset is outside this file.')
        def result(end):
            return {'resource': resource, 'offset': offset, 'characters': len(text),
                    'text': text[offset:end], 'next_offset': end if end < len(text) else None}
        # Bound the serialized UTF-8 response too, including JSON escaping.
        low, high = offset, min(len(text), offset + length)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(result(middle), ensure_ascii=False).encode()) <= 12000:
                low = middle
            else:
                high = middle - 1
        return result(low)

    def query(self, resource, **options):
        text = self._text(resource)
        if not resource.endswith('.json'):
            raise ToolError('invalid_resource', 'Use search_resource for text files.')
        if resource == 'public/records.json' and not options.get('path') and 'fields' not in options:
            options['fields'] = {k: '/' + k for k in ('app', 'entity', 'label', 'business_id', 'business_ids', 'identity', 'source_keys')}
        return {'resource': resource, **query_document(json.loads(text),
                identity=[self.bundle.manifest['revision'], resource], **options)}

    def search(self, resource, query, *, context=200, limit=20, cursor=None):
        validate(SEARCH_RESOURCE.input_schema, {k: v for k, v in dict(resource=resource,
            query=query, context=context, limit=limit, cursor=cursor).items() if v is not None})
        text = self._text(resource)
        if resource.endswith('.json'):
            raise ToolError('invalid_resource', 'Use query_resource for JSON records.')
        start, continuation = cursor_start(cursor, [self.bundle.manifest['revision'], resource, query, context, limit])
        if start > len(text):
            raise ToolError('invalid_cursor', 'Cursor is outside this file.')
        response = {'resource': resource, 'matches': [], 'next_cursor': None}
        for match in re.finditer(re.escape(query), text[start:], re.IGNORECASE):
            begin, end = start + match.start(), start + match.end()
            left, right = max(0, begin - context), min(len(text), end + context)
            hit = {'match_offset': begin, 'offset': left, 'length': right - left,
                   'line': text.count('\n', 0, begin) + 1, 'text': text[left:right]}
            candidate = {**response, 'matches': [*response['matches'], hit], 'next_cursor': continuation(end)}
            if len(response['matches']) == limit or len(json.dumps(candidate, ensure_ascii=False).encode()) > 12000:
                if not response['matches']:
                    raise ToolError('search_excerpt_too_large', 'Request less surrounding context.')
                response['next_cursor'] = continuation(begin)
                break
            response['matches'].append(hit)
        return response
