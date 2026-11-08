"""Deterministic, bounded queries over public JSON and saved observations."""
import base64
import json
import math
from hashlib import sha256

from .contracts import ToolError, encoded, object_schema, validate
from .navigation import brief, pointer, resolve, PAGE_BYTES

QUERY_PROPERTIES = {
    'path': {'type': 'string'},
    'keys': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 1000, 'uniqueItems': True},
    'query': {'type': 'string', 'maxLength': 200},
    'fields': {'type': 'object', 'additionalProperties': {'type': 'string'}, 'maxProperties': 30},
    'where': {'type': 'array', 'maxItems': 20, 'items': object_schema({
        'path': {'type': 'string'}, 'op': {'enum': ['eq', 'in', 'contains', 'gt', 'gte', 'lt', 'lte', 'exists']},
        'value': {}}, ['path', 'op', 'value'])},
    'sort': object_schema({'path': {'type': 'string'}, 'descending': {'type': 'boolean'}}, ['path']),
    'aggregates': {'type': 'array', 'maxItems': 20, 'items': object_schema({
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 80},
        'op': {'enum': ['count', 'sum', 'min', 'max', 'mean']}, 'path': {'type': 'string'}}, ['name', 'op'])},
    'cursor': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
}
QUERY_DESCRIPTION = (
    'Query a JSON object/array at path (JSON Pointer). keys selects exact object keys or array indices; '
    'query matches a literal substring in keys/values, case-insensitively. where combines predicates with AND; '
    'predicate paths and fields are relative to each row. fields maps output labels to JSON Pointers '
    '(e.g. {"id":"/identity/id"}); omit fields for small row previews and recovery paths. '
    'sort orders by a scalar field. aggregates [{name,op,path}] compute over ALL matched rows before pagination; '
    'count needs no path, other operations require a numeric field. Missing/non-numeric values are reported. '
    'Results include total_matched, exact row paths and next_cursor; repeat identical arguments with cursor '
    'to continue in the same tool. Read and query cursors are not interchangeable; changing any '
    'options requires restarting without cursor. Large field values return recovery paths instead of full contents.')


def cursor_start(cursor, identity):
    fingerprint = sha256(encoded(identity).encode()).hexdigest()
    start = 0
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor))
            if token['document'] != fingerprint or type(token['offset']) is not int or token['offset'] < 0:
                raise ValueError()
            start = token['offset']
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ToolError('invalid_cursor', 'Repeat the same query, tool, fields, path, filters, sort and page size with its returned cursor. Read and query cursors are not interchangeable. To change options, restart without cursor.') from None
    def continuation(offset):
        return base64.urlsafe_b64encode(encoded({'document': fingerprint, 'offset': offset}).encode()).decode()
    return start, continuation


def _value(row, path):
    try:
        return True, resolve(row, path)
    except ToolError as exc:
        if exc.code != 'path_not_found':
            raise
        return False, None


def _matches(row, condition):
    found, value = _value(row, condition['path'])
    op, expected = condition['op'], condition['value']
    if op == 'exists':
        if type(expected) is not bool:
            raise ToolError('invalid_query', 'exists requires a boolean value.')
        return found == expected
    if op == 'in' and not isinstance(expected, list):
        raise ToolError('invalid_query', 'in requires an array value.')
    if not found:
        return False
    if op == 'eq':
        return value == expected and ((type(value) is bool) == (type(expected) is bool))
    if op == 'in':
        return any(value == v and ((type(value) is bool) == (type(v) is bool)) for v in expected)
    if op == 'contains':
        if isinstance(value, str) and isinstance(expected, str):
            return expected.casefold() in value.casefold()
        return isinstance(value, list) and any(v == expected and
            ((type(v) is bool) == (type(expected) is bool)) for v in value)
    compatible = (isinstance(value, str) and isinstance(expected, str) or
                  type(value) in (int, float) and type(expected) in (int, float))
    if not compatible:
        return False
    return {'gt': lambda: value > expected, 'gte': lambda: value >= expected,
            'lt': lambda: value < expected, 'lte': lambda: value <= expected}[op]()


