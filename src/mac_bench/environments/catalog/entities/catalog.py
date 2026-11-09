"""Immutable, version-pinned business entities with bounded discovery interfaces."""
from copy import deepcopy

from mac_bench.environments.catalog.entities.errors import CatalogError
from mac_bench.environments.catalog.entities.pagination import paginate
from mac_bench.environments.catalog.entities.storage import (
    EvidenceStore,
    digest,
    encoded,
    read_json,
    write_json,
)
from mac_bench.environments.catalog.entities.type_graph import (
    references,
    validate_graph,
)

ROLES = frozenset({'resource', 'component', 'reference', 'internal'})
SEMANTICS = frozenset({'identity', 'reference', 'quantity', 'money', 'time', 'status', 'boolean', 'text'})
SECTIONS = frozenset({'fields', 'operations', 'references', 'components', 'bindings', 'diagnostics'})


class EntityCatalog:
    """No factory, application process, model client or downstream module needed.

    Entity IDs identify business resources. Variants identify semantic contracts;
    bindings identify native deployment instances. Evidence stays in separate files.
    All returned values are copies. Mutating them cannot change this catalog.
    """
    FORMAT = 'business-entity-catalog/v2'

    def __init__(self, payload, *, evidence=None):
        self._initialize(deepcopy(payload), evidence=evidence)

    def _initialize(self, payload, *, evidence=None):
        self._data = payload
        self._evidence = evidence
        self._validate()

    @property
    def revision(self):
        return self._data['digest']

    def application(self, application):
        """App-wide transport rules shared by its resource/operation contracts."""
        if application not in self._data['applications']:
            raise CatalogError('application_not_found', application=application)
        return deepcopy({'application': application, **self._data['applications'][application]})

    def _validate(self):
        data = self._data
        if (set(data) != {'format', 'applications', 'entities', 'bindings', 'coverage', 'digest', 'type_libraries'}
                or data['format'] != self.FORMAT
                or data['digest'] != digest({k: v for k, v in data.items() if k != 'digest'})):
            raise CatalogError('catalog_envelope_invalid')
        for entity_id, entity in data['entities'].items():
            if entity['id'] != entity_id or entity['application'] not in data['applications'] or not entity['variants']:
                raise CatalogError('entity_identity_invalid', entity=entity_id)
            if entity['role'] not in ROLES:
                raise CatalogError('entity_role_invalid', entity=entity_id)
            for variant_id, variant in entity['variants'].items():
                if variant_id != digest(variant):
                    raise CatalogError('variant_digest_mismatch', entity=entity_id)
                if set(variant) != {'identity', 'fields', 'operations', 'diagnostics'}:
                    raise CatalogError('variant_contract_invalid', entity=entity_id)
                fields = variant['fields']
                if set(variant['identity']['fields']) - fields.keys():
                    raise CatalogError('identity_field_missing', entity=entity_id)
                for field_id, field in fields.items():
                    if (not {'name', 'native_type', 'semantic_type', 'shape', 'read', 'write', 'constraints', 'native_mapping', 'dependencies'} <= set(field)
                            or field['name'] != field_id or field['semantic_type'] not in SEMANTICS | {None}):
                        raise CatalogError('field_contract_invalid', entity=entity_id, field=field_id)
                    for dependency in field.get('dependencies', []):
                        if dependency not in fields:
                            raise CatalogError('field_dependency_missing', entity=entity_id, field=field_id, dependency=dependency)
                    reference = field.get('reference')
                    if reference:
                        for target in reference.get('targets', []):
                            if target not in data['entities']:
                                raise CatalogError('reference_target_missing', target=target)
                for name, operation in variant['operations'].items():
                    if operation['name'] != name or operation['contract_status'] not in {'declared', 'partial', 'unavailable'}:
                        raise CatalogError('operation_contract_invalid', entity=entity_id, operation=name)
                    if set(operation.get('required_fields', [])) - fields.keys():
                        raise CatalogError('operation_field_missing', entity=entity_id, operation=name)
        for binding_id, binding in data['bindings'].items():
            entity = data['entities'].get(binding['entity'])
            if (binding_id != binding['id'] or entity is None or binding['variant'] not in entity['variants']):
                raise CatalogError('binding_invalid', binding=binding_id)
            variant = entity['variants'][binding['variant']]
            if set(binding['field_sources']) != set(variant['fields']):
                raise CatalogError('field_source_coverage_incomplete', binding=binding_id)
            if set(binding['operation_sources']) != set(variant['operations']):
                raise CatalogError('operation_source_coverage_incomplete', binding=binding_id)
            references = [binding['source'], *binding['field_sources'].values(),
                          *(ref for refs in binding['operation_sources'].values() for ref in refs)]
            for ref in references:
                if (ref.get('application') != entity['application']
                        or ref.get('sha256') != data['applications'][entity['application']]['evidence_sha256']):
                    raise CatalogError('binding_evidence_version_mismatch', binding=binding_id)
        validate_graph(data, self._evidence)

    def save(self, path):
        write_json(path, self._data)

    @classmethod
    def load(cls, path, *, evidence_manifest=None):
        evidence = EvidenceStore(evidence_manifest) if evidence_manifest else None
        # The decoder owns this graph exclusively. The public constructor still
        # copies caller-owned data, but loading need not duplicate the entire
        # catalog before validating it.
        payload = read_json(path)
        if cls is EntityCatalog:
            result = cls.__new__(cls)
            result._initialize(payload)
        else:
            result = cls(payload)
        if evidence:
            for app, spec in result._data['applications'].items():
                if evidence.manifest['applications'].get(app, {}).get('sha256') != spec['evidence_sha256']:
                    raise CatalogError('catalog_evidence_version_mismatch', application=app)
        result._evidence = evidence
        return result

    def _entity(self, entity):
        if entity not in self._data['entities']:
            raise CatalogError('entity_not_found', entity=entity)
        return self._data['entities'][entity]

    def _variant(self, entity, *, variant=None, binding=None):
        item = self._entity(entity)
        if binding is not None:
            selected = self._data['bindings'].get(binding)
            if selected is None or selected['entity'] != entity:
                raise CatalogError('binding_entity_mismatch', entity=entity, binding=binding)
            if variant is not None and variant != selected['variant']:
                raise CatalogError('binding_variant_mismatch')
            variant = selected['variant']
        if variant is None:
            if len(item['variants']) != 1:
                raise CatalogError('variant_required', entity=entity, variants=sorted(item['variants']))
            variant = next(iter(item['variants']))
        if variant not in item['variants']:
            raise CatalogError('variant_not_found', entity=entity, variant=variant)
        return variant, item['variants'][variant]

    def _page(self, items, *, query, **page):
        return paginate(items, revision=self.revision, query=query, **page)

    def overview(self, *, application=None, group=None, role='resource', query=None, **page):
        """Compact resource cards. No raw schema, source code or internal fields."""
        if role is not None and role not in ROLES:
            raise CatalogError('unknown_role', role=role)
        if application is not None and application not in self._data['applications']:
            raise CatalogError('application_not_found', application=application)
        rows = []
        for key, entity in sorted(self._data['entities'].items()):
            if application is not None and entity['application'] != application:
                continue
            if role is not None and entity['role'] != role:
                continue
            if group is not None and entity['group'] != group:
                continue
            if query and query.casefold() not in (key + ' ' + entity['label'] + ' ' + entity['description']).casefold():
                continue
            names = set().union(*(v['operations'].keys() for v in entity['variants'].values()))
            rows.append({'id': key, 'application': entity['application'], 'label': entity['label'],
                         'group': entity['group'], 'role': entity['role'], 'description': entity['description'],
                         'variant_count': len(entity['variants']),
                         'actions': {name: sorted({v['operations'][name]['contract_status'] for v in entity['variants'].values() if name in v['operations']}) for name in sorted(names)}})
        return self._page(rows, query=['overview', application, group, role, query], **page)

    def groups(self, *, application=None, role='resource', **page):
        if application is not None and application not in self._data['applications']:
            raise CatalogError('application_not_found', application=application)
        if role is not None and role not in ROLES:
            raise CatalogError('unknown_role', role=role)
        groups = {}
        for item in self._data['entities'].values():
            if application is not None and item['application'] != application:
                continue
            if role is not None and item['role'] != role:
                continue
            key = (item['application'], item['group'])
            groups[key] = groups.get(key, 0) + 1
        rows = [{'application': app, 'group': group, 'resources': count} for (app, group), count in sorted(groups.items())]
        return self._page(rows, query=['groups', application, role], **page)

    def describe(self, entity, *, variant=None, binding=None, section='fields', **page):
        if section not in SECTIONS:
            raise CatalogError('unknown_section', section=section)
        variant_id, contract = self._variant(entity, variant=variant, binding=binding)
        if section in {'fields', 'operations'}:
            rows = [v for _, v in sorted(contract[section].items())]
        elif section == 'references':
            rows = [{'field': k, **v['reference']} for k, v in sorted(contract['fields'].items()) if v.get('reference')]
        elif section == 'components':
            rows = [{'field': k, **v['reference']} for k, v in sorted(contract['fields'].items())
                    if v.get('reference', {}).get('kind') == 'owned_component']
        elif section == 'bindings':
            rows = [{'id': k, 'native_resource': v['native_resource'], 'scope': v['scope']}
                    for k, v in sorted(self._data['bindings'].items()) if v['entity'] == entity and v['variant'] == variant_id]
        else:
            rows = contract['diagnostics']
        return self._page(rows, query=['describe', entity, variant_id, section], **page)

    def resolve(self, entity, *, variant=None, binding=None, field=None, operation=None, revision=None):
        """Exact machine interface; never fuzzy-match or silently choose a variant."""
        if revision is not None and revision != self.revision:
            raise CatalogError('catalog_revision_mismatch')
        if field is not None and operation is not None:
            raise CatalogError('ambiguous_member_selection')
        variant_id, contract = self._variant(entity, variant=variant, binding=binding)
        member = contract
        if field is not None:
            if field not in contract['fields']:
                raise CatalogError('field_not_found', entity=entity, field=field)
            member = contract['fields'][field]
        if operation is not None:
            if operation not in contract['operations']:
                raise CatalogError('operation_not_found', entity=entity, operation=operation)
            member = contract['operations'][operation]
        return {'revision': self.revision, 'entity': entity, 'variant': variant_id, 'contract': deepcopy(member)}

    def entity(self, identity):
        """Small resource descriptor including exact version choices."""
        item = self._entity(identity)
        return {**{key: deepcopy(value) for key, value in item.items() if key != 'variants'},
                'variants': self.variants(identity), 'revision': self.revision}

    def select(self, requests, *, max_bytes=1000000):
        """Resolve explicit selections plus local contract dependencies.

        Reference targets are typed endpoints, not instructions to instantiate
        their entire transitive graph. Creation ordering belongs to factories.
        No selection is silently truncated to fit the response budget.
        """
        if not isinstance(requests, list) or not requests:
            raise CatalogError('selection_requires_requests')
        selected = []
        for request in requests:
            if not isinstance(request, dict) or 'entity' not in request or set(request) - {'entity', 'variant', 'binding', 'fields', 'operations'}:
                raise CatalogError('selection_request_invalid')
            entity = request['entity']
            variant, contract = self._variant(entity, variant=request.get('variant'), binding=request.get('binding'))
            names = request.get('fields', [])
            actions = request.get('operations', [])
            if not isinstance(names, list) or not isinstance(actions, list):
                raise CatalogError('selection_members_require_lists')
            if any(not isinstance(x, str) for x in names + actions):
                raise CatalogError('selection_member_invalid')
            fields, operations = set(names), set(actions)
            if fields - contract['fields'].keys() or operations - contract['operations'].keys():
                raise CatalogError('selection_member_not_found', entity=entity)
            for action in operations:
                operation = contract['operations'][action]
                fields.update(operation.get('required_fields', []))
                # A selected document operation carries its input members. Omitting
                # conditionally required fields would weaken the native contract.
                fields.update(operation.get('input', {}).get('document_fields', []))
            fields.update(name for name in contract['identity']['fields'] if name in contract['fields'])
            queue = list(fields)
            while queue:
                name = queue.pop()
                for dependency in contract['fields'][name].get('dependencies', []):
                    if dependency not in fields:
                        fields.add(dependency)
                        queue.append(dependency)
            selected.append({'entity': entity, 'variant': variant, 'identity': deepcopy(contract['identity']),
                             'fields': {name: deepcopy(contract['fields'][name]) for name in sorted(fields)},
                             'operations': {name: deepcopy(contract['operations'][name]) for name in sorted(operations)},
                             'diagnostics': deepcopy(contract['diagnostics'])})
        result = {'revision': self.revision, 'selection': selected}
        if type(max_bytes) is not int or max_bytes < 1024:
            raise CatalogError('response_budget_invalid')
        if len(encoded(result)) > max_bytes:
            raise CatalogError('selection_exceeds_response_budget', required_bytes=len(encoded(result)), max_bytes=max_bytes)
        return result

    def variants(self, entity):
        item = self._entity(entity)
        return [{'id': key, 'bindings': [b['id'] for b in self._data['bindings'].values()
                                       if b['entity'] == entity and b['variant'] == key]} for key in sorted(item['variants'])]

    def binding(self, identity):
        if identity not in self._data['bindings']:
            raise CatalogError('binding_not_found', binding=identity)
        return deepcopy(self._data['bindings'][identity])

    def owners(self, entity, **page):
        """List declared component ownership without inventing creation policies."""
        item = self._entity(entity)
        rows = []
        for variant, contract in sorted(item['variants'].items()):
            for owner in contract['identity'].get('component', {}).get('owners', []):
                rows.append({**owner, 'component_variant': variant})
        if rows:
            return self._page(rows, query=['owners', entity], **page)
        for owner, descriptor in sorted(self._data['entities'].items()):
            for variant, contract in sorted(descriptor['variants'].items()):
                for name, field in sorted(contract['fields'].items()):
                    ref = field.get('reference', {})
                    if ref.get('kind') == 'owned_component' and entity in ref.get('targets', []):
                        rows.append({'owner': owner, 'variant': variant, 'field': name, 'cardinality': ref['cardinality']})
        return self._page(rows, query=['owners', entity], **page)

    def source(self, entity, *, binding=None, field=None, operation=None):
        """Retrieve evidence pointers without loading source documents."""
        if field is not None and operation is not None:
            raise CatalogError('ambiguous_member_selection')
        self._entity(entity)
        candidates = [b for b in self._data['bindings'].values() if b['entity'] == entity]
        if binding is not None:
            candidates = [b for b in candidates if b['id'] == binding]
            if not candidates:
                raise CatalogError('binding_entity_mismatch', entity=entity, binding=binding)
        rows = []
        for item in candidates:
            if field is not None:
                refs = [item['field_sources'][field]] if field in item['field_sources'] else []
            elif operation is not None:
                refs = item['operation_sources'].get(operation, [])
            else:
                refs = [item['source']]
            rows.extend({'binding': item['id'], 'reference': deepcopy(ref)} for ref in refs)
        if not rows:
            raise CatalogError('member_evidence_not_found', entity=entity, field=field, operation=operation)
        return rows

    def evidence(self, reference):
        if self._evidence is None:
            raise CatalogError('evidence_store_not_attached')
        return self._evidence.resolve(reference)

    def coverage(self):
        return deepcopy(self._data['coverage'])

    def types(self, library, *, query=None, **page):
        """Discover native input/output types without expanding their graphs."""
        if library not in self._data['type_libraries']:
            raise CatalogError('type_library_not_found', library=library)
        spec = self._data['type_libraries'][library]
        rows = [{'name': name, 'format': spec['format']} for name in sorted(spec['types'])
                if not query or query.casefold() in name.casefold()]
        return self._page(rows, query=['types', library, query], **page)

    def type_definition(self, library, name, *, closure=False, max_bytes=1000000):
        """Resolve a native type, optionally with its finite referenced-type graph.

        Recursive schemas remain references. Input and response graphs are not
        merged; requesting an input type does not pull in its response graph.
        """
        if library not in self._data['type_libraries']:
            raise CatalogError('type_library_not_found', library=library)
        spec = self._data['type_libraries'][library]
        if name not in spec['types']:
            raise CatalogError('native_type_not_found', library=library, name=name)
        if type(max_bytes) is not int or max_bytes < 1024:
            raise CatalogError('response_budget_invalid')
        selected = {}
        pending = [name]
        while pending:
            current = pending.pop()
            if current in selected:
                continue
            if current not in spec['types']:
                raise CatalogError('native_type_reference_missing', library=library, name=current)
            selected[current] = spec['types'][current]
            if closure:
                pending.extend(references(selected[current], spec['format']))
        result = {'revision': self.revision, 'library': library, 'format': spec['format'],
                  'root': name, 'types': selected}
        if len(encoded(result)) > max_bytes:
            raise CatalogError('type_graph_exceeds_response_budget', required_bytes=len(encoded(result)), max_bytes=max_bytes)
        return deepcopy(result)
