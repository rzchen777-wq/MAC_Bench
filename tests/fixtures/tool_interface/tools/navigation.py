"""Lossless, bounded navigation of schemas and saved native observations."""

import base64
import json
from copy import deepcopy
from hashlib import sha256

from mac_bench.construction.realize.tools.contracts import ToolError, encoded

PAGE_BYTES = 8000


def pointer(parent, key):
    return parent + '/' + str(key).replace('~', '~0').replace('/', '~1')


def resolve(document, path):
    if path.startswith('#'):
        path = path[1:]
    if path and not path.startswith('/'):
        raise ToolError('invalid_path', 'Use a JSON Pointer from the returned path or schema $ref.')
    node = document
    try:
        for segment in path.split('/')[1:] if path else ():
            key = segment.replace('~1', '/').replace('~0', '~')
            if isinstance(node, list):
                if not key.isdigit() or (len(key) > 1 and key.startswith('0')):
                    raise KeyError(key)
                node = node[int(key)]
            elif isinstance(node, dict):
                node = node[key]
            else:
                raise KeyError(key)
    except (KeyError, IndexError, ValueError):
        raise ToolError('path_not_found', 'That path does not exist. Read its parent to discover available paths.') from None
    return node


def brief(value, path):
    """Small structural descriptor; exact values remain navigable at path."""
    if len(encoded(value).encode()) <= 600:
        return {'path': path, 'value': deepcopy(value), 'complete': True}
    if isinstance(value, (dict, list)):
        result = {'path': path, 'kind': 'object' if isinstance(value, dict) else 'array',
                  'size': len(value), 'complete': False}
        if isinstance(value, dict):
            for key in ('type', '$ref', 'description'):
                if key in value and len(encoded(value[key]).encode()) <= 200:
                    result[key] = deepcopy(value[key])
        return result
    return {'path': path, 'kind': 'string' if isinstance(value, str) else 'scalar',
            'characters': len(str(value)), 'complete': False}


def page(document, *, identity, path='', cursor=None, limit=20):
    """A page is complete JSON. Large children link to exact retrievable nodes.

    String leaves use character continuation; no schema or observation text is
    discarded. Cursors bind to the document revision, path and page size.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ToolError('invalid_pagination', 'limit must be an integer from 1 to 100.')
    path = path.removeprefix('#')
    node = resolve(document, path)
    fingerprint = sha256(encoded([identity, path, limit]).encode()).hexdigest()
    start = 0
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor))
            if token['document'] != fingerprint or type(token['offset']) is not int:
                raise ValueError()
            start = token['offset']
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ToolError('invalid_cursor', 'Cursor belongs to another document, path or limit.') from None
    def continuation(offset):
        return base64.urlsafe_b64encode(encoded({'document': fingerprint, 'offset': offset}).encode()).decode()
    base = {'path': path, 'next_cursor': None}
    if isinstance(node, (dict, list)):
        keys = sorted(node) if isinstance(node, dict) else list(range(len(node)))
        if not 0 <= start <= len(keys):
            raise ToolError('invalid_cursor', 'Cursor offset is outside this value.')
        base.update(kind='object' if isinstance(node, dict) else 'array', total=len(keys), items=[])
        end = start
        while end < len(keys) and end - start < limit:
            key = keys[end]
            entry = {'key': key, **brief(node[key], pointer(path, key))}
            candidate = {**base, 'items': [*base['items'], entry], 'next_cursor': continuation(end + 1)}
            if len(encoded(candidate).encode()) > PAGE_BYTES:
                if not base['items']:
                    raise ToolError('key_too_large', 'This object key exceeds the navigation budget.')
                break
            base['items'].append(entry)
            end += 1
        base['next_cursor'] = continuation(end) if end < len(keys) else None
    elif isinstance(node, str):
        if not 0 <= start <= len(node):
            raise ToolError('invalid_cursor', 'Cursor offset is outside this string.')
        # Worst-case JSON escaping is six bytes per character.
        end = min(len(node), start + 900)
        base.update(kind='string', offset=start, characters=len(node), value=node[start:end],
                    next_cursor=continuation(end) if end < len(node) else None)
    else:
        if start:
            raise ToolError('invalid_cursor', 'Scalar values have no continuation.')
        base.update(kind='scalar', value=node)
    return base
