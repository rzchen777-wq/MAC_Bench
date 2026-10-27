"""Revision-pinned, byte-bounded discovery shared by catalog interfaces."""
import base64
import json
from copy import deepcopy

from mac_bench.environments.catalog.entities.errors import CatalogError
from mac_bench.environments.catalog.entities.storage import digest, encoded


def paginate(items, *, revision, query, cursor=None, limit=20, max_bytes=16000):
    if type(limit) is not int or not 1 <= limit <= 100 or type(max_bytes) is not int or not 1024 <= max_bytes <= 1000000:
        raise CatalogError('pagination_limits_invalid')
    fingerprint = digest({'revision': revision, 'query': query})
    start = 0
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        except (ValueError, TypeError, UnicodeError):
            raise CatalogError('cursor_invalid') from None
        if (not isinstance(token, dict) or token.get('query') != fingerprint
                or type(token.get('offset')) is not int or not 0 <= token['offset'] <= len(items)):
            raise CatalogError('cursor_query_or_revision_mismatch')
        start = token['offset']
    output = []
    def envelope(end):
        next_cursor = None if end == len(items) else base64.urlsafe_b64encode(
            encoded({'query': fingerprint, 'offset': end})).decode()
        return {'revision': revision, 'items': output, 'total': len(items), 'next_cursor': next_cursor}
    end = start
    while end < len(items) and len(output) < limit:
        output.append(items[end])
        if len(encoded(envelope(end + 1))) > max_bytes:
            output.pop()
            if not output:
                raise CatalogError('entry_exceeds_response_budget', index=end,
                                   required_bytes=len(encoded(items[end])), max_bytes=max_bytes)
            break
        end += 1
    return deepcopy(envelope(end))

