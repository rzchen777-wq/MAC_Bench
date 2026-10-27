"""Deterministic relational projection of native records, without business inference."""
import json
import re
from copy import deepcopy
from hashlib import sha256

from mac_bench.environments.verification.errors import VerificationError


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def identifier(parts):
    raw = '__'.join(parts)
    readable = re.sub(r'[^a-zA-Z0-9_]', '_', raw)[:90] or 'value'
    if readable != raw or readable.startswith('_'):
        readable = 'f_' + readable + '_' + sha256(raw.encode()).hexdigest()[:8]
    return readable


def tokens(pointer):
    if not pointer:
        return []
    if not pointer.startswith('/'):
        raise VerificationError('Invalid source field pointer')
    return [x.replace('~1', '/').replace('~0', '~') for x in pointer[1:].split('/')]


def field_paths(value, path=''):
    result = {path}
    if isinstance(value, dict):
        for name, child in value.items():
            result |= field_paths(child, path + '/' + name.replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list):
        for child in value:
            result |= field_paths(child, path + '/*')
    return result


def source_cards(initial, descriptions, *, native_fields=None, identity_paths=None):
    cards = {}
    for name, rows in initial.items():
        if not isinstance(rows, list):
            raise VerificationError('Collections must be arrays')
        fields = set((native_fields or {}).get(name, []))
        for row in rows:
            fields |= field_paths(row)
        cards[name] = {'description': descriptions[name], 'fields': sorted(fields),
                       'initial_count': len(rows), 'examples': deepcopy(rows[:2]),
                       'identity_paths': list((identity_paths or {}).get(name, []))}
    return cards


def typed_sources(sources, initial, identities):
    """Partition fixed reference inventories using materializer identities only.

    Discovery collections stay unpartitioned: newly created records cannot be
    classified by an initial-only object index. No native field names are guessed.
    """
    result = deepcopy(sources)
    for collection, card in result.items():
        rows = initial[collection]
        if card.get('requires_discovery', True) or not rows:
            continue
        if not all(r.get('_object_key') in identities for r in rows):
            continue
        groups = {}
        for row in rows:
            entity = identities[row['_object_key']]['entity']
            group = groups.setdefault(entity, {'keys': [], 'fields': set(), 'identity_fields': set()})
            group['keys'].append(row['_object_key'])
            group['fields'].update(field_paths(row))
            group['identity_fields'].update(identities[row['_object_key']]['identity'])
        card['entity_partitions'] = [{
            'entity': entity, 'object_keys': sorted(g['keys']),
            'fields': sorted(g['fields']), 'identity_fields': sorted(g['identity_fields'])}
            for entity, g in sorted(groups.items())]
    return result


def make_layout(sources):
    tables = []
    for collection, card in sorted(sources.items()):
        partitions = card.get('entity_partitions')
        if not partitions:
            tables.extend(_collection_layout(collection, card))
            continue
        for partition in partitions:
            name = identifier([collection, partition['entity']])
            for table in _collection_layout(name, partition):
                table.update(collection=collection, entity=partition['entity'],
                             object_keys=partition['object_keys'],
                             identity_fields=partition['identity_fields'])
                tables.append(table)
    names = [t['name'].casefold() for t in tables]
    if len(names) != len(set(names)):
        raise VerificationError('Ambiguous projected table names')
    return tables


