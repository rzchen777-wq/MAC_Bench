"""Lossless, bounded navigation of schemas and saved native observations."""

import base64
import json
from copy import deepcopy
from hashlib import sha256

from mac_bench.environments.tools.contracts import ToolError, encoded

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


def brief(value, path, *, inline_bytes=600):
    """Small structural descriptor; exact values remain navigable at path."""
    if len(encoded(value).encode()) <= inline_bytes:
        return {'path': path, 'value': deepcopy(value), 'complete': True}
    if isinstance(value, (dict, list)):
        result = {'path': path, 'kind': 'object' if isinstance(value, dict) else 'array',
                  'size': len(value), 'complete': False}
        if isinstance(value, dict):
            for key in ('type', '$ref', 'description'):
                if key in value and len(encoded(value[key]).encode()) <= 200:
                    result[key] = deepcopy(value[key])
            if inline_bytes >= 2000:
                # A long record still exposes its identity and short fields on
                # the first page. This preview never replaces the saved value.
                preview = {}
                for key, item in value.items():
                    if not isinstance(item, (dict, list)) and len(encoded(item).encode()) <= 300:
                        candidate = {**preview, key: item}
                        if len(encoded(candidate).encode()) <= 1200:
                            preview = candidate
                if preview:
                    result['preview'] = preview
        return result
    return {'path': path, 'kind': 'string' if isinstance(value, str) else 'scalar',
            'characters': len(str(value)), 'complete': False}


def page(document, *, identity, path='', cursor=None, limit=20, compact=False, context_provider=None):
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
            raise ToolError('invalid_cursor', 'Reuse this cursor only with the same tool, document, section, path and limit. Read and query cursors are not interchangeable. To change options, restart without cursor.') from None
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
            entry = {'key': key, **brief(node[key], pointer(path, key), inline_bytes=2000 if compact else 600)}
            candidate = {**base, 'items': [*base['items'], entry], 'next_cursor': continuation(end + 1)}
            if context_provider and (context := context_provider(node[key], pointer(path, key))):
                context_id, detail = context
                entry['context'] = context_id
                candidate['contexts'] = {**base.get('contexts', {}), context_id: detail}
            if len(encoded(candidate).encode()) > PAGE_BYTES:
                if not base['items']:
                    raise ToolError('key_too_large', 'This object key exceeds the navigation budget.')
                break
            base['items'].append(entry)
            if 'contexts' in candidate:
                base['contexts'] = candidate['contexts']
            end += 1
        base['next_cursor'] = continuation(end) if end < len(keys) else None
    elif isinstance(node, str):
        if not 0 <= start <= len(node):
            raise ToolError('invalid_cursor', 'Cursor offset is outside this string.')
        end = min(len(node), start + 900)
        if compact:
            low, high = start, min(len(node), start + PAGE_BYTES)
            while low < high:
                middle = (low + high + 1) // 2
                candidate = {**base, 'kind': 'string', 'offset': start, 'characters': len(node),
                             'value': node[start:middle],
                             'next_cursor': continuation(middle) if middle < len(node) else None}
                if len(encoded(candidate).encode()) <= PAGE_BYTES - 800:
                    low = middle
                else:
                    high = middle - 1
            end = low
            if end == start and start < len(node):
                raise ToolError('page_metadata_too_large', 'Choose a shorter exact path.')
        base.update(kind='string', offset=start, characters=len(node), value=node[start:end],
                    next_cursor=continuation(end) if end < len(node) else None)
    else:
        if start:
            raise ToolError('invalid_cursor', 'Scalar values have no continuation.')
        base.update(kind='scalar', value=node)
    if not isinstance(node, (dict, list)) and context_provider and (context := context_provider(node, path)):
        context_id, detail = context
        # A full string page can consume the navigation budget already. Keep a
        # source-section link in that case; the complete context remains public.
        if len(encoded({**base, 'context': context_id, 'contexts': {context_id: detail}}).encode()) > PAGE_BYTES:
            detail = {'section_path': detail['section_path']}
        base.update(context=context_id, contexts={context_id: detail})
    return base
