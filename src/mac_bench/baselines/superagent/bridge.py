"""Concurrent public-world transport used by the native DSH capabilities."""
import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from mac_bench.evaluation.episodes.budget import BudgetExceeded
from mac_bench.evaluation.episodes.identity import call_scope
from mac_bench.evaluation.tasks.execution import TaskExecutionError

FILE_TOOLS = frozenset({'read', 'read_image', 'write', 'edit', 'glob', 'grep', 'bash'})


class WorldBridge:
    def __init__(self, context, execution):
        self.context, self.execution = context, execution
        self.token = secrets.token_urlsafe(32)
        self.stop_error = None
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                bridge._handle(self)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = False
        self.thread = Thread(target=self.server.serve_forever, name='superagent-world')
        self.url = f'http://127.0.0.1:{self.server.server_port}/world'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        # Join accepted calls before the caller freezes the task's final state.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def dispatch(self, operation, args, identity):
        with call_scope(**identity):
            if operation == 'tool.start':
                if identity.get('tool') not in FILE_TOOLS:
                    raise ValueError('Unknown execution tool')
                self.context.budget.admit_tool()
                self.context.event({'kind': 'task_tool_start', **identity, 'arguments': args})
                return None
            if operation == 'tool.end':
                self.context.event({'kind': 'task_tool_end', **identity, 'result': args})
                return None
            if operation.startswith(('fs.', 'process.')):
                if operation == 'process.spawn':
                    return self.execution.call(operation, args, public_tools=bool(
                        identity.get('agent_id') and identity['agent_id'] != 'superagent-router'))
                return self.execution.call(operation, args)
            if operation == 'tools.call':
                if identity.get('agent_id') == 'superagent-router':
                    raise TaskExecutionError('TOOLS_UNAVAILABLE', 'Application tools belong to workers; delegate this operation.')
                return self.context.environment.call(args['name'], args['arguments'])
            raise ValueError('Unknown world operation: ' + operation)

    def _handle(self, handler):
        if (handler.path != '/world'
                or not hmac.compare_digest(handler.headers.get('authorization', ''), 'Bearer ' + self.token)):
            return self._respond(handler, 403, {'error': {'code': 'WORLD_ACCESS', 'message': 'Invalid world connection'}})
        try:
            body = json.loads(handler.rfile.read(int(handler.headers.get('content-length', '0'))))
            result = self.dispatch(body['operation'], body['arguments'], body['identity'])
            payload = {'result': result}
        except BudgetExceeded as exc:
            self.stop_error = exc
            payload = {'error': {'code': 'EPISODE_BUDGET', 'message': str(exc)}}
        except TaskExecutionError as exc:
            payload = {'error': {'code': exc.code, 'message': str(exc)}}
        except (ValueError, TypeError, KeyError) as exc:
            payload = {'error': {'code': 'WORLD_ARGUMENTS', 'message': str(exc)}}
        except Exception as exc:
            self.context.event({'kind': 'world_error', 'error_type': type(exc).__name__})
            payload = {'error': {'code': 'WORLD_EXECUTION', 'message': f'World operation failed ({type(exc).__name__})'}}
        self._respond(handler, 200, payload)

    @staticmethod
    def _respond(handler, status, value):
        data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        handler.send_response(status)
        handler.send_header('content-type', 'application/json')
        handler.send_header('content-length', str(len(data)))
        handler.send_header('connection', 'close')
        handler.end_headers()
        handler.close_connection = True
        try:
            handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The accepted operation has finished even if its caller cancelled.
