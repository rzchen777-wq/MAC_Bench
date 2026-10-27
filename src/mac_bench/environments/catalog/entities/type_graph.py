"""Shared native-type traversal for validation and bounded dependency closure."""
from mac_bench.environments.catalog.entities.errors import CatalogError
from mac_bench.environments.catalog.entities.storage import digest


def references(value, format):
    if isinstance(value, list):
        for item in value:
            yield from references(item, format)
    elif isinstance(value, dict):
        if format == 'graphql-introspection':
            if value.get('kind') in {'SCALAR', 'ENUM', 'OBJECT', 'INPUT_OBJECT', 'INTERFACE', 'UNION'} and value.get('name'):
                yield value['name']
        elif format == 'openapi-components':
            if isinstance(value.get('$ref'), str) and value['$ref'].startswith('#/components/schemas/'):
                yield value['$ref'].removeprefix('#/components/schemas/')
        elif format == 'drf-structural-declarations':
            if isinstance(value.get('schema'), str) and value['schema'].startswith('schema/'):
                yield value['schema'].removeprefix('schema/')
            if isinstance(value.get('serializer'), str):
                yield value['serializer']
            yield from value.get('serializers', [])
        for child in value.values():
            yield from references(child, format)


def validate_graph(data, evidence=None):
    libraries = data['type_libraries']
    checked_sources = set()

    def sources(value):
        if isinstance(value, list):
            for item in value:
                sources(item)
        elif isinstance(value, dict):
            if {'application', 'sha256', 'pointer'} <= value.keys():
                app = value['application']
                if app not in data['applications'] or value['sha256'] != data['applications'][app]['evidence_sha256']:
                    raise CatalogError('type_evidence_version_mismatch', reference=value)
                key = (app, value['pointer'])
                if evidence and key not in checked_sources:
                    evidence.resolve(value)
                    checked_sources.add(key)
            for item in value.values():
                sources(item)

    def check(library, value):
        if library not in libraries:
            raise CatalogError('type_library_not_found', library=library)
        spec = libraries[library]
        for name in references(value, spec['format']):
            if name not in spec['types']:
                raise CatalogError('native_type_reference_missing', library=library, name=name)

    for identifier, spec in libraries.items():
        if identifier.split(':', 1)[1] != digest({k: v for k, v in spec.items() if k != 'sources'}):
            raise CatalogError('type_library_digest_mismatch', library=identifier)
        check(identifier, spec['types'])
        sources(spec)
    for entity in data['entities'].values():
        for variant in entity['variants'].values():
            for owner in variant['identity'].get('component', {}).get('owners', []):
                if owner['owner'] not in data['entities']:
                    raise CatalogError('component_owner_missing', entity=entity['id'])
            sources(variant['identity'])
            for operation in variant['operations'].values():
                for section in ('input', 'response'):
                    contract = operation.get(section, {})
                    if contract.get('library'):
                        check(contract['library'], contract)
                sources(operation)
    # Bindings contain all resource, field and operation source pointers.
    sources(data['bindings'])
    sources(data['applications'])
