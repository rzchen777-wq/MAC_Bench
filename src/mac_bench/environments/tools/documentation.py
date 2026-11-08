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
    'duplicate_allocation_target': 'Combine additional amounts into one entry per invoice or bill; no native request was sent.',
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
            'result_navigation': 'read_result retrieves saved data without another app call. Use result_ref, or the original request_id if the reply was lost; path="" includes status and error.',
            'retry': 'Use a new request_id for a fresh read or different operation. Reuse it only to recover the same receipt. Never automatically retry unknown writes.'}
    example = call_example(tool)
    readback = tool.native_contract.get('output', {}).get('document_read')
    if readback:
        card['readback'] = deepcopy(readback)
    overview = tool.native_contract.get('output', {}).get('record_overview')
    if overview:
        from .presentation import overview_example
        card['response_example'] = {
            'data': overview_example(overview),
            'data_view': 'document_overview', 'data_complete': False,
        }
        card['response_example_note'] = 'Fictional document illustrating the returned data fields.'
        card['output_navigation'] = {'section': 'output', 'path': '/record_overview',
                                     'instruction': 'Field types, meanings and optionality for data.'}
    if example and len(encoded(example).encode()) <= 2500:
        card['call_example'] = example
        card['example_note'] = 'Schema-checked fictional template, not a native execution or task solution. Replace sample values with actual IDs and business values.'
    if len(encoded(card).encode()) > 14000:
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
    if section == 'input' and not path and not cursor:
        return {'revision': revision, 'tool_name': tool.name, 'section': section, **request_card(tool)}
    documents = {'input': tool.input_schema, 'fields': tool.native_contract.get('fields', {}),
                 'raw_input': tool.input_schema,
                 'output': tool.native_contract.get('output', {}),
                 'lifecycle': {'notes': list(tool.notes), 'native': {
                     k: v for k, v in tool.native_contract.get('operation_contract', {}).items()
                     if k in ('description', 'required_fields', 'permissions')}},
                 'errors': {**ERRORS, **({'no_write_errors': tool.native_contract['no_write_errors']}
                     if tool.native_contract.get('no_write_errors') else {})},
                 'examples': tool.native_contract.get('examples', [])}
    if section not in documents:
        raise ToolError('invalid_section', 'Choose usage, input, raw_input, fields, output, lifecycle, errors or examples.')
    return {'revision': revision, 'tool_name': tool.name, 'section': section,
            **page(documents[section], identity=[revision, tool.name, section], path=path, cursor=cursor, limit=limit, compact=True)}
