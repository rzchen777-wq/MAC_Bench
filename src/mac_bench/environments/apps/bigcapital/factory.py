"""Full native BigCapital document factories and explicit lifecycle bindings.

Bindings identify operations and response envelopes in the installed schema.
Accounting documents and their line/settlement arrays are never flattened.
Readback of one root document does not prove ledger or allocation consistency.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from mac_bench.environments.errors import CompileFailure


def _value(document, pointer):
    if pointer == '':
        return document
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise CompileFailure('bigcapital_result_requires_JSON_pointer')
    node = document
    for token in pointer[1:].split('/'):
        key = token.replace('~1', '/').replace('~0', '~')
        if not isinstance(node, dict) or key not in node:
            raise CompileFailure('bigcapital_response_envelope_missing')
        node = node[key]
    return node


@dataclass(frozen=True)
class BigCapitalOperation:
    path: str
    method: str
    record_pointer: str
    identity_pointer: str
    identity_parameter: str | None = None


@dataclass(frozen=True)
class BigCapitalListing:
    """Explicit bindings to the installed page-number listing DTO."""
    path: str
    records_pointer: str
    identity_pointer: str
    page_parameter: str
    page_pointer: str
    page_size_pointer: str
    total_pointer: str


class PendingAccountingOperation(CompileFailure):
    """The request was accepted; do not repeat it or claim a completed record."""
    def __init__(self, response):
        super().__init__('bigcapital_async_completion_binding_required')
        # Kept for the execution journal, not formatted into model diagnostics.
        self.response = deepcopy(response.body)
        self.http_status = response.status


class AccountingReadbackRequired(CompileFailure):
    """A mutation returned successfully but identity/readback is unresolved."""
    def __init__(self, binding, response, identity):
        super().__init__('bigcapital_mutation_readback_required_do_not_retry')
        self.operation = binding
        self.response = deepcopy(response.body)
        self.http_status = response.status
        self.identity = identity


class BigCapitalDocumentFactory:
    def __init__(self, application, *, read: BigCapitalOperation,
                 create: BigCapitalOperation | None = None,
                 update: BigCapitalOperation | None = None,
                 actions: dict[str, BigCapitalOperation] | None = None,
                 path_parameters: dict | None = None, listing: BigCapitalListing | None = None):
        self.application = application
        self.create_operation, self.read_operation, self.update_operation = create, read, update
        self.actions = dict(actions or {})
        self.path_parameters = deepcopy(path_parameters or {})
        self.listing = listing
        for binding, methods in ((create, {'post'}), (read, {'get'}), (update, {'put', 'patch'})):
            if binding is not None:
                self._check(binding, methods)
        if read is None or not read.identity_parameter:
            raise CompileFailure('bigcapital_factory_identity_read_required')
        if (create is not None and create.identity_parameter
                or update is not None and not update.identity_parameter):
            raise CompileFailure('bigcapital_factory_identity_binding_invalid')
        if listing is not None:
            self.application.require_response_contracts(listing.path, 'get')
            if not isinstance(listing.page_parameter, str) or not listing.page_parameter:
                raise CompileFailure('bigcapital_listing_page_parameter_required')
            request = self.application.request_contract(listing.path, 'get')
            if not any(p['in'] == 'query' and p['name'] == listing.page_parameter
                       for p in request['parameters']):
                raise CompileFailure('bigcapital_listing_page_parameter_not_in_native_contract')
            for pointer in (listing.records_pointer, listing.identity_pointer, listing.page_pointer,
                            listing.page_size_pointer, listing.total_pointer):
                if not isinstance(pointer, str) or pointer and not pointer.startswith('/'):
                    raise CompileFailure('bigcapital_listing_pointer_invalid')
        for name, binding in self.actions.items():
            if not isinstance(name, str) or not name:
                raise CompileFailure('bigcapital_lifecycle_binding_name_invalid')
            self._check(binding, {'post', 'put', 'patch', 'delete'})
            if not binding.identity_parameter:
                raise CompileFailure('bigcapital_lifecycle_target_identity_required')

    def _check(self, binding, methods):
        if binding.method.lower() not in methods:
            raise CompileFailure('bigcapital_factory_operation_role_mismatch')
        item = self.application.document['paths'].get(binding.path, {})
        if binding.method.lower() not in item:
            raise CompileFailure('bigcapital_factory_operation_not_in_schema')
        self.application.require_response_contracts(binding.path, binding.method.lower())
        request = self.application.request_contract(binding.path, binding.method)
        if binding.identity_parameter and not any(
                p['in'] == 'path' and p['name'] == binding.identity_parameter for p in request['parameters']):
            raise CompileFailure('bigcapital_identity_parameter_not_in_native_contract')
        for pointer in (binding.record_pointer, binding.identity_pointer):
            if not isinstance(pointer, str) or pointer and not pointer.startswith('/'):
                raise CompileFailure('bigcapital_factory_pointer_invalid')

    def _invoke(self, binding, *, identity=None, values=None):
        parameters = {key: value for key, value in self.path_parameters.items()
                      if '{' + key + '}' in binding.path}
        if binding.identity_parameter:
            if identity is None:
                raise CompileFailure('bigcapital_operation_identity_missing')
            if binding.identity_parameter in parameters:
                raise CompileFailure('bigcapital_parent_scope_overrides_target_identity')
            parameters[binding.identity_parameter] = identity
        response = self.application.invoke(binding.path, binding.method,
                                            path_parameters=parameters, body=deepcopy(values))
        if response.status == 202:
            raise PendingAccountingOperation(response)
        return response

    @staticmethod
    def _identity(binding, record):
        identity = _value(record, binding.identity_pointer)
        if (isinstance(identity, bool) or not isinstance(identity, (int, str))
                or isinstance(identity, str) and not identity):
            raise CompileFailure('bigcapital_native_identity_invalid')
        return identity

    def read(self, identity):
        op = self.read_operation
        record = _value(self._invoke(op, identity=identity).body, op.record_pointer)
        if not isinstance(record, dict) or self._identity(op, record) != identity:
            raise CompileFailure('bigcapital_readback_identity_mismatch')
        return deepcopy(record)

    def create(self, values: dict):
        if self.create_operation is None:
            raise CompileFailure('bigcapital_entity_has_no_native_create')
        if not isinstance(values, dict):
            raise CompileFailure('bigcapital_factory_values_require_object')
        op = self.create_operation
        response = self._invoke(op, values=values)
        identity = None
        try:
            record = _value(response.body, op.record_pointer)
            identity = self._identity(op, record)
            return self.read(identity)
        except Exception:
            raise AccountingReadbackRequired(op, response, identity) from None

    def inventory(self, query: dict | None = None, *, max_records: int = 100000,
                  max_pages: int = 10000):
        """Read a bounded, explicit scope twice using native list and detail APIs.

        Pagination uses returned totals/page size, never a guessed empty-page
        stopping rule. Filtered disappearance is not evidence of deletion. Two
        equal passes do not establish transaction isolation, job completion or
        visibility of records outside this principal's permissions.
        """
        binding = self.listing
        if binding is None:
            raise CompileFailure('bigcapital_listing_not_bound')
        if (type(max_records) is not int or max_records < 1
                or type(max_pages) is not int or max_pages < 1):
            raise CompileFailure('bigcapital_inventory_limits_invalid')
        query = deepcopy({} if query is None else query)
        if not isinstance(query, dict) or binding.page_parameter in query:
            raise CompileFailure('bigcapital_inventory_requires_complete_page_scope')
        parameters = {key: value for key, value in self.path_parameters.items()
                      if '{' + key + '}' in binding.path}

        def capture():
            ids = []
            seen = set()
            expected_total = expected_size = None
            for page in range(1, max_pages + 1):
                response = self.application.invoke(binding.path, 'get', path_parameters=parameters,
                    query={**query, binding.page_parameter: page})
                if response.status == 202:
                    raise PendingAccountingOperation(response)
                records = _value(response.body, binding.records_pointer)
                current = _value(response.body, binding.page_pointer)
                size = _value(response.body, binding.page_size_pointer)
                total = _value(response.body, binding.total_pointer)
                if (not isinstance(records, list) or type(current) is not int or current != page
                        or type(size) is not int or size < 1 or type(total) is not int or total < 0):
                    raise CompileFailure('bigcapital_pagination_metadata_invalid')
                if total > max_records or max(1, (total + size - 1) // size) > max_pages:
                    raise CompileFailure('bigcapital_inventory_limit_exceeded')
                if expected_total is None:
                    expected_total, expected_size = total, size
                elif (total, size) != (expected_total, expected_size):
                    raise CompileFailure('bigcapital_listing_changed_during_capture')
                if len(records) != min(size, max(0, total - (page - 1) * size)):
                    raise CompileFailure('bigcapital_listing_incomplete_page')
                for record in records:
                    if not isinstance(record, dict):
                        raise CompileFailure('bigcapital_listing_record_invalid')
                    identity = self._identity(binding, record)
                    if identity in seen:
                        raise CompileFailure('bigcapital_listing_duplicate_identity')
                    seen.add(identity)
                    ids.append(identity)
                if len(ids) == total:
                    return {identity: self.read(identity) for identity in ids}
            raise CompileFailure('bigcapital_inventory_page_limit_exceeded')

        first = capture()
        second = capture()
        if first != second:
            raise CompileFailure('bigcapital_inventory_changed_during_capture')
        return second

    def update(self, identity, values: dict):
        if self.update_operation is None or not isinstance(values, dict):
            raise CompileFailure('bigcapital_factory_update_not_bound_or_invalid')
        response = self._invoke(self.update_operation, identity=identity, values=values)
        try:
            return self.read(identity)
        except Exception:
            raise AccountingReadbackRequired(self.update_operation, response, identity) from None

    def act(self, action: str, identity, values=None):
        """Return raw action result plus independent read of the target.

        Endpoints that create allocations/refunds return their own identities in
        response; these are retained, never substituted with the target's ID.
        Additional effect records must be read by the lifecycle's own binding.
        Delete actions intentionally have no root readback; they need inventory.
        """
        op = self.actions.get(action)
        if op is None:
            raise CompileFailure('bigcapital_native_lifecycle_not_bound')
        response = self._invoke(op, identity=identity, values=values)
        try:
            target = None if op.method.lower() == 'delete' else self.read(identity)
        except Exception:
            raise AccountingReadbackRequired(op, response, identity) from None
        return {'http_status': response.status, 'action_response': deepcopy(response.body),
                'target': target}
