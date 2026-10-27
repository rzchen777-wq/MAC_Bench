"""Agent usage cards, separated from complete executable native contracts."""

from copy import deepcopy

from mac_bench.construction.realize.tools.contracts import ToolError, encoded
from mac_bench.construction.realize.tools.navigation import brief, page, pointer

ERRORS = {
    'unsupported_filter_operator': 'The native field accepts only the listed operators even if introspection lists more. '
                                   'Choose an allowed operator with the intended semantics; regex and LIKE are not interchangeable.',
    'invalid_arguments': 'Correct the named fields using the input contract; no native request was sent.',
    'stale_version': 'Read the current record and use its version; do not reuse the stale write.',
    'native_call_failed': 'Inspect the error code and native state. An unknown write may already have taken effect.',
    'native_schema_mismatch': 'The application could not resolve a database field or relation; this is not evidence of an invalid record ID. '
                              'For reads, select basic record fields explicitly. For writes, inspect current state before another attempt.',
    'request_id_conflict': 'One request_id identifies one exact invocation. Use a new ID for different input or a fresh read.',
}


def usage(tool):
    properties = tool.input_schema.get('properties', {})
    card = {**tool.summary(), 'interface_version': 2,
            'input': [{'name': k, 'required': k in tool.input_schema.get('required', []),
                       **brief(v, pointer('/properties', k))} for k, v in properties.items()],
            'input_navigation': {'section': 'input', 'path': '',
                                 'instruction': 'Inspect nested input paths before filling them. Follow $ref in the same input section.'},
            'returns': tool.output_description,
            'lifecycle': list(tool.notes),
            'details': ['input', 'fields', 'output', 'lifecycle', 'errors', 'examples'],
            'result_navigation': 'Large results return a result_ref. read_result retrieves saved data without another app call.',
            'retry': 'Use a new request_id for a fresh read or different operation. Reuse it only to recover the same receipt. Never automatically retry unknown writes.'}
    examples = tool.native_contract.get('examples', [])
    if examples and len(encoded(examples[0]).encode()) <= 1500:
        card['call_example'] = {'tool_name': tool.name, 'arguments': deepcopy(examples[0]['arguments']), 'request_id': 'example-001'}
        card['example_note'] = examples[0]['meaning']
    if len(encoded(card).encode()) > 7000:
        # Explicit structural navigation, never head/tail truncation of a schema.
        card['input'] = {'section': 'input', 'path': '/properties', 'field_count': len(properties),
                         'required_path': '/required'}
        card['lifecycle'] = {'section': 'lifecycle', 'path': ''}
    return card


def describe(tool, revision, *, section='usage', path='', cursor=None, limit=20):
    if section == 'usage':
        if path or cursor:
            raise ToolError('invalid_section', 'usage is a single card. Select a details section to navigate.')
        return {'revision': revision, **usage(tool)}
    documents = {'input': tool.input_schema, 'fields': tool.native_contract.get('fields', {}),
                 'output': tool.native_contract.get('output', {}),
                 'lifecycle': {'notes': list(tool.notes), 'native': {
                     k: v for k, v in tool.native_contract.get('operation_contract', {}).items()
                     if k in ('description', 'required_fields', 'permissions')}},
                 'errors': ERRORS, 'examples': tool.native_contract.get('examples', [])}
    if section not in documents:
        raise ToolError('invalid_section', 'Choose usage, input, fields, output, lifecycle, errors or examples.')
    return {'revision': revision, 'tool_name': tool.name, 'section': section,
            **page(documents[section], identity=[revision, tool.name, section], path=path, cursor=cursor, limit=limit)}
