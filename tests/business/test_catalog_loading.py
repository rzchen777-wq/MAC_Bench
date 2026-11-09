"""Loading shortcuts preserve validation, ownership and dependency resolution."""
from copy import deepcopy
from hashlib import sha256

import pytest

from mac_bench.environments.catalog.entities import EntityCatalog, CatalogError
from mac_bench.environments.catalog.entities.storage import digest, write_json
from mac_bench.environments.catalog.factories.assembly import BuildContext
from mac_bench.environments.tools.contracts import Tool, encoded, object_schema
from mac_bench.environments.tools.registry import ToolRegistry


@pytest.mark.parametrize('suffix', ['.json', '.json.gz'])
def test_catalog_load_validates_and_preserves_public_copy_boundary(tmp_path, suffix):
    payload = {'format': EntityCatalog.FORMAT, 'applications': {'app': {'label': ['Original']}},
               'entities': {}, 'bindings': {}, 'coverage': [], 'type_libraries': {}}
    payload['digest'] = digest(payload)
    path = tmp_path / ('catalog' + suffix)
    write_json(path, payload)
    direct, loaded = EntityCatalog(payload), EntityCatalog.load(path)
    payload['applications']['app']['label'][0] = 'caller mutation'
    for catalog in (direct, loaded):
        result = catalog.application('app')
        result['label'][0] = 'returned value mutation'
        assert catalog.application('app')['label'] == ['Original']
        assert catalog.revision == payload['digest']
    write_json(path, payload)
    with pytest.raises(CatalogError, match='catalog_envelope_invalid'):
        EntityCatalog.load(path)


def test_build_context_loads_cross_binding_dependencies_without_eager_copies():
    calls = []
    class Entities:
        def overview(self, **kwargs):
            return {'items': [{'id': 'child'}, {'id': 'parent'}, {'id': 'unused'}], 'next_cursor': None}
        def variants(self, entity):
            return [{'bindings': [entity + '-binding']}]
        def binding(self, identity):
            return {'id': identity, 'entity': identity.removesuffix('-binding'), 'scope': {}}
        def resolve(self, entity, *, binding):
            calls.append(binding)
            return {'contract': {'fields': {'name': {'label': entity}}}}
    context = BuildContext(Entities())
    assert not calls
    assert set(context.contracts) == set(context.bindings)
    assert len(context.contracts) == 3
    child = context.contracts['child-binding']
    parent = context.target('parent', 'child-binding')
    assert context.contracts[parent['id']]['fields']['name']['label'] == 'parent'
    assert context.contracts['child-binding'] is child
    assert calls == ['child-binding', 'parent-binding']
    with pytest.raises(KeyError):
        context.contracts['missing']


def test_registry_hash_matches_public_archive_and_archive_is_detached():
    tool = Tool('app.entity.read', 'app', 'app.entity', 'read', 'binding', {},
                'Read record', object_schema({'id': {'type': 'integer'}}), 'Record', True,
                lambda *args: None, native_contract={'fields': {'name': {'label': 'Name'}}})
    archive = tool.archive()
    original = deepcopy(archive)
    registry = ToolRegistry([tool], catalog_revision='catalog')
    expected = sha256(encoded({'interface': 2, 'catalog': 'catalog',
        'tools': [(tool.name, sha256(encoded(original).encode()).hexdigest())]}).encode()).hexdigest()
    assert registry.revision == expected
    archive['input_schema']['properties']['id']['type'] = 'string'
    archive['native_contract']['fields']['name']['label'] = 'Changed'
    assert tool.archive() == original
