"""Keep source context attached when public evidence is projected or paginated."""
from collections import defaultdict

from .contracts import encoded
from .navigation import brief, resolve


def display(value, path, *, inline_bytes):
    detail = brief(value, path, inline_bytes=inline_bytes)
    return detail['value'] if detail['complete'] else detail


class EvidenceContext:
    """Page-local views of deterministic section links; no inferred row values."""

    def __init__(self, document):
        self.document = document
        self.sections = document['sections']
        self.fields = defaultdict(list)
        self.cache = {}
        for index, field in enumerate(document['fields']):
            self.fields[field['section_id']].append((index, field))

    def __call__(self, value, path):
        # Projection can exclude provenance. Use the underlying public record,
        # not the projected model-facing values, to determine its context.
        while True:
            if path.startswith('/sections/') and path.count('/') == 2:
                identity = path.rsplit('/', 1)[1]
                if identity in self.sections:
                    return identity, self.describe(identity)
            if isinstance(value, dict):
                identity = value.get('_section_id', value.get('section_id'))
                if identity in self.sections:
                    return identity, self.describe(identity)
            if not path:
                return None
            path = path.rsplit('/', 1)[0]
            value = resolve(self.document, path)

    def describe(self, identity):
        if identity in self.cache:
            return self.cache[identity]
        section = self.sections[identity]
        path = '/sections/' + identity
        result = {'title': display(section['title'], path + '/title', inline_bytes=250),
                  'section_path': path, 'source': section['source'],
                  'fields': [], 'fields_complete': True,
                  'fields_query': {'path': '/fields', 'where': [
                      {'path': '/section_id', 'op': 'eq', 'value': identity}]}}
        if section['parent_id']:
            parent = section['parent_id']
            result['parent'] = {'section_path': '/sections/' + parent,
                                'title': display(self.sections[parent]['title'],
                                    '/sections/' + parent + '/title', inline_bytes=250),
                                'labelled_fact_count': len(self.fields[parent])}
        for index, field in self.fields[identity]:
            detail = {'path': '/fields/' + str(index),
                      'field': display(field['field'], f'/fields/{index}/field', inline_bytes=160),
                      'value': display(field['value'], f'/fields/{index}/value', inline_bytes=300)}
            candidate = {**result, 'fields': [*result['fields'], detail]}
            if len(encoded(candidate).encode()) > 2000:
                result['fields_complete'] = False
                break
            result = candidate
        self.cache[identity] = result
        return result
