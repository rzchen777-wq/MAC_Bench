"""World-bound, actor-attributed execution over real application connections."""
import re
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from dataclasses import dataclass
from fnmatch import fnmatchcase
from threading import RLock, Condition, local

from mac_bench.environments.catalog.entities.errors import CatalogError
from mac_bench.environments.errors import CompileFailure, NativeHTTPFailure
from mac_bench.environments.tools.contracts import (
    NativeResult,
    ToolError,
    encoded,
    validate,
)
from mac_bench.environments.tools.error_feedback import feedback
from mac_bench.environments.tools.navigation import page
from mac_bench.environments.tools.query import query_document


@dataclass(frozen=True)
class Role:
    name: str
    connections: tuple
    allowed_tools: tuple[str, ...]


class WorldTools:
    """One owner per live world. Role connections carry native credentials privately.

    Per-application locks order calls through this process, not external writers.
    A world ID must be new after reset; never reuse receipts for a rebuilt world.
    """
    def __init__(self, world_id, registry, roles, journal):
        if not isinstance(world_id, str) or not world_id:
            raise ValueError('A nonempty world instance ID is required')
        self.world_id, self.registry, self.journal = world_id, registry, journal
        self.roles = {r.name: r for r in roles}
        if len(self.roles) != len(roles):
            raise ValueError('Duplicate role names')
        for role in roles:
            keys = [(c.application, encoded(c.scope)) for c in role.connections]
            if len(set(keys)) != len(keys):
                raise ValueError('Ambiguous role application connections')
        self.lock = RLock()
        self._condition = Condition(self.lock)
        self._active = 0
        self._local = local()
        self._application_locks = {c.application: RLock() for r in roles for c in r.connections}
        self._actors = {}
        self.record_forms = None
        self.data_resources = {}
        self.register_catalog = None
        self.closed = False

    def configure_data(self, resources, registers=None):
        with self.lock:
            if self.closed or self._active or self.data_resources:
                raise ValueError('Data resources can only be configured once in an open world')
            if any(not name.startswith('public/') or not name.endswith('.json') for name in resources):
                raise ValueError('Only explicitly public JSON resources can enter the data workspace')
            self.data_resources = deepcopy(resources)
            if registers is not None:
                from .registers import RegisterCatalog
                self.register_catalog = RegisterCatalog(registers)
                for agent in self._actors.values():
                    agent.registry = self.register_catalog.extend(agent.registry)

    def configure_record_forms(self, specification, directory):
        from .record_forms import FormCatalog
        with self.lock:
            if self.closed or self._active or self.record_forms is not None:
                raise ValueError('Record forms can only be configured once in an open world')
            self.record_forms = FormCatalog(specification, directory)
            for agent in self._actors.values():
                agent.registry = self.record_forms.extend(agent.registry)

    def agent(self, actor_id, role):
        if not isinstance(actor_id, str) or not actor_id or role not in self.roles:
            raise ValueError('Unknown role or invalid actor ID')
        with self.lock:
            if self.closed:
                raise ValueError('World tool service is closed')
            if actor_id in self._actors and self._actors[actor_id].role.name != role:
                raise ValueError('An actor cannot change roles within a world instance')
            if actor_id not in self._actors:
                self._actors[actor_id] = AgentTools(self, actor_id, self.roles[role])
            return self._actors[actor_id]

    def close(self):
        """Stop agent access; application shutdown remains the environment owner's job."""
        with self._condition:
            self.closed = True
            while self._active:
                self._condition.wait()

    @contextmanager
    def activity(self):
        """Drain whole admitted operations, including their nested native steps."""
        depth = getattr(self._local, 'depth', 0)
        if not depth:
            with self._condition:
                if self.closed:
                    raise ToolError('world_closed', 'This world instance is closed.')
                self._active += 1
        self._local.depth = depth + 1
        try:
            yield
        finally:
            self._local.depth = depth
            if not depth:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    @contextmanager
    def operation(self, applications):
        required = frozenset(applications)
        parent = getattr(self._local, 'applications', None)
        if parent is not None and not required <= parent:
            raise ToolError('undeclared_application', 'Compound operation used an application outside its declared native steps.')
        with self.activity(), ExitStack() as locks:
            if parent is None:
                for app in sorted(required):
                    locks.enter_context(self._application_locks[app])
            self._local.applications = required if parent is None else parent
            try:
                yield
            finally:
                self._local.applications = parent


