"""Reusable entity creation contracts with strict native execution boundaries."""
from copy import deepcopy

from jsonschema import Draft202012Validator
from referencing import Registry

from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.entities.pagination import paginate
from mac_bench.environments.catalog.entities.storage import (
    digest,
    read_json,
    write_json,
)
from mac_bench.environments.catalog.factories.contracts import (
    BatchResult,
    CreationResult,
    FactoryExecutionError,
    NativeObject,
    NativeProvider,
)


def _schema(schema):
    if not isinstance(schema, (dict, bool)):
        raise CatalogError('factory_schema_invalid')
    if isinstance(schema, dict) and schema.get('$schema', 'https://json-schema.org/draft/2020-12/schema') != 'https://json-schema.org/draft/2020-12/schema':
        raise CatalogError('factory_schema_dialect_unsupported')
    Draft202012Validator.check_schema(schema)
    # No remote retrieval or implicit resolution against deployment URLs.
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {'$ref', '$dynamicRef'} and not child.startswith('#'):
                    raise CatalogError('factory_schema_external_reference', reference=child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    return Draft202012Validator(schema, registry=Registry())


def _validate_value(schema, value, *, section):
    errors = sorted(_schema(schema).iter_errors(value), key=lambda error: str(list(error.path)))
    if errors:
        raise CatalogError('factory_value_invalid', section=section,
                           errors=[{'path': list(error.path), 'rule': error.validator,
                                    'message': error.message} for error in errors])


def _identity(identity):
    if (not isinstance(identity, dict) or not identity
            or any(not isinstance(key, str) or not key or type(value) not in (str, int)
                   or value == '' for key, value in identity.items())):
        raise CatalogError('factory_native_identity_invalid')
    return deepcopy(identity)


class EntityFactory:
    """Registry + execution facade. Specs are portable; providers are runtime-only.

    A factory ID identifies one reusable creation path for an exact native entity
    binding (e.g. draft creation vs a lifecycle operation producing a document).
    Missing implementations are visible but never advertised as ready.
    """
    FORMAT = 'entity-factories/v1'

    def __init__(self, entities):
        self.entities = entities
        self._specs = {}
        self._providers = {}
        self._coverage = []
        self._spec_revisions = {}
        self._revision_cache = None

    @property
    def revision(self):
        if self._revision_cache is None:
            self._revision_cache = digest({'entity_revision': self.entities.revision, 'specs': self._specs})
        return self._revision_cache

    def register(self, spec, provider=None):
        """Register a complete JSON contract and optionally its native adapter.

        Required spec keys: id, entity, binding, entity_revision, description,
        parameters, output, parents, operations, sources.
        parents maps slot -> {entity, binding, required, many}.
        operations maps role 'create'/'read' -> {entity, binding, operation}; the
        creator may be another resource's lifecycle action. Extra roles allowed.
        JSON schemas describe factory parameters and independently read output.
        """
        if not isinstance(spec, dict):
            raise CatalogError('factory_spec_invalid')
        spec = deepcopy(spec)
        required = {'id', 'entity', 'binding', 'entity_revision', 'description',
                    'parameters', 'output', 'parents', 'operations', 'sources'}
        if not required <= spec.keys() or not spec['id'] or not spec['description'] or not spec['sources']:
            raise CatalogError('factory_spec_invalid')
        if (not isinstance(spec['id'], str) or not isinstance(spec['parents'], dict)
                or not isinstance(spec['operations'], dict) or not isinstance(spec['sources'], list)):
            raise CatalogError('factory_spec_invalid')
        if spec['id'] in self._specs:
            raise CatalogError('factory_already_registered', factory=spec['id'])
        if spec['entity_revision'] != self.entities.revision:
            raise CatalogError('factory_entity_revision_mismatch')
        if not isinstance(spec['binding'], str) or not spec['binding']:
            raise CatalogError('factory_binding_required')
        selected = self.entities.resolve(spec['entity'], binding=spec['binding'])
        spec['variant'] = selected['variant']
        _schema(spec['parameters'])
        _schema(spec['output'])
        if not {'create', 'read'} <= spec['operations'].keys():
            raise CatalogError('factory_operation_roles_missing')
        for role, endpoint in spec['operations'].items():
            if not isinstance(endpoint, dict) or not {'entity', 'binding', 'operation'} <= endpoint.keys():
                raise CatalogError('factory_operation_contract_invalid', role=role)
            if not isinstance(endpoint.get('binding'), str) or not endpoint['binding']:
                raise CatalogError('factory_operation_binding_required', role=role)
            operation = self.entities.resolve(endpoint['entity'], binding=endpoint['binding'],
                                              operation=endpoint['operation'])['contract']
            if operation['contract_status'] != 'declared':
                raise CatalogError('factory_operation_not_declared', role=role)
        read = spec['operations']['read']
        if read['entity'] != spec['entity'] or read['binding'] != spec['binding']:
            raise CatalogError('factory_read_binding_mismatch')
        for slot, parent in spec['parents'].items():
            if not isinstance(parent, dict) or not {'entity', 'binding', 'required', 'many'} <= parent.keys():
                raise CatalogError('factory_parent_contract_invalid', slot=slot)
            if (not slot or type(parent.get('required')) is not bool or type(parent.get('many')) is not bool):
                raise CatalogError('factory_parent_contract_invalid', slot=slot)
            if not isinstance(parent.get('binding'), str) or not parent['binding']:
                raise CatalogError('factory_parent_binding_required', slot=slot)
            self.entities.resolve(parent['entity'], binding=parent['binding'])
        if provider is not None:
            self._check_provider(spec, provider)
        version = digest(spec)  # Reject invalid JSON before mutating the registry.
        self._specs[spec['id']] = spec
        if hasattr(self, '_reference_contracts'):
            del self._reference_contracts
        self._spec_revisions[spec['id']] = version
        self._revision_cache = None
        if provider is not None:
            self._providers[spec['id']] = provider
        return self.describe(spec['id'])

    @staticmethod
    def _check_provider(spec, provider):
        if not isinstance(provider, NativeProvider) or any(
                not callable(getattr(provider, name)) for name in
                ('prepare', 'validate', 'create', 'identify', 'read', 'verify')):
            raise CatalogError('factory_provider_invalid')
        if spec['parents'] and not callable(provider.read_parent):
            raise CatalogError('factory_parent_reader_required')

    def bind(self, identity, provider):
        """Attach executable app capabilities to a loaded, versioned spec."""
        spec = self._spec(identity)
        self._check_provider(spec, provider)
        if identity in self._providers:
            raise CatalogError('factory_provider_already_bound', factory=identity)
        self._providers[identity] = provider

    def _spec(self, identity):
        if identity not in self._specs:
            raise CatalogError('factory_not_found', factory=identity)
        return self._specs[identity]

    def _provider(self, identity):
        self._spec(identity)
        if identity not in self._providers:
            raise CatalogError('factory_provider_unbound', factory=identity)
        return self._providers[identity]

    def describe(self, identity):
        spec = deepcopy(self._spec(identity))
        return {'spec': spec, 'revision': self._spec_revisions[identity], 'ready': identity in self._providers}

    def reference_contracts(self, identity):
        """Exact native reference paths and target types, including child arrays."""
        from mac_bench.environments.catalog.factories.references import (
            ReferenceContracts,
        )
        if not hasattr(self, '_reference_contracts'):
            self._reference_contracts = ReferenceContracts(self)
        return self._reference_contracts.describe(self._spec(identity))

    def overview(self, *, entity=None, ready_only=False, **page):
        if entity is not None:
            self.entities.entity(entity)
        rows = [{'id': identity, 'entity': spec['entity'], 'binding': spec['binding'],
                 'description': spec['description'], 'revision': self._spec_revisions[identity],
                 'ready': identity in self._providers}
                for identity, spec in sorted(self._specs.items())
                if (entity is None or spec['entity'] == entity)
                and (not ready_only or identity in self._providers)]
        # Binding a provider changes discovery, so it must invalidate its cursor.
        revision = digest([self.revision, sorted(self._providers)])
        return paginate(rows, revision=revision, query=['factories', entity, ready_only], **page)

    def _parents(self, spec, parents, *, require_required=True):
        if not isinstance(parents, dict) or parents.keys() - spec['parents'].keys():
            raise CatalogError('factory_parent_slots_invalid')
        for slot, contract in spec['parents'].items():
            if slot not in parents:
                if require_required and contract['required']:
                    raise CatalogError('factory_parent_missing', slot=slot)
                continue
            objects = parents[slot]
            if contract['many']:
                if not isinstance(objects, (list, tuple)) or (contract['required'] and not objects):
                    raise CatalogError('factory_parent_cardinality_invalid', slot=slot)
            else:
                objects = [objects]
            for obj in objects:
                if (not isinstance(obj, NativeObject) or obj.entity != contract['entity']
                        or obj.binding != contract['binding']):
                    raise CatalogError('factory_parent_type_mismatch', slot=slot)
                _identity(dict(obj.identity))
        return deepcopy(parents)

    def validate(self, identity, parameters, *, parents=None):
        """Pure contract validation; does not assert live parent existence."""
        spec = self._spec(identity)
        checked = self._parents(spec, {} if parents is None else parents)
        if spec.get('recipe'):
            from mac_bench.environments.catalog.factories.native import (
                merge_parents,
            )
            parameters = merge_parents(parameters, checked, spec['recipe'].get('parent_projection', {}))
        _validate_value(spec['parameters'], parameters, section='parameters')
        return {'factory': identity, 'revision': self._spec_revisions[identity],
                'parameters': deepcopy(parameters), 'parents': checked}

    def prepare(self, identity, parameters, *, parents=None):
        """Pure native payload projection; not a live-app validation certificate."""
        request = self.validate(identity, parameters, parents=parents)
        provider = self._provider(identity)
        return deepcopy(provider.prepare(request['parameters'], request['parents']))

    def _read_parents(self, spec, provider, parents):
        result = {}
        for slot, supplied in parents.items():
            many = spec['parents'][slot]['many']
            checked = []
            for obj in supplied if many else [supplied]:
                actual = provider.read_parent(deepcopy(obj))
                if (not isinstance(actual, NativeObject) or actual.entity != obj.entity
                        or actual.binding != obj.binding or dict(actual.identity) != dict(obj.identity)):
                    raise CatalogError('factory_parent_readback_mismatch', slot=slot)
                checked.append(actual)
            result[slot] = checked if many else checked[0]
        return result

    def read(self, identity, native_identity, *, parents=None):
        spec, provider = self._spec(identity), self._provider(identity)
        checked = self._parents(spec, {} if parents is None else parents, require_required=False)
        locator = _identity(native_identity)
        data = provider.read(deepcopy(locator), checked)
        if not isinstance(data, dict):
            raise CatalogError('factory_readback_not_object')
        _validate_value(spec['output'], data, section='readback')
        if _identity(provider.identify(data)) != locator:
            raise CatalogError('factory_readback_identity_mismatch')
        return NativeObject(spec['entity'], spec['binding'], locator, deepcopy(data))

    def create(self, identity, parameters, *, parents=None, revision=None):
        spec, provider = self._spec(identity), self._provider(identity)
        if revision is not None and revision != self._spec_revisions[identity]:
            raise CatalogError('factory_revision_mismatch')
        request = self.validate(identity, parameters, parents=parents)
        phase, receipt, locator, may_write = 'parents', None, None, False
        try:
            checked = self._read_parents(spec, provider, request['parents'])
            phase = 'prepare'
            payload = provider.prepare(request['parameters'], deepcopy(checked))
            phase = 'validate'
            if provider.validate(deepcopy(payload), deepcopy(checked)) is False:
                raise CatalogError('factory_native_validation_failed')
            phase, may_write = 'create', True
            receipt = provider.create(deepcopy(payload), deepcopy(checked))
            phase = 'identity'
            locator = _identity(provider.identify(receipt))
            phase = 'readback'
            obj = self.read(identity, locator, parents=checked)
            phase = 'verify'
            if provider.verify(deepcopy(payload), deepcopy(obj.data), deepcopy(checked)) is False:
                raise CatalogError('factory_native_verification_failed')
            return CreationResult(identity, self._spec_revisions[identity], obj, deepcopy(receipt))
        except Exception as exc:
            # Preserve the native exception as cause, including asynchronous app
            # operation handles. Never repeat a write or claim rollback happened.
            raise FactoryExecutionError(factory=identity, phase=phase,
                                        side_effects_possible=may_write,
                                        identity=locator, receipt=receipt) from exc

    def create_many(self, requests):
        """Ordered creation, stop on error, no hidden retry/rollback/dependency solver."""
        completed = []
        for index, request in enumerate(requests):
            try:
                completed.append(self.create(**request))
            except Exception as exc:
                return BatchResult(tuple(completed), index, exc)
        return BatchResult(tuple(completed), None, None)

    def coverage(self):
        return deepcopy({'entity_revision': self.entities.revision, 'revision': self.revision,
                         'factories': len(self._specs), 'ready': len(self._providers),
                         'bindings': self._coverage})

    def save(self, path):
        """Export definitions only. Runtime credentials/callables are never saved."""
        write_json(path, {'format': self.FORMAT, 'entity_revision': self.entities.revision,
                          'specs': list(self._specs.values()), 'coverage': self._coverage, 'digest': self.revision})

    @classmethod
    def load(cls, path, *, entities):
        payload = read_json(path)
        if payload.get('format') != cls.FORMAT or payload.get('entity_revision') != entities.revision:
            raise CatalogError('factory_catalog_version_mismatch')
        result = cls(entities)
        for spec in payload['specs']:
            result.register(spec)
        result._coverage = deepcopy(payload.get('coverage', []))
        if result.revision != payload.get('digest'):
            raise CatalogError('factory_catalog_digest_mismatch')
        return result
