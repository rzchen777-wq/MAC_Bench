"""Host-only provisioning: deployment URLs and credentials come from the environment."""
import os
from pathlib import Path

from mac_bench.construction.realize.catalog.entities import EntityCatalog
from mac_bench.construction.realize.runtime.infrastructure.transport import RestTransport
from mac_bench.construction.realize.tools import (
    AppConnection,
    CallJournal,
    Role,
    WorldTools,
    build_registry,
)


def provision(config, *, environ=None):
    environ = os.environ if environ is None else environ
    def env(name):
        value = environ.get(name)
        if not value:
            raise ValueError('Required environment variable is missing: ' + name)
        return value
    catalog = EntityCatalog.load(config['catalog'])
    roles = []
    for role in config['roles']:
        connections = []
        for spec in role['connections']:
            transport = RestTransport(env(spec['base_url_env']),
                headers={name: env(key) for name, key in spec.get('headers_env', {}).items()},
                timeout=spec.get('timeout', 30))
            connections.append(AppConnection(spec['application'], spec['scope'], transport,
                {'path_parameters': spec.get('path_parameters', {})}))
        roles.append(Role(role['name'], tuple(connections), tuple(role['allowed_tools'])))
    scopes = {(c.application, str(sorted(c.scope.items()))) for r in roles for c in r.connections}
    # Include every business binding for these deployments, not per-task reference actions.
    from mac_bench.construction.realize.catalog.relations.catalog import pages
    bindings = []
    for card in pages(catalog.overview, role=None):
        for variant in catalog.variants(card['id']):
            for key in variant['bindings']:
                binding = catalog.binding(key)
                if (card['application'], str(sorted(binding['scope'].items()))) in scopes:
                    bindings.append(key)
    registry = build_registry(catalog, bindings=bindings)
    journal_path = Path(config['journal'])
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal = CallJournal(journal_path)
    world = WorldTools(config['world_id'], registry, roles, journal)
    sessions = {}
    try:
        for entry in config['agents']:
            token = env(entry['token_env'])
            if token in sessions:
                raise ValueError('Duplicate agent session token')
            sessions[token] = world.agent(entry['actor'], entry['role'])
        if not sessions:
            raise ValueError('At least one agent is required')
    except Exception:
        journal.close()
        raise
    return world, sessions