class AgentTools:
    def __init__(self, world, actor, role):
        self.world, self.actor, self.role = world, actor, role
        self.connections = {(c.application, encoded(c.scope)): c for c in role.connections}
        self.registry = world.registry.subset(lambda t:
            (t.application, encoded(t.scope)) in self.connections
            and any(fnmatchcase(t.name, p) for p in role.allowed_tools))
        from .structured_records import extend as structured_records
        from .appointments import extend as appointments
        self.registry = appointments(structured_records(self.registry))
        if world.record_forms is not None:
            self.registry = world.record_forms.extend(self.registry)
        if world.register_catalog is not None:
            self.registry = world.register_catalog.extend(self.registry)

    def list_apps(self):
        return self.registry.list_apps()

    def list_tools(self, app=None, query=None, cursor=None, limit=20, entity=None, read_only=None):
        return self.registry.list_tools(app, query, cursor, limit, entity, read_only)

    def describe_tool(self, tool_name, section='usage', path='', cursor=None, limit=20):
        return self.registry.describe_tool(tool_name, section, path, cursor, limit)

    def read_result(self, result_ref=None, path='/data', cursor=None, limit=20, *, request_id=None):
        with self.world.activity():
            if (result_ref is None) == (request_id is None):
                raise ToolError('invalid_result_locator', 'Supply exactly one of result_ref and request_id.')
            if request_id is not None:
                result_ref = self.world.journal.find_request(
                    self.world.world_id, self.actor, self.role.name, request_id)
            result, timestamp = self.world.journal.receipt(
                self.world.world_id, self.actor, self.role.name, result_ref)
            return {'result_ref': result_ref, 'status': result['status'],
                    'observed_at': timestamp, 'source': 'saved_receipt_not_live_state',
                    **page(result, identity=[self.world.world_id, self.actor, result_ref],
                           path=path, cursor=cursor, limit=limit, compact=True)}

    def _deliver(self, result, replayed, tool=None):
        response = {**result, 'replayed': replayed, 'result_ref': result['call_id'], 'data_complete': True}
        if tool is not None and result['status'] == 'succeeded':
            from .presentation import record_overview
            try:
                overview = record_overview(tool, result.get('data'))
            except ToolError as exc:
                if exc.code != 'response_schema_mismatch':
                    raise
                # Presentation cannot undo a native success or discard its
                # evidence. Deliver the original result and identify the mismatch.
                overview = None
                response['presentation_error'] = {'code': exc.code, 'message': exc.message}
            if overview is not None and len(encoded(overview).encode()) <= 8000:
                _, timestamp = self.world.journal.receipt(
                    self.world.world_id, self.actor, self.role.name, result['call_id'])
                response.update(data=overview, data_complete=False, data_view='document_overview',
                    observed_at=timestamp,
                    instruction='This overview belongs to the saved result at result_ref. '
                    'read_result(result_ref, path="/data") retrieves all fields; '
                    'path="/data/entries" retrieves line items. observed_at is the receipt completion time. '
                    'Use a fresh application read for current state.')
        if len(encoded(response).encode()) > 14000:
            response = {k: response[k] for k in ('call_id', 'status', 'replayed', 'result_ref', 'presentation_error') if k in response}
            # Paging the data must not erase the reason a partial/unknown call
            # stopped. The full original receipt remains available as well.
            response['error'] = result.get('error')
            data = result.get('data')
            path = '/data'
            if isinstance(data, dict):
                # Present ordinary query rows and native REST collections directly.
                for key in ('rows', 'results', 'edges'):
                    if isinstance(data.get(key), list):
                        path += '/' + key
                        response['row_count'] = len(data[key])
                        if data[key] and isinstance(data[key][0], dict):
                            response['columns'] = list(data[key][0])[:32]
                        break
            if result.get('pagination') is not None:
                response['pagination'] = result['pagination']
            response.update(data=None, data_complete=False,
                            result_page=self.read_result(result['call_id'], path=path),
                            instruction='Continue read_result with this result_ref, result_page.path and next_cursor. '
                            'This reads the saved receipt without another app call. Native collection pagination, '
                            'when present, is separate from receipt pagination.')
        return response

    def query_result(self, result_ref, **options):
        with self.world.activity():
            result, timestamp = self.world.journal.receipt(
                self.world.world_id, self.actor, self.role.name, result_ref)
            options.setdefault('path', '/data')
            return {'result_ref': result_ref, 'observed_at': timestamp,
                    'source': 'saved_receipt_not_live_state',
                    'native_pagination': result.get('pagination'),
                    **query_document(result, identity=[self.world.world_id, self.actor, result_ref], **options)}

    def compute(self, **arguments):
        from .data_workspace import compute, COMPUTE_SCHEMA
        validate(COMPUTE_SCHEMA, arguments)
        with self.world.activity():
            call_id, cached = self.world.journal.begin(self.world.world_id, self.actor, self.role.name,
                arguments['request_id'], 'data.compute', arguments)
            if cached is not None:
                return self._deliver(cached, True)
            try:
                result = {'call_id': call_id, 'status': 'succeeded', 'data': compute(self, arguments), 'error': None}
            except ToolError as exc:
                result = {'call_id': call_id, 'status': 'rejected', 'data': None,
                    'error': {'code': exc.code, 'message': exc.message}}
            self.world.journal.finish(call_id, result)
            return deepcopy(self._deliver(result, False))

    def call_tool(self, tool_name, arguments=None, request_id=None, arguments_ref=None):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ToolError('invalid_request_id', 'Supply a stable request_id of 1 to 128 characters for this logical call.')
        tool = self.registry.get(tool_name)
        if (arguments is None) == (arguments_ref is None):
            raise ToolError('invalid_arguments', 'Supply exactly one of arguments and arguments_ref.')
        if arguments_ref is not None:
            from .data_workspace import result_value, REFERENCE
            validate(REFERENCE, arguments_ref)
            with self.world.activity():
                arguments = result_value(self, arguments_ref)
            if not isinstance(arguments, dict):
                raise ToolError('invalid_arguments_reference', 'arguments_ref must select an object matching the operation input schema.')
        if len(encoded(arguments).encode()) > 1_000_000:
            raise ToolError('arguments_too_large', 'Arguments exceed the 1 MB request budget.')
        validate(tool.input_schema, arguments)
        from .compound import CompoundOperation
        applications = (tool.handler.applications | {tool.application}
                        if isinstance(tool.handler, CompoundOperation) else {tool.application})
        with self.world.operation(applications):
            call_id, cached = self.world.journal.begin(self.world.world_id, self.actor, self.role.name,
                                                     request_id, tool_name, arguments)
            if cached is not None:
                return self._deliver(cached, True, tool)
            try:
                if isinstance(tool.handler, CompoundOperation):
                    result = {'call_id': call_id, **tool.handler.execute_agent(self, deepcopy(arguments), call_id)}
                else:
                    connection = self.connections[tool.application, encoded(tool.scope)]
                    native = tool.handler(connection, deepcopy(arguments))
                    if not isinstance(native, NativeResult) or native.status not in {'succeeded', 'accepted'}:
                        raise ValueError('Invalid native result')
                    result = {'call_id': call_id, 'status': native.status, 'data': native.data,
                              'pagination': native.pagination, 'error': None}
                encoded(result)
            except ToolError as exc:
                result = {'call_id': call_id, 'status': 'rejected' if exc.outcome == 'not_started' else 'unknown',
                          'data': None, 'error': {'code': exc.code, 'message': exc.message}}
                if exc.code == 'invalid_projection':
                    from .cards import projections
                    result['error']['projection_help'] = projections(tool, requested=arguments.get('fields'))
            except Exception as exc:
                native_code = (exc.reason if isinstance(exc, CompileFailure) else
                               exc.code if isinstance(exc, CatalogError) else None)
                if native_code is not None and not re.fullmatch(r'[A-Za-z0-9_:.-]{1,200}', native_code):
                    native_code = None
                # The write may have committed before a timeout/decode/readback failure.
                # Do not expose transport exceptions, headers or server tracebacks.
                result = {'call_id': call_id, 'status': 'error' if tool.read_only else 'unknown', 'data': None,
                          'error': {'code': 'native_call_failed', 'error_type': type(exc).__name__, 'native_code': native_code,
                                    'message': 'Native operation did not produce a complete success receipt. Read current native state; writes are not retried.'}}
                if detail := feedback(exc, tool):
                    result['error'] = detail
                if isinstance(exc, NativeHTTPFailure) and exc.outcome == 'not_started':
                    result['status'] = 'rejected'
            self.world.journal.finish(call_id, result)
            return deepcopy(self._deliver(result, False, tool))
