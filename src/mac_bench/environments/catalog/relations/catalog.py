"""Static, source-backed relations over versioned business entity contracts."""
from collections import Counter
from copy import deepcopy

from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.entities.pagination import paginate
from mac_bench.environments.catalog.entities.storage import (
    at,
    digest,
    read_json,
    write_json,
)


def pages(method, **kwargs):
    """Consume a bounded discovery API without reaching into its storage."""
    cursor = None
    while True:
        page = method(cursor=cursor, limit=100, max_bytes=1000000, **kwargs)
        yield from page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return


def observation(document, *, uri, task_id, pointer, excerpt):
    """Capture a verified Stage-I excerpt; multiple excerpts count as one task."""
    value = at(document, pointer)
    if not task_id or not excerpt or not isinstance(value, str) or excerpt not in value:
        raise CatalogError('relation_observation_invalid', uri=uri, pointer=pointer)
    return {'uri': uri, 'sha256': digest(document), 'task_id': task_id,
            'pointer': pointer, 'excerpt': excerpt}


class RelationCatalog:
    FORMAT = 'business-relation-catalog/v1'

    def __init__(self, entities, records=()):
        self.entities = entities
        self._records = {}
        for record in records:
            row = deepcopy(record)
            self._validate(row)
            if row['id'] in self._records:
                raise CatalogError('duplicate_relation', relation=row['id'])
            self._records[row['id']] = row
        self.revision = digest(self._payload())

    def _payload(self):
        return {'format': self.FORMAT, 'entity_revision': self.entities.revision,
                'relations': [self._records[k] for k in sorted(self._records)]}

    @classmethod
    def from_entities(cls, entities):
        """Keep polymorphic, external and unresolved declarations, without guessing."""
        rows = []
        for card in pages(entities.overview, role=None):
            entity = card['id']
            for variant in entities.variants(entity):
                selected = entities.resolve(entity, variant=variant['id'])
                for field, spec in selected['contract']['fields'].items():
                    ref = spec.get('reference')
                    if not ref:
                        continue
                    key = [entity, selected['variant'], field]
                    rows.append({'id': 'native:' + digest(key), 'kind': 'native',
                                 'source': entity, 'source_variant': selected['variant'],
                                 'field': field, 'targets': ref.get('targets', []),
                                 'semantics': ref['kind'], 'reference': ref,
                                 'provenance': 'schema', 'support_count': 0,
                                 'sources': [entry for binding in variant['bindings']
                                             for entry in entities.source(entity, binding=binding, field=field)],
                                 'status': ref.get('resolution', 'unresolved')})
                component = selected['contract']['identity'].get('component', {})
                for owner in component.get('owners', []):
                    # Ownership is useful even when no forward field reference exists.
                    rows.append({'id': 'owner:' + digest([entity, variant['id'], owner]),
                                 'kind': 'ownership', 'source': entity,
                                 'source_variant': variant['id'], 'targets': [owner['owner']],
                                 'semantics': 'component_owned_by', 'ownership': owner,
                                 'provenance': 'schema', 'support_count': 0,
                                 'sources': [owner['source']], 'status': 'resolved'})
        return cls(entities, rows)

    def _validate(self, row):
        if not row.get('id') or row.get('kind') not in {'native', 'ownership', 'bridge'}:
            raise CatalogError('relation_invalid')
        self.entities.entity(row['source'])
        for target in row['targets']:
            self.entities.entity(target)
        if row['kind'] != 'bridge':
            contract = self.entities.resolve(row['source'], variant=row['source_variant'])['contract']
            if row['kind'] == 'native':
                reference = contract['fields'].get(row['field'], {}).get('reference')
                if not reference or reference != row['reference'] or row['targets'] != reference.get('targets', []):
                    raise CatalogError('native_relation_mismatch', relation=row['id'])
            elif row['ownership'] not in contract['identity'].get('component', {}).get('owners', []):
                raise CatalogError('ownership_relation_mismatch', relation=row['id'])
            if row['provenance'] != 'schema' or row['support_count'] != 0:
                raise CatalogError('native_relation_provenance_invalid')
            if row['kind'] == 'native':
                if (row['semantics'] != reference['kind']
                        or row['status'] != reference.get('resolution', 'unresolved')):
                    raise CatalogError('native_relation_status_invalid')
            elif (row['targets'] != [row['ownership']['owner']]
                  or row['semantics'] != 'component_owned_by' or row['status'] != 'resolved'):
                raise CatalogError('ownership_relation_target_invalid')
            return
        if (len(row['targets']) != 1 or not row.get('semantics')
                or row['provenance'] not in {'task_observed', 'inferred'}):
            raise CatalogError('bridge_invalid', relation=row['id'])
        support = row.get('support', [])
        if row['provenance'] == 'task_observed' and not support:
            raise CatalogError('observed_bridge_requires_support')
        for item in support:
            if not all(item.get(key) for key in ('uri', 'sha256', 'task_id', 'excerpt')) or 'pointer' not in item:
                raise CatalogError('bridge_support_invalid')
        if row['support_count'] != len({item['task_id'] for item in support}):
            raise CatalogError('bridge_support_count_invalid')
        if row['provenance'] == 'inferred' and not row.get('rationale'):
            raise CatalogError('inferred_bridge_requires_rationale')
        binding = row.get('binding')
        if row['status'] != ('bound' if binding else 'semantic_only'):
            raise CatalogError('bridge_status_invalid')
        if binding:
            if binding.get('kind') != 'equality' or not binding.get('pairs') or not binding.get('evidence'):
                raise CatalogError('bridge_binding_invalid')
            if binding.get('cardinality') not in {'one_to_one', 'one_to_many', 'many_to_one', 'many_to_many'}:
                raise CatalogError('bridge_cardinality_invalid')
            for side, entity in [('source', row['source']), ('target', row['targets'][0])]:
                endpoint = binding[side]
                for pair in binding['pairs']:
                    self.entities.resolve(entity, variant=endpoint.get('variant'),
                                          binding=endpoint.get('binding'), field=pair[side])

    def with_bridge(self, *, identity, source, target, semantics, provenance,
                    support=(), rationale=None, binding=None):
        """Add one reviewed bridge. Native equality bindings require explicit fields.

        support must be captured with observation(); never infer equality from a
        semantic relationship. Other relation semantics remain discoverable even
        without an executable field join.
        """
        support = list(support)
        row = {'id': identity, 'kind': 'bridge', 'source': source, 'targets': [target],
               'semantics': semantics, 'provenance': provenance, 'support': list(support),
               'support_count': len({item['task_id'] for item in support}),
               'rationale': rationale, 'binding': binding,
               'status': 'bound' if binding else 'semantic_only'}
        return type(self)(self.entities, [*self._records.values(), row])

    def resolve(self, identity, *, revision=None):
        if revision is not None and revision != self.revision:
            raise CatalogError('relation_revision_mismatch')
        if identity not in self._records:
            raise CatalogError('relation_not_found', relation=identity)
        return deepcopy(self._records[identity])

    def overview(self, *, entity=None, direction='either', kind=None, provenance=None, **page):
        if direction not in {'outgoing', 'incoming', 'either'}:
            raise CatalogError('relation_direction_invalid')
        if entity is not None:
            self.entities.entity(entity)
        rows = []
        for row in self._records.values():
            if kind is not None and row['kind'] != kind:
                continue
            if provenance is not None and row['provenance'] != provenance:
                continue
            if entity is not None and not ((direction != 'incoming' and row['source'] == entity)
                                           or (direction != 'outgoing' and entity in row['targets'])):
                continue
            rows.append({key: deepcopy(row[key]) for key in
                         ('id', 'kind', 'source', 'source_variant', 'field', 'targets', 'semantics',
                          'provenance', 'support_count', 'status') if key in row})
        rows.sort(key=lambda row: (row['provenance'] != 'task_observed', -row['support_count'], row['id']))
        return paginate(rows, revision=self.revision,
                        query=['relations', entity, direction, kind, provenance], **page)

    def select(self, identities, *, max_bytes=1000000):
        from mac_bench.environments.catalog.entities.storage import encoded
        result = {'revision': self.revision, 'entity_revision': self.entities.revision,
                  'relations': [self.resolve(identity) for identity in identities]}
        if len(encoded(result)) > max_bytes:
            raise CatalogError('selection_exceeds_response_budget')
        return result

    def join_pairs(self, identity, source_rows, target_rows):
        """Evaluate an explicit bridge against caller-supplied native records.

        Return row indices; no fuzzy/coercive or null joins. Cardinality is checked
        on matching rows. This does not claim that records exist in a live app.
        """
        row = self.resolve(identity)
        binding = row.get('binding')
        if not binding:
            raise CatalogError('relation_has_no_equality_binding', relation=identity)
        def keys(records, side):
            result = {}
            for index, record in enumerate(records):
                values = [record.get(pair[side]) for pair in binding['pairs']]
                if any(value is None or value == '' for value in values):
                    continue
                if any(type(value) not in (str, int, float, bool) for value in values):
                    raise CatalogError('bridge_join_value_not_scalar', side=side, index=index)
                key = digest([[type(value).__name__, value] for value in values])
                result.setdefault(key, []).append(index)
            return result
        left, right = keys(source_rows, 'source'), keys(target_rows, 'target')
        pairs = []
        cardinality = binding['cardinality']
        for key in sorted(left.keys() & right.keys()):
            if ((cardinality in {'one_to_one', 'one_to_many'} and len(left[key]) > 1)
                    or (cardinality in {'one_to_one', 'many_to_one'} and len(right[key]) > 1)):
                raise CatalogError('bridge_cardinality_violated', relation=identity)
            pairs.extend((a, b) for a in left[key] for b in right[key])
        return sorted(pairs)

    def coverage(self):
        return {'revision': self.revision, 'entity_revision': self.entities.revision,
                'total': len(self._records),
                'kinds': dict(Counter(row['kind'] for row in self._records.values())),
                'statuses': dict(Counter(row['status'] for row in self._records.values()))}

    def save(self, path):
        write_json(path, {**self._payload(), 'digest': self.revision})

    @classmethod
    def load(cls, path, *, entities):
        payload = read_json(path)
        if payload.get('format') != cls.FORMAT or payload.get('entity_revision') != entities.revision:
            raise CatalogError('relation_catalog_version_mismatch')
        result = cls(entities, payload['relations'])
        if result.revision != payload.get('digest'):
            raise CatalogError('relation_catalog_digest_mismatch')
        return result