def query_document(document, *, identity, path='', keys=None, query='', fields=None,
                   where=None, sort=None, aggregates=None, cursor=None, limit=50,
                   context_provider=None):
    validate(object_schema(QUERY_PROPERTIES), {k: v for k, v in dict(path=path, keys=keys, query=query,
        fields=fields, where=where, sort=sort, aggregates=aggregates, cursor=cursor, limit=limit).items()
        if v is not None})
    for location in [path, *(fields or {}).values(), *[c['path'] for c in where or []],
                     *([sort['path']] if sort else []), *[a['path'] for a in aggregates or [] if 'path' in a]]:
        if location and not location.removeprefix('#').startswith('/'):
            raise ToolError('invalid_path', 'Field/collection paths are JSON Pointers, not dotted names.')
    for condition in where or []:
        if condition['op'] == 'exists' and type(condition['value']) is not bool:
            raise ToolError('invalid_query', 'exists requires a boolean value.')
        if condition['op'] == 'in' and not isinstance(condition['value'], list):
            raise ToolError('invalid_query', 'in requires an array value.')
    path = path.removeprefix('#')
    node = resolve(document, path)
    if not isinstance(node, (dict, list)):
        raise ToolError('invalid_query', 'Choose an object or array path; read a scalar directly.')
    options = [path, keys, query, fields, where, sort, aggregates, limit]
    start, continuation = cursor_start(cursor, [identity, options])
    available = {str(k): v for k, v in (node.items() if isinstance(node, dict) else enumerate(node))}
    missing = [key for key in keys or [] if key not in available]
    selected = [(k, available[k]) for k in (keys if keys is not None else available) if k in available]
    matched = [(k, v) for k, v in selected
               if (not query or query.casefold() in (k + ' ' + encoded(v)).casefold())
               and all(_matches(v, condition) for condition in where or [])]
    if sort:
        present, absent = [], []
        for k, v in matched:
            found, value = _value(v, sort['path'])
            if not found or value is None:
                absent.append((k, v)); continue
            if type(value) not in (str, int, float, bool):
                raise ToolError('invalid_query', 'sort requires a scalar field.')
            present.append((value, k, v))
        types = {str if isinstance(v, str) else bool if type(v) is bool else float for v, _, _ in present}
        if len(types) > 1:
            raise ToolError('invalid_query', 'sort fields must have comparable types; filter mixed values first.')
        present.sort(key=lambda r: r[0], reverse=sort.get('descending', False))
        matched = [(k, v) for _, k, v in present] + absent
    summaries = {}
    for spec in aggregates or []:
        name, op = spec['name'], spec['op']
        if name in summaries:
            raise ToolError('invalid_query', 'Aggregate names must be unique.')
        if op == 'count':
            summaries[name] = {'value': len(matched)}; continue
        if 'path' not in spec:
            raise ToolError('invalid_query', 'Numeric aggregates require a path.')
        values = []; missing_count = invalid_count = 0
        for _, row in matched:
            found, value = _value(row, spec['path'])
            if not found or value is None:
                missing_count += 1
            elif type(value) not in (int, float) or type(value) is float and not math.isfinite(value):
                invalid_count += 1
            else:
                values.append(value)
        result = {'value': None, 'numeric_count': len(values), 'missing_count': missing_count,
                  'non_numeric_count': invalid_count}
        if not missing_count and not invalid_count:
            try:
                if op in ('sum', 'mean'):
                    total = sum(values) if all(type(v) is int for v in values) else math.fsum(values)
                    result['value'] = total if op == 'sum' else total / len(values) if values else None
                else:
                    result['value'] = (min if op == 'min' else max)(values) if values else None
                if type(result['value']) is float and not math.isfinite(result['value']):
                    raise OverflowError()
            except OverflowError:
                raise ToolError('aggregate_overflow', 'Numeric aggregate exceeds finite range; narrow the query.') from None
        summaries[name] = result
    if start > len(matched):
        raise ToolError('invalid_cursor', 'Cursor is outside this query.')
    response = {'path': path, 'total_matched': len(matched), 'missing_keys': missing,
                'aggregates': summaries, 'rows': [], 'next_cursor': None}
    if len(encoded(response).encode()) > PAGE_BYTES // 2:
        raise ToolError('query_metadata_too_large', 'Request fewer keys/aggregates; query metadata exceeds the page budget.')
    end = start
    while end < len(matched) and end - start < limit:
        key, value = matched[end]; row_path = pointer(path, key)
        row = {'key': key, 'path': row_path}
        if fields is None:
            row['preview'] = brief(value, row_path)
        else:
            row['values'] = {}; absent = []
            for label, field in fields.items():
                found, item = _value(value, field)
                if not found:
                    absent.append(label); continue
                location = row_path + field.removeprefix('#')
                detail = brief(item, location)
                row['values'][label] = detail['value'] if detail['complete'] else detail
            if absent:
                row['missing_fields'] = absent
        candidate = {**response, 'rows': [*response['rows'], row], 'next_cursor': continuation(end + 1)}
        if context_provider and (context := context_provider(value, row_path)):
            context_id, detail = context
            row['context'] = context_id
            candidate['contexts'] = {**response.get('contexts', {}), context_id: detail}
        if len(encoded(candidate).encode()) > PAGE_BYTES:
            if not response['rows']:
                raise ToolError('query_row_too_large', 'Select fewer fields; recover long values with their exact paths.')
            break
        response['rows'].append(row)
        if 'contexts' in candidate:
            response['contexts'] = candidate['contexts']
        end += 1
    response['next_cursor'] = continuation(end) if end < len(matched) else None
    return response
