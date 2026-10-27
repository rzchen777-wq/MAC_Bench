"""Native Twenty factories bound to captured workspace GraphQL operations.

Bindings name real fields in query documents, never generate a schema or assume
the native shape of person/company/opportunity records. Compound input values
and relation connects stay intact. Read selections are explicit projections;
their completeness for a business mechanism still needs Catalog validation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from mac_bench.environments.errors import CompileFailure


@dataclass(frozen=True)
class GraphQLOperation:
    document: str
    result_path: tuple[str, ...]
    operation_name: str | None = None

    def result(self, data):
        value = data
        for key in self.result_path:
            if not isinstance(value, dict) or key not in value:
                raise CompileFailure('twenty_operation_result_path_missing')
            value = value[key]
        return deepcopy(value)


@dataclass(frozen=True)
class TwentyRelation:
    operation: GraphQLOperation
    parent_result_path: tuple[str, ...]
    parent_identity_field: str
    parent_variable: str
    cursor_variable: str
    node_identity_field: str
    variables: dict

    def validate(self, client):
        op = self.operation
        names = (self.parent_identity_field, self.parent_variable,
                 self.cursor_variable, self.node_identity_field)
        if (any(not isinstance(name, str) or not name for name in names)
                or self.parent_variable == self.cursor_variable
                or not isinstance(self.parent_result_path, tuple) or not self.parent_result_path
                or not isinstance(op.result_path, tuple)
                or len(op.result_path) <= len(self.parent_result_path)
                or op.result_path[:len(self.parent_result_path)] != self.parent_result_path
                or not isinstance(self.variables, dict)
                or {self.parent_variable, self.cursor_variable} & self.variables.keys()):
            raise CompileFailure('twenty_relation_scope_binding_invalid')
        operation = client.operation('query', op.document, op.operation_name)
        declared = {item.variable.name.value for item in operation.variable_definitions or ()}
        if declared != {*self.variables, self.parent_variable, self.cursor_variable}:
            raise CompileFailure('twenty_relation_variables_not_bound')
        client.require_selection('query', op, [self.parent_result_path + (self.parent_identity_field,),
            op.result_path + ('pageInfo', 'hasNextPage'), op.result_path + ('pageInfo', 'endCursor')])


class TwentyEntityFactory:
    def __init__(self, client, *, create: GraphQLOperation | None, read: GraphQLOperation,
                 required_read_paths: tuple[tuple[str, ...], ...],
                 update: GraphQLOperation | None = None, identity_field: str = 'id',
                 relations: dict[str, TwentyRelation] | None = None):
        if client.endpoint != 'graphql' or not isinstance(identity_field, str) or not identity_field:
            raise CompileFailure('twenty_factory_requires_core_records')
        self.client, self.identity_field = client, identity_field
        if not isinstance(required_read_paths, tuple) or not required_read_paths:
            raise CompileFailure('twenty_explicit_read_coverage_required')
        self.required_read_paths = required_read_paths
        self.create_operation, self.read_operation, self.update_operation = create, read, update
        for binding, kind, variables in ((create, 'mutation', {'values'}), (read, 'query', {'id'}),
                                         (update, 'mutation', {'id', 'values'})):
            if binding is None:
                if kind == 'query':
                    raise CompileFailure('twenty_factory_required_operation_missing')
                continue
            operation = client.operation(kind, binding.document, binding.operation_name)
            declared = {item.variable.name.value for item in operation.variable_definitions or ()}
            if declared != variables:
                raise CompileFailure('twenty_factory_operation_variable_binding_invalid')
            if not isinstance(binding.result_path, tuple):
                raise CompileFailure('twenty_factory_result_path_invalid')
            client.require_selection(kind, binding, [binding.result_path + (identity_field,)])
        client.require_selection('query', read, [read.result_path + path
            for path in required_read_paths if isinstance(path, tuple)])
        if any(not isinstance(path, tuple) or not path for path in required_read_paths):
            raise CompileFailure('twenty_factory_read_coverage_path_invalid')
        if relations is not None and not isinstance(relations, dict):
            raise CompileFailure('twenty_relation_bindings_require_mapping')
        self.relations = deepcopy(relations or {})
        for name, relation in self.relations.items():
            if not isinstance(name, str) or not name or not isinstance(relation, TwentyRelation):
                raise CompileFailure('twenty_relation_binding_invalid')
            relation.validate(client)

    def _identity(self, record):
        identity = record.get(self.identity_field) if isinstance(record, dict) else None
        if not isinstance(identity, str) or not identity:
            raise CompileFailure('twenty_factory_native_identity_missing')
        return identity

    def read(self, identity: str) -> dict:
        op = self.read_operation
        record = op.result(self.client.query(op.document, {'id': identity}, operation_name=op.operation_name))
        if self._identity(record) != identity:
            raise CompileFailure('twenty_factory_readback_identity_mismatch')
        for path in self.required_read_paths:
            value = record
            for key in path:
                # A nullable parent is observed as null; its children are not
                # missing selections. Catalog handles null in business predicates.
                if value is None:
                    break
                if not isinstance(value, dict) or key not in value:
                    raise CompileFailure('twenty_factory_required_read_field_missing')
                value = value[key]
        return record

    def read_with_relations(self, identity: str, *, max_records: int = 100000,
                            max_pages: int = 10000):
        """Observe the record and every declared parent-scoped connection twice.

        The separate relation results are explicit query projections, not merged
        into native objects. Only registered scopes and visible nodes are covered;
        equality of two reads is not a transactional snapshot or job-drain proof.
        The record bound applies across relations per pass; page bounds apply
        separately to each connection.
        """
        if (type(max_records) is not int or max_records < 1
                or type(max_pages) is not int or max_pages < 1):
            raise CompileFailure('twenty_relation_inventory_limit_invalid')

        def capture():
            record = self.read(identity)
            relations = {}
            count = 0
            for name, binding in self.relations.items():
                nodes = read_connection(self.client, binding.operation,
                    {**binding.variables, binding.parent_variable: identity},
                    cursor_variable=binding.cursor_variable, identity_field=binding.node_identity_field,
                    max_records=max(1, max_records - count), max_pages=max_pages,
                    parent_result_path=binding.parent_result_path,
                    parent_identity_field=binding.parent_identity_field, parent_identity=identity)
                count += len(nodes)
                if count > max_records:
                    raise CompileFailure('twenty_relation_inventory_limit_exceeded')
                relations[name] = {node[binding.node_identity_field]: node for node in nodes}
            if self.read(identity) != record:
                raise CompileFailure('twenty_relation_parent_changed_during_capture')
            return {'record': record, 'relations': relations}

        first, second = capture(), capture()
        if first != second:
            raise CompileFailure('twenty_relations_changed_during_capture')
        return second

    def create(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise CompileFailure('twenty_factory_values_require_object')
        op = self.create_operation
        if op is None:
            raise CompileFailure('twenty_entity_has_no_native_create')
        result = op.result(self.client.mutate(op.document, {'values': deepcopy(values)},
                                             operation_name=op.operation_name))
        return self.read(self._identity(result))

    def update(self, identity: str, values: dict) -> dict:
        if not isinstance(values, dict):
            raise CompileFailure('twenty_factory_values_require_object')
        op = self.update_operation
        if op is None:
            raise CompileFailure('twenty_factory_update_not_bound')
        result = op.result(self.client.mutate(op.document, {'id': identity, 'values': deepcopy(values)},
                                             operation_name=op.operation_name))
        if self._identity(result) != identity:
            raise CompileFailure('twenty_factory_mutation_identity_mismatch')
        return self.read(identity)


def read_connection(client, operation: GraphQLOperation, variables: dict, *,
                    cursor_variable: str, identity_field: str = 'id',
                    max_records: int = 100000, max_pages: int = 10000,
                    parent_result_path: tuple[str, ...] | None = None,
                    parent_identity_field: str | None = None, parent_identity: str | None = None) -> list[dict]:
    """Read a native edges/node/pageInfo connection without silently truncating.

    The binding aliases native fields to edges, node, pageInfo, hasNextPage and
    endCursor when needed; aliases are checked against the captured schema.
    Nested connections in a node still need their own explicit pagination.
    """
    if type(max_records) is not int or type(max_pages) is not int or min(max_records, max_pages) < 1:
        raise ValueError('invalid pagination bounds')
    parent_binding = None
    if any(value is not None for value in (parent_result_path, parent_identity_field, parent_identity)):
        if (not isinstance(parent_result_path, tuple) or not parent_result_path
                or not isinstance(parent_identity_field, str) or not parent_identity_field
                or not isinstance(parent_identity, str) or not parent_identity):
            raise CompileFailure('twenty_connection_parent_binding_invalid')
        parent_binding = GraphQLOperation(operation.document, parent_result_path, operation.operation_name)
    definition = client.operation('query', operation.document, operation.operation_name)
    declared = {item.variable.name.value for item in definition.variable_definitions or ()}
    if cursor_variable not in declared or not isinstance(variables, dict):
        raise CompileFailure('twenty_connection_cursor_variable_not_bound')
    if cursor_variable in variables and variables[cursor_variable] is not None:
        raise CompileFailure('twenty_inventory_cannot_start_after_a_cursor')
    arguments = deepcopy(variables)
    arguments[cursor_variable] = None
    records, seen_ids, cursors = [], set(), set()
    for _ in range(max_pages):
        data = client.query(operation.document, arguments, operation_name=operation.operation_name)
        if parent_binding is not None:
            parent = parent_binding.result(data)
            if not isinstance(parent, dict) or parent.get(parent_identity_field) != parent_identity:
                raise CompileFailure('twenty_connection_parent_identity_mismatch')
        page = operation.result(data)
        if (not isinstance(page, dict) or not isinstance(page.get('edges'), list)
                or not isinstance(page.get('pageInfo'), dict)):
            raise CompileFailure('twenty_connection_envelope_invalid')
        info = page['pageInfo']
        if type(info.get('hasNextPage')) is not bool:
            raise CompileFailure('twenty_connection_page_info_invalid')
        for edge in page['edges']:
            node = edge.get('node') if isinstance(edge, dict) else None
            identity = node.get(identity_field) if isinstance(node, dict) else None
            if not isinstance(identity, str) or not identity or identity in seen_ids:
                raise CompileFailure('twenty_connection_identity_invalid_or_duplicate')
            seen_ids.add(identity)
            records.append(deepcopy(node))
            if len(records) > max_records:
                raise CompileFailure('twenty_connection_record_limit_exceeded')
        if not info['hasNextPage']:
            return records
        cursor = info.get('endCursor')
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise CompileFailure('twenty_connection_cursor_missing_or_cyclic')
        cursors.add(cursor)
        arguments[cursor_variable] = cursor
    raise CompileFailure('twenty_connection_page_limit_exceeded')
