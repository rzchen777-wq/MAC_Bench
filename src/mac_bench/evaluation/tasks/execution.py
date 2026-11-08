"""Persistent file and process access to the task's public execution world."""
import json
import base64
import re
import shlex
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue, Empty
from threading import Lock, Thread
from time import monotonic
from uuid import uuid4

from ..episodes.budget import BudgetExceeded
from ..episodes.identity import call_identity, call_scope
from .tools import AgentAccessClosed

_WORKER = (Path(__file__).with_name('command_resources.py').read_text() + '\n'
           + Path(__file__).with_name('execution_worker.py').read_text())


class TaskExecutionError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class TaskExecution:
    def __init__(self, backend, sandbox, workspace, *, output_archive=None,
                 public_environment=None, tool_definitions=()):
        self.workspace = workspace
        self._backend, self._sandbox = backend, sandbox
        self._channel = backend.open_channel(sandbox, argv=['python3', '-I', '-u', '-c', _WORKER], cwd=workspace)
        self._lock = Lock()
        self._outgoing = Queue()
        self._pending = {}
        self._sequence = 0
        self._closed = False
        self._failure = None
        self._output_archive = output_archive
        self._process_ids = set()
        self._process_access = {}
        self._public_environment = public_environment
        self.public_tool_guide = None
        self.stop_error = None
        self._callbacks = ThreadPoolExecutor(max_workers=16, thread_name_prefix='workspace-public-tool')
        self._reader = Thread(target=self._receive, name='task-execution-reader', daemon=True)
        self._writer = Thread(target=self._send, name='task-execution-writer', daemon=True)
        self._reader.start()
        self._writer.start()
        if public_environment is not None:
            try:
                installed = self.call('runtime.install_public_tools', {
                    'client': Path(__file__).with_name('public_tool_client.py').read_text(),
                    'guide': Path(__file__).with_name('public_tools.md').read_text(),
                    'tools': [definition.as_function()['function'] for definition in tool_definitions]})
                self.public_tool_guide = installed['guide']
            except BaseException:
                self.close()
                raise

    def _fail(self, error):
        with self._lock:
            self._failure = error
            pending, self._pending = self._pending, {}
        for future in pending.values():
            future.set_exception(error)

    def _send(self):
        """Coalesce concurrent RPCs into one transport flush, without caching IO.

        Modal stdin drain is a remote acknowledgement. Holding a caller lock
        across each drain serializes otherwise independent agents. A single
        writer preserves order while batching requests arriving within 2 ms.
        """
        try:
            while (first := self._outgoing.get()) is not None:
                batch = [first]
                size = len(first)
                deadline = monotonic() + .002
                closing = False
                while size < 256 * 1024:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    try:
                        request = self._outgoing.get(timeout=remaining)
                    except Empty:
                        break
                    if request is None:
                        closing = True
                        break
                    batch.append(request)
                    size += len(request)
                self._channel.write(b''.join(batch))
                if closing:
                    return
        except Exception as exc:
            self._fail(TaskExecutionError('WORLD_CONNECTION',
                f'Task execution write failed ({type(exc).__name__})'))

    def install_executable(self, name, content):
        """Install a method's runtime dependency outside the submitted source tree."""
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('Invalid executable name')
        self.call('runtime.install_executable', {
            'name': name, 'content': base64.b64encode(content).decode('ascii')})

    def _receive(self):
        remaining = b''
        try:
            for chunk in self._channel.read():
                remaining += chunk
                while b'\n' in remaining:
                    line, remaining = remaining.split(b'\n', 1)
                    result = json.loads(line)
                    if 'callback_id' in result:
                        self._callbacks.submit(self._public_call, result)
                        continue
                    with self._lock:
                        future = self._pending.pop(result['id'])
                    future.set_result(result)
            error = TaskExecutionError('WORLD_CONNECTION', 'Task execution connection closed')
        except Exception as exc:
            error = TaskExecutionError('WORLD_CONNECTION', f'Task execution connection failed ({type(exc).__name__})')
        self._fail(error)

    def _public_call(self, request):
        try:
            with self._lock:
                access = self._process_access.get(request['process_id'])
                if self._closed or not access or not access['allowed'] or self._public_environment is None:
                    raise TaskExecutionError('TOOLS_UNAVAILABLE', 'Public tool access is not enabled for this process.')
                identity = dict(access['identity'])
            parent = identity.pop('tool_call_id', None)
            identity.update(tool=request['name'], tool_call_id='script-' + uuid4().hex,
                            parent_tool_call_id=parent, execution_process_id=request['process_id'])
            with call_scope(**identity):
                result = self._public_environment.call(request['name'], request['arguments'])
            payload = {'result': result}
        except BudgetExceeded as exc:
            self.stop_error = exc
            payload = {'error': {'code': 'EPISODE_BUDGET', 'message': str(exc)}}
        except AgentAccessClosed:
            payload = {'error': {'code': 'TOOLS_CLOSED', 'message': 'The episode has ended; this call was not executed.'}}
        except TaskExecutionError as exc:
            payload = {'error': {'code': exc.code, 'message': str(exc)}}
        except Exception as exc:
            payload = {'error': {'code': 'TOOL_EXECUTION', 'message':
                f'Public tool execution failed ({type(exc).__name__}). Inspect recorded calls and '
                'current state before retrying; an operation may have run.'}}
        with self._lock:
            if not self._closed:
                self._outgoing.put(json.dumps({'callback_id': request['callback_id'], **payload},
                    allow_nan=False).encode() + b'\n')

    def call(self, operation, arguments, *, public_tools=False):
        with self._lock:
            if self._closed or self._failure:
                raise self._failure or TaskExecutionError('WORLD_CONNECTION', 'Task execution is closed')
            self._sequence += 1
            request_id = self._sequence
            if operation == 'process.spawn':
                identity = call_identity()
                if public_tools and self._public_environment is not None and not identity.get('agent_id'):
                    raise TaskExecutionError('PROCESS_IDENTITY', 'Public tool access requires a launching agent identity.')
                access = {'identity': identity, 'allowed': bool(public_tools and self._public_environment is not None)}
                previous = self._process_access.setdefault(arguments['id'], access)
                if previous != access:
                    raise TaskExecutionError('PROCESS_IDENTITY', 'Process ID already belongs to another call or access scope.')
            request = json.dumps({'id': request_id, 'operation': operation,
                                  'arguments': arguments, 'cwd': self.workspace,
                                  'public_tools': bool(public_tools and self._public_environment is not None)}, allow_nan=False).encode() + b'\n'
            future = self._pending[request_id] = Future()
            self._outgoing.put(request)
        result = future.result(timeout=60)
        if 'error' in result:
            raise TaskExecutionError(result['error']['code'], result['error']['message'])
        if operation == 'process.spawn':
            with self._lock:
                self._process_ids.add(result['result']['id'])
        return result['result']

    def _export_outputs(self):
        if self._output_archive is None or not self._process_ids:
            return
        root = str(Path(self.workspace).parent / 'tool-output')
        archive = root + '/native-outputs.tar'
        script = ('import pathlib,tarfile,json,sys; root=pathlib.Path(sys.argv[1]); '
                  'out=tarfile.open(sys.argv[2],"w"); '
                  '[out.add(root/name,arcname=name) for name in json.loads(sys.argv[3])]; out.close()')
        command = shlex.join(['python3', '-I', '-c', script, root, archive, json.dumps(sorted(self._process_ids))])
        result = self._backend.exec(self._sandbox, command, timeout=60)
        if result.exit_code:
            raise RuntimeError('Cannot archive native process outputs')
        destination = Path(self._output_archive)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._backend.read_file(self._sandbox, path=archive))

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._outgoing.put(None)
        try:
            self._writer.join()
            self._channel.close()
        finally:
            self._reader.join(timeout=5)
            self._callbacks.shutdown(wait=True)
        self._export_outputs()
