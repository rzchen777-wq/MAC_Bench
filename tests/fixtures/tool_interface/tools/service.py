"""World-bound, actor-attributed execution over real application connections."""
import re
from copy import deepcopy
from dataclasses import dataclass
from fnmatch import fnmatchcase
from threading import RLock

from mac_bench.construction.realize.catalog.entities.errors import CatalogError
from mac_bench.construction.realize.errors import CompileFailure
from mac_bench.construction.realize.tools.contracts import (
    NativeResult,
    ToolError,
    encoded,
    validate,
)
from mac_bench.construction.realize.tools.error_feedback import feedback
from mac_bench.construction.realize.tools.navigation import page
from mac_bench.construction.realize.tools.query import query_document


@dataclass(frozen=True)
class Role:
    name: str
    connections: tuple
    allowed_tools: tuple[str, ...]


class WorldTools:
    """One owner per live world. Role connections carry native credentials privately.

    This lock orders calls through this process, not external/native writers.
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
        self._actors = {}
        self.closed = False

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
        with self.lock:
            self.closed = True


class AgentTools:
    def __init__(self, world, actor, role):
        self.world, self.actor, self.role = world, actor, role
        self.connections = {(c.application, encoded(c.scope)): c for c in role.connections}
        self.registry = world.registry.subset(lambda t:
            (t.application, encoded(t.scope)) in self.connections
            and any(fnmatchcase(t.name, p) for p in role.allowed_tools))

    def list_apps(self):
        return self.registry.list_apps()

    def list_tools(self, app=None, query=None, cursor=None, limit=20, entity=None, read_only=None):
        return self.registry.list_tools(app, query, cursor, limit, entity, read_only)

    def describe_tool(self, tool_name, section='usage', path='', cursor=None, limit=20):
        return self.registry.describe_tool(tool_name, section, path, cursor, limit)

    def read_result(self, result_ref, path='/data', cursor=None, limit=20):
        with self.world.lock:
            if self.world.closed:
                raise ToolError('world_closed', 'This world instance is closed.')
            result, timestamp = self.world.journal.receipt(
                self.world.world_id, self.actor, self.role.name, result_ref)
            return {'result_ref': result_ref, 'observed_at': timestamp, 'source': 'saved_receipt_not_live_state',
                    **page(result, identity=[self.world.world_id, self.actor, result_ref],
                           path=path, cursor=cursor, limit=limit)}

    def _deliver(self, result, replayed):
        response = {**result, 'replayed': replayed, 'result_ref': result['call_id'], 'data_complete': True}
        if len(encoded(response).encode()) > 14000:
            response = {k: response[k] for k in ('call_id', 'status', 'replayed', 'result_ref')}
            response.update(data=None, data_complete=False,
                            result_page=self.read_result(result['call_id'], path=''),
                            instruction='Use read_result with result_ref and returned paths to retrieve the complete saved receipt; no app call is repeated.')
        return response

    def query_result(self, result_ref, **options):
        with self.world.lock:
            if self.world.closed:
                raise ToolError('world_closed', 'This world instance is closed.')
            result, timestamp = self.world.journal.receipt(
                self.world.world_id, self.actor, self.role.name, result_ref)
            options.setdefault('path', '/data')
            return {'result_ref': result_ref, 'observed_at': timestamp,
                    'source': 'saved_receipt_not_live_state',
                    'native_pagination': result.get('pagination'),
                    **query_document(result, identity=[self.world.world_id, self.actor, result_ref], **options)}

    def call_tool(self, tool_name, arguments, request_id):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ToolError('invalid_request_id', 'Supply a stable request_id of 1 to 128 characters for this logical call.')
        tool = self.registry.get(tool_name)
        if len(encoded(arguments).encode()) > 1_000_000:
            raise ToolError('arguments_too_large', 'Arguments exceed the 1 MB request budget.')
        validate(tool.input_schema, arguments)
        with self.world.lock:
            if self.world.closed:
                raise ToolError('world_closed', 'This world instance is closed.')
            call_id, cached = self.world.journal.begin(self.world.world_id, self.actor, self.role.name,
                                                     request_id, tool_name, arguments)
            if cached is not None:
                return self._deliver(cached, True)
            try:
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
            self.world.journal.finish(call_id, result)
            return deepcopy(self._deliver(result, False))
