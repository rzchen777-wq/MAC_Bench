"""Agent usage cards, separated from complete executable native contracts."""

from copy import deepcopy

from mac_bench.environments.tools.contracts import ToolError, encoded
from mac_bench.environments.tools.navigation import brief, page, pointer
from mac_bench.environments.tools.cards import request_card, projections, call_example

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
            'request': request_card(tool),
            'input_navigation': {'section': 'input', 'path': '',
                                 'instruction': 'The request view expands references; follow explicit details links only for deferred constraints.'},
            'projection': projections(tool),
            'returns': tool.output_description,
            'lifecycle': list(tool.notes),
            'details': ['input', 'fields', 'output', 'lifecycle', 'errors', 'examples'],
            'result_navigation': 'Large results return a result_ref. read_result retrieves saved data without another app call.',
            'retry': 'Use a new request_id for a fresh read or different operation. Reuse it only to recover the same receipt. Never automatically retry unknown writes.'}
    example = call_example(tool)
    if example and len(encoded(example).encode()) <= 2500:
        card['call_example'] = example
        card['example_note'] = 'Schema-checked fictional template, not a native execution or task solution. Replace sample values with actual IDs and business values.'
    if len(encoded(card).encode()) > 14000:
        # Explicit structural navigation, never head/tail truncation of a schema.
        card['input'] = {'section': 'input', 'path': '/properties', 'field_count': len(properties),
                         'required_path': '/required'}
        card['lifecycle'] = {'section': 'lifecycle', 'path': ''}
    card['presentation_version'] = 3
    return card


def describe(tool, revision, *, section='usage', path='', cursor=None, limit=20):
    if section == 'usage':
        if path or cursor:
            raise ToolError('invalid_section', 'usage is a single card. Select a details section to navigate.')
        return {'revision': revision, **usage(tool)}
    if section == 'input' and not path and not cursor:
        return {'revision': revision, 'tool_name': tool.name, 'section': section, **request_card(tool)}
    documents = {'input': tool.input_schema, 'fields': tool.native_contract.get('fields', {}),
                 'raw_input': tool.input_schema,
                 'output': tool.native_contract.get('output', {}),
                 'lifecycle': {'notes': list(tool.notes), 'native': {
                     k: v for k, v in tool.native_contract.get('operation_contract', {}).items()
                     if k in ('description', 'required_fields', 'permissions')}},
                 'errors': ERRORS, 'examples': tool.native_contract.get('examples', [])}
    if section not in documents:
        raise ToolError('invalid_section', 'Choose usage, input, raw_input, fields, output, lifecycle, errors or examples.')
    return {'revision': revision, 'tool_name': tool.name, 'section': section,
            **page(documents[section], identity=[revision, tool.name, section], path=path, cursor=cursor, limit=limit, compact=True)}
