"""Shared parent materialization and response validation for native adapters."""
from copy import deepcopy

from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.factories.contracts import (
    NativeObject,
    NativeProvider,
)
from mac_bench.environments.catalog.factories.registry import _validate_value


def object_schema(properties, required=(), *, closed=True):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': not closed}


def parameter_schema(values, **extra):
    values = deepcopy(values)
    definitions = values.pop('$defs', {})
    return {**object_schema({'values': values, **extra}, ['values']), '$defs': definitions}


def identity_output(fields):
    return object_schema({name: {'type': ['string', 'integer']} for name in fields}, fields, closed=False)


class NativeRuntime:
    def __init__(self, registry, connections):
        self.registry = registry
        self.connections = connections
        self.adapters = {}

    def read_parent(self, obj):
        candidates = self.adapters.get(obj.binding, [])
        adapter = next((a for a in candidates if set(obj.identity) ==
                        set(a.recipe['identity_fields']) | set(a.recipe.get('read_scope_fields', a.recipe.get('scope_fields', [])))), None)
        if adapter is None:
            from mac_bench.environments.catalog.factories.readers import (
                read_existing,
            )
            return read_existing(self, obj)
        data = adapter.read(dict(obj.identity), {})
        if adapter.identify(data) != dict(obj.identity):
            raise CatalogError('factory_parent_readback_mismatch')
        return NativeObject(obj.entity, obj.binding, deepcopy(obj.identity), data)


class NativeAdapter:
    def __init__(self, spec, connection, runtime):
        self.spec, self.connection, self.runtime = spec, connection, runtime
        self.recipe = spec['recipe']
        self.transport = connection.transport

    def provider(self):
        return NativeProvider(self.prepare, self.validate, self.create, self.identify,
                              self.read, self.verify, self.runtime.read_parent)

    def prepare(self, parameters, parents):
        return merge_parents(parameters, parents, self.recipe.get('parent_projection', {}))

    def validate(self, payload, parents):
        _validate_value(self.recipe['input_schema'], payload['values'], section='native_input')

    def identify(self, record):
        fields = self.recipe['identity_fields']
        if not isinstance(record, dict) or any(name not in record for name in fields):
            raise CatalogError('factory_native_identity_missing', factory=self.spec['id'])
        return {name: record[name] for name in fields}

    def verify(self, payload, record, parents):
        # Server controllers remain authoritative. This checks the actual read
        # contract; app-specific state and parent invariants are checked below.
        _validate_value(self.spec['output'], record, section='native_output')
        business_record = record['record'] if self.recipe['kind'] in {'pretix', 'bigcapital'} else record
        for projection in self.recipe.get('parent_projection', {}).values():
            read_field = projection.get('read_field')
            if read_field and read_field in business_record:
                expected = payload
                for key in projection['path']:
                    if not isinstance(expected, dict) or key not in expected:
                        break
                    expected = expected[key]
                else:
                    if business_record[read_field] != expected:
                        raise CatalogError('factory_parent_persistence_mismatch', field=read_field)


def merge_parents(parameters, parents, projections):
    result = deepcopy(parameters)
    for slot, parent in parents.items():
        projection = projections.get(slot)
        if projection is None:
            continue  # A path-scoped parent, consumed by the native adapter.
        value = parent.identity[projection['identity']]
        path = projection['path']
        target = result
        for key in path[:-1]:
            target = target.setdefault(key, {})
        if path[-1] in target and target[path[-1]] != value:
            raise CatalogError('factory_parent_parameter_conflict', slot=slot)
        target[path[-1]] = value
    return result