def _collection_layout(collection, card):
    tables = []
    paths = [tokens(p) for p in card['fields']]
    array_paths = {tuple(p[:i+1]) for p in paths for i, token in enumerate(p) if token == '*'}
    for array_path in sorted({(), *array_paths}, key=lambda p: (len(p), p)):
        columns = []
        for path in paths:
            if path[:len(array_path)] != list(array_path):
                continue
            relative = path[len(array_path):]
            if not relative or '*' in relative:
                continue
            # Nested objects flatten into leaves; raw JSON remains available on every row.
            if any(other[:len(path)] == path and len(other) > len(path) for other in paths):
                continue
            columns.append({'name': identifier(relative), 'path': relative})
        names = [c['name'].casefold() for c in columns]
        if len(names) != len(set(names)):
            raise VerificationError('Ambiguous projected column names: ' + collection)
        name = identifier([collection, *[p for p in array_path if p != '*']])
        parents = [p for p in array_paths if len(p) < len(array_path) and array_path[:len(p)] == p]
        parent_path = max(parents, key=len) if parents else ()
        tables.append({'name': name, 'collection': collection, 'array_path': list(array_path),
                       'parent_path': list(parent_path), 'columns': sorted(columns, key=lambda c: c['name'])})
    names = [t['name'].casefold() for t in tables]
    if len(names) != len(set(names)):
        raise VerificationError('Ambiguous projected table names')
    return tables


def value_at(value, path):
    for token in path:
        if not isinstance(value, dict) or token not in value:
            return None
        value = value[token]
    return value


def sql_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if value is not None and not isinstance(value, (str, bool, int, float)):
        raise VerificationError('Non-JSON native value')
    # Also rejects non-finite floats before SQLite silently coerces them.
    json.dumps(value, allow_nan=False)
    return value


def project(snapshot, layout, *, max_rows=100000):
    expected = {t['collection'] for t in layout}
    if set(snapshot) != expected:
        raise VerificationError('Snapshot collection coverage mismatch')
    result = {}
    cached = {}
    total = 0
    for table in layout:
        collection = table['collection']
        entity = table.get('entity')
        path = tuple(table['array_path'])
        if not path:
            raw = snapshot[collection]
            if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
                raise VerificationError('Root collections must contain native objects')
            if entity:
                known = {key for t in layout if t['collection'] == collection for key in t.get('object_keys', [])}
                if any(r.get('_object_key') not in known for r in raw):
                    raise VerificationError('Unknown fixed-reference object key: ' + collection)
                raw = [r for r in raw if r['_object_key'] in table['object_keys']]
            occurrences = [(str(i), None, i, row) for i, row in enumerate(raw)]
        else:
            parent_path = tuple(table['parent_path'])
            relative = path[len(parent_path):-1]
            occurrences = []
            for parent_id, _, _, parent in cached[(collection, entity, parent_path)]:
                values = value_at(parent, relative)
                if values is None:
                    continue
                if not isinstance(values, list):
                    raise VerificationError('Native child collection is not an array: ' + table['name'])
                for i, row in enumerate(values):
                    occurrences.append((parent_id + '/' + table['name'] + '/' + str(i), parent_id, i, row))
        total += len(occurrences)
        if total > max_rows:
            raise VerificationError('Projected snapshot row limit')
        cached[(collection, entity, path)] = occurrences
        result[table['name']] = [
            [row_id, parent, ordinal, json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True),
             sql_value(row) if not isinstance(row, (dict, list)) else None,
             *[sql_value(value_at(row, c['path'])) for c in table['columns']]]
            for row_id, parent, ordinal, row in occurrences]
    return result


SYSTEM_COLUMNS = ['_row_id', '_parent_id', '_ordinal', '_json', '_value']


def describe(layout, snapshot, sources, *, examples=2):
    projected = project(snapshot, layout)
    return [{**table, 'description': sources[table['collection']]['description'],
             'sql_columns': SYSTEM_COLUMNS + [c['name'] for c in table['columns']],
             'initial_count': len(projected[table['name']]),
             'examples': [dict(zip(SYSTEM_COLUMNS + [c['name'] for c in table['columns']], row))
                          for row in projected[table['name']][:examples]]}
            for table in layout]


def identity_parameters(identities):
    result = {}
    for key, obj in identities.items():
        for field, value in obj['identity'].items():
            name = 'ref__' + identifier([key, field])
            if name in result:
                raise VerificationError('Ambiguous native reference parameter')
            if value is None or isinstance(value, (dict, list, bool)):
                raise VerificationError('Native reference must have a scalar identity')
            result[name] = sql_value(value)
    return result
