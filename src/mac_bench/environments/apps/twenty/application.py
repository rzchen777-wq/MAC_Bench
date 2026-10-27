"""Twenty workspace-specific GraphQL contracts and native operations.

Core records and metadata have separate captured schemas. Introspection retains
compound fields, nullability, custom scalars, relations and input objects. It
does not prove permissions, custom scalar semantics or business side effects.
"""
from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256

from graphql import (
    GraphQLError,
    assert_valid_schema,
    build_client_schema,
    get_introspection_query,
    get_named_type,
    get_operation_ast,
    is_list_type,
    is_non_null_type,
    parse,
    validate,
)
from graphql.execution.values import get_variable_values

from mac_bench.environments.errors import CompileFailure, NativeGraphQLFailure
from mac_bench.environments.runtime.infrastructure.deployment import (
    DeploymentEvidence,
)


def _digest(document):
    return sha256(json.dumps(document, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False).encode()).hexdigest()


class TwentyGraphQL:
    def __init__(self, transport, endpoint: str, schema: dict):
        if endpoint not in {'graphql', 'metadata'}:
            raise CompileFailure('twenty_graphql_endpoint_invalid')
        self.transport = transport
        self.endpoint = endpoint
        self.document = deepcopy(schema)
        try:
            self.schema = build_client_schema(self.document)
            assert_valid_schema(self.schema)
        except (TypeError, ValueError):
            raise CompileFailure('twenty_introspection_schema_invalid') from None

    @classmethod
    def capture(cls, transport, endpoint: str):
        if endpoint not in {'graphql', 'metadata'}:
            raise CompileFailure('twenty_graphql_endpoint_invalid')
        response = transport.request('POST', endpoint, {
            'query': get_introspection_query(), 'operationName': 'IntrospectionQuery'})
        data = cls._data(response)
        return cls(transport, endpoint, data)

    def query(self, document: str, variables: dict, *, operation_name: str | None = None):
        return self._execute('query', document, variables, operation_name)

    def mutate(self, document: str, variables: dict, *, operation_name: str | None = None):
        """No retries. An error may follow partial server-side mutation effects.

        Caller must journal intent and independently query affected objects. A
        mutation response by itself is not a materialization/readback proof.
        """
        return self._execute('mutation', document, variables, operation_name)

    def operation(self, kind, document, operation_name=None):
        """Validate a binding without issuing any native request."""
        try:
            ast = parse(document)
        except (GraphQLError, TypeError):
            raise CompileFailure('twenty_graphql_document_invalid') from None
        if validate(self.schema, ast):
            raise CompileFailure('twenty_graphql_document_not_in_captured_schema')
        operation = get_operation_ast(ast, operation_name)
        if operation is None or operation.operation.value != kind:
            raise CompileFailure('twenty_graphql_operation_kind_or_name_mismatch')
        return operation

    def _execute(self, kind, document, variables, operation_name):
        if not isinstance(variables, dict):
            raise CompileFailure('twenty_graphql_variables_require_object')
        operation = self.operation(kind, document, operation_name)
        coerced = get_variable_values(self.schema, operation.variable_definitions or (), variables)
        if isinstance(coerced, list):
            raise CompileFailure('twenty_graphql_variable_type_mismatch')
        body = {'query': document, 'variables': deepcopy(variables)}
        if operation_name is not None:
            body['operationName'] = operation_name
        return self._data(self.transport.request('POST', self.endpoint, body))

    def require_selection(self, kind, binding, paths):
        """Prove unconditional response paths exist before issuing a mutation.

        Paths use response aliases. Fragments on the declared type or one of
        its interfaces are accepted; subtype-only fragments cannot establish
        coverage for every possible result. Nested lists require their own
        explicit connection/pagination binding, not a scalar record path.
        """
        operation = self.operation(kind, binding.document, binding.operation_name)
        document = parse(binding.document)
        fragments = {item.name.value: item for item in document.definitions
                     if item.kind == 'fragment_definition'}
        root = self.schema.query_type if kind == 'query' else self.schema.mutation_type

        def unconditional(node):
            # Custom directives may also alter selection semantics; no guessing.
            return not node.directives

        def applicable(condition, parent):
            return condition is None or condition.name.value in {
                parent.name, *(item.name for item in getattr(parent, 'interfaces', ())) }

        def selected(selection, parent, alias, visiting=frozenset()):
            for node in selection.selections:
                if not unconditional(node):
                    continue
                if node.kind == 'field':
                    if (node.alias or node.name).value == alias:
                        yield node
                elif node.kind == 'inline_fragment' and applicable(node.type_condition, parent):
                    yield from selected(node.selection_set, parent, alias, visiting)
                elif node.kind == 'fragment_spread':
                    name = node.name.value
                    fragment = fragments[name]
                    if (name not in visiting and unconditional(fragment)
                            and applicable(fragment.type_condition, parent)):
                        yield from selected(fragment.selection_set, parent, alias, visiting | {name})

        def covered(selections, parent, path):
            nodes = [node for selection in selections
                     for node in selected(selection, parent, path[0])]
            if not nodes:
                return False
            field = getattr(parent, 'fields', {}).get(nodes[0].name.value)
            if field is None:
                return False
            field_type = field.type.of_type if is_non_null_type(field.type) else field.type
            if len(path) == 1:
                # Selecting the list field guarantees the whole value is
                # observed; it does not guarantee membership or element paths.
                return True
            if is_list_type(field_type):
                return False
            nested = [node.selection_set for node in nodes if node.selection_set is not None]
            return bool(nested) and covered(nested, get_named_type(field.type), path[1:])

        for path in paths:
            if (not isinstance(path, tuple) or not path
                    or any(not isinstance(key, str) or not key for key in path)
                    or not covered([operation.selection_set], root, path)):
                raise CompileFailure('twenty_required_read_selection_not_guaranteed')

    @staticmethod
    def _data(response):
        if not isinstance(response, dict) or response.get('errors'):
            # GraphQL errors may include query values and native records. Do not
            # leak those into generic compiler diagnostics or model feedback.
            raise NativeGraphQLFailure(response.get('errors', []) if isinstance(response, dict) else [])
        if not isinstance(response.get('data'), dict):
            raise CompileFailure('twenty_graphql_data_missing')
        return deepcopy(response['data'])


