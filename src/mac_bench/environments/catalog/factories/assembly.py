"""Bind task resource contracts to native application clients."""
from copy import deepcopy
from dataclasses import dataclass, field

from mac_bench.environments.catalog.entities import CatalogError
from mac_bench.environments.catalog.entities.storage import digest
from mac_bench.environments.catalog.factories.registry import EntityFactory
from mac_bench.environments.catalog.relations.catalog import pages


@dataclass(frozen=True)
class AppConnection:
    application: str
    scope: dict
    transport: object
    settings: dict = field(default_factory=dict)


class BuildContext:
    def __init__(self, entities):
        self.entities = entities
        self.bindings = {}
        self.contracts = {}
        self.cards = {}
        self._types = {}
        for card in pages(entities.overview, role=None):
            self.cards[card['id']] = card
            for variant in entities.variants(card['id']):
                for identity in variant['bindings']:
                    binding = entities.binding(identity)
                    self.bindings[identity] = binding
                    self.contracts[identity] = entities.resolve(card['id'], binding=identity)['contract']

    def types(self, library):
        if library not in self._types:
            self._types[library] = {card['name']: self.entities.type_definition(library, card['name'])['types'][card['name']]
                                   for card in pages(self.entities.types, library=library)}
        return self._types[library]

    def target(self, entity, source_binding):
        source = self.bindings[source_binding]
        candidates = [b for b in self.bindings.values() if b['entity'] == entity and b['scope'] == source['scope']]
        if len(candidates) != 1:
            raise CatalogError('factory_parent_binding_ambiguous', entity=entity, source=source_binding)
        return candidates[0]

    def spec(self, binding, create, read, *, recipe, parameters, output, parents=None):
        entity = binding['entity']
        endpoint = lambda operation: {'entity': entity, 'binding': binding['id'], 'operation': operation}
        return {'id': binding['id'] + '/' + create, 'entity': entity, 'binding': binding['id'],
                'entity_revision': self.entities.revision,
                'description': f"Native {entity} creation via {create}",
                'parameters': parameters, 'output': output, 'parents': parents or {},
                'operations': {'create': endpoint(create), 'read': endpoint(read)},
                'sources': [*binding['operation_sources'][create], *binding['operation_sources'][read]],
                'recipe': recipe, 'application': self.cards[entity]['application'], 'scope': binding['scope'],
                'execution_verified': False}



def attach_connections(registry, connections):
    """Bind matching deployments to built-in adapters. No network during assembly."""
    from mac_bench.environments.catalog.factories.apps import (
        bigcapital,
        frappe,
        pretix,
        twenty,
    )
    from mac_bench.environments.catalog.factories.native import NativeRuntime
    modules = {'hrms': frappe, 'twenty': twenty, 'pretix': pretix, 'bigcapital': bigcapital}
    connections = list(connections)
    keys = [(c.application, digest(c.scope)) for c in connections]
    if len(keys) != len(set(keys)):
        raise CatalogError('factory_connection_scope_duplicate')
    runtime = NativeRuntime(registry, connections)
    providers = {}
    for identity, spec in registry._specs.items():
        matching = [c for c in connections if c.application == spec['application'] and c.scope == spec['scope']]
        if not matching:
            continue
        adapter = modules[spec['application']].adapter(deepcopy(spec), matching[0], runtime)
        providers[identity] = adapter.provider()
        runtime.adapters.setdefault(spec['binding'], []).append(adapter)
    # All adapters exist before any provider can resolve parents. No partial bind
    # is committed when construction fails.
    if registry._providers.keys() & providers.keys():
        raise CatalogError('factory_provider_already_bound')
    for identity, provider in providers.items():
        registry.bind(identity, provider)
    registry._runtime = runtime
    return registry