class TwentyApplication:
    def __init__(self, transport, *, deployment: DeploymentEvidence, core: dict, metadata: dict,
                 native_bindings: dict | None = None):
        self.deployment = DeploymentEvidence.from_dict(deployment.to_dict())
        self.core = TwentyGraphQL(transport, 'graphql', core)
        self.metadata = TwentyGraphQL(transport, 'metadata', metadata)
        self.native_bindings = deepcopy({} if native_bindings is None else native_bindings)
        self.documents = self._assemble_native_bindings()

    def _assemble_native_bindings(self):
        from mac_bench.environments.apps.twenty.factory import (
            GraphQLOperation,
            TwentyRelation,
        )

        def mapping(value):
            if not isinstance(value, dict) or any(not isinstance(k, str) or not k for k in value):
                raise CompileFailure('twenty_native_binding_mapping_invalid')
            return value

        def path(value):
            if not isinstance(value, list) or not value or any(not isinstance(k, str) or not k for k in value):
                raise CompileFailure('twenty_native_binding_path_invalid')
            return tuple(value)

        def operation(value):
            if value is None:
                return None
            if (not isinstance(value, dict) or set(value) != {'document', 'result_path', 'operation_name'}
                    or not isinstance(value['document'], str) or not value['document'].strip()
                    or value['operation_name'] is not None and (
                        not isinstance(value['operation_name'], str) or not value['operation_name'])):
                raise CompileFailure('twenty_native_operation_binding_invalid')
            return GraphQLOperation(value['document'], path(value['result_path']), value['operation_name'])

        documents = {}
        for name, spec in mapping(self.native_bindings).items():
            if not isinstance(spec, dict) or set(spec) != {
                    'read', 'create', 'update', 'identity_field', 'required_read_paths', 'relations'}:
                raise CompileFailure('twenty_native_document_binding_invalid')
            if not isinstance(spec['required_read_paths'], list) or not spec['required_read_paths']:
                raise CompileFailure('twenty_native_read_coverage_required')
            relations = {}
            for label, relation in mapping(spec['relations']).items():
                if not isinstance(relation, dict) or set(relation) != {
                        'operation', 'parent_result_path', 'parent_identity_field', 'parent_variable',
                        'cursor_variable', 'node_identity_field', 'variables'}:
                    raise CompileFailure('twenty_native_relation_binding_invalid')
                relation_op = operation(relation['operation'])
                if relation_op is None:
                    raise CompileFailure('twenty_native_relation_operation_required')
                relations[label] = TwentyRelation(operation=relation_op,
                    parent_result_path=path(relation['parent_result_path']),
                    parent_identity_field=relation['parent_identity_field'],
                    parent_variable=relation['parent_variable'], cursor_variable=relation['cursor_variable'],
                    node_identity_field=relation['node_identity_field'], variables=mapping(relation['variables']))
            documents[name] = self.factory(read=operation(spec['read']), create=operation(spec['create']),
                update=operation(spec['update']), identity_field=spec['identity_field'],
                required_read_paths=tuple(path(value) for value in spec['required_read_paths']),
                relations=relations)
        return documents

    def factory(self, *, create, read, required_read_paths, update=None, identity_field='id', relations=None):
        from mac_bench.environments.apps.twenty.factory import (
            TwentyEntityFactory,
        )
        return TwentyEntityFactory(self.core, create=create, read=read, update=update,
                                   identity_field=identity_field, required_read_paths=required_read_paths,
                                   relations=relations)

    @classmethod
    def capture(cls, transport, *, deployment: DeploymentEvidence, native_bindings: dict | None = None):
        core = TwentyGraphQL.capture(transport, 'graphql')
        metadata = TwentyGraphQL.capture(transport, 'metadata')
        return cls(transport, deployment=deployment, core=core.document, metadata=metadata.document,
                   native_bindings=native_bindings)

    def describe(self):
        return {'site': self.core.transport.base,
                'native_bindings': deepcopy(self.native_bindings),
                'deployment': {key: value for key, value in self.deployment.to_dict().items() if key != 'sources'},
                'source_files': {key: {'path': item['path'], 'sha256': item['sha256']}
                                 for key, item in self.deployment.sources.items()},
                'schemas': {client.endpoint: {'digest': _digest(client.document),
                                              'introspection': deepcopy(client.document)}
                            for client in (self.core, self.metadata)}}

    def archive(self):
        return {'version': 2, 'site': self.core.transport.base, 'deployment': self.deployment.to_dict(),
                'schemas': self.describe()['schemas'], 'native_bindings': deepcopy(self.native_bindings)}

    @classmethod
    def from_archive(cls, transport, archive):
        if (not isinstance(archive, dict) or set(archive) != {'version', 'site', 'deployment', 'schemas', 'native_bindings'}
                or archive['version'] != 2 or archive['site'] != transport.base
                or not isinstance(archive['schemas'], dict)
                or set(archive['schemas']) != {'graphql', 'metadata'}):
            raise CompileFailure('twenty_archive_invalid_or_site_mismatch')
        for schema in archive['schemas'].values():
            if (not isinstance(schema, dict) or set(schema) != {'digest', 'introspection'}
                    or schema['digest'] != _digest(schema['introspection'])):
                raise CompileFailure('twenty_schema_archive_invalid')
        return cls(transport, deployment=DeploymentEvidence.from_dict(archive['deployment']),
                   core=archive['schemas']['graphql']['introspection'],
                   metadata=archive['schemas']['metadata']['introspection'],
                   native_bindings=archive['native_bindings'])
