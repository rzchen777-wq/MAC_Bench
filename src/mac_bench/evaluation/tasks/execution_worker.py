"""File and process capabilities executed inside the public task sandbox.

It uses only the standard library. File tools and shell commands see the same
paths; guarded edits compare and replace within one per-file critical section.
"""
import base64
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import signal
import socket
import socketserver
import struct
import subprocess
import time
import re
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock, Thread, Event, Condition

_UMASK = os.umask(0)
os.umask(_UMASK)
os.environ['PATH'] = '/work/.agent-tools/bin:' + os.environ.get('PATH', os.defpath)


class FileError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def target(path, cwd):
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def version(info):
    return ':'.join(str(value) for value in (info.st_dev, info.st_ino, info.st_size,
                                            info.st_mtime_ns, info.st_ctime_ns))


def metadata(path, *, follow=True):
    try:
        value = os.stat(path, follow_symlinks=follow)
    except FileNotFoundError:
        return None
    kind = ('file' if stat.S_ISREG(value.st_mode) else 'directory' if stat.S_ISDIR(value.st_mode)
            else 'symlink' if stat.S_ISLNK(value.st_mode) else 'other')
    return {'version': version(value), 'type': kind, 'size': value.st_size}


def regular(path):
    info = metadata(path)
    if info is None:
        raise FileError('FS_NOT_FOUND', f'File not found: {path}')
    if info['type'] != 'file':
        raise FileError('FS_NOT_REGULAR_FILE', f'Not a regular file: {path}')
    return info


def text(data):
    try:
        value = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise FileError('FS_NOT_TEXT', 'The file is not UTF-8 text') from None
    if '\0' in value:
        raise FileError('FS_NOT_TEXT', 'The file contains binary data')
    return value


def normalized(value):
    return value.replace('\r\n', '\n').replace('\r', '\n')


def mutate(path, args, editing):
    # A lock per canonical path permits unrelated files to be edited in parallel.
    locks = Path(tempfile.gettempdir()) / 'mac-bench-file-locks'
    locks.mkdir(exist_ok=True)
    key = hashlib.sha256(path.encode()).hexdigest()
    with (locks / key).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        info = metadata(path)
        expected = args.get('expected')
        if expected:
            if expected.get('kind') == 'createIfAbsent':
                if info is not None:
                    raise FileError('FS_NOT_OBSERVED', 'File already exists; read it before overwriting')
            elif info is None or info['version'] != expected['version']:
                raise FileError('FS_STALE_VERSION', 'File changed since it was read; read the current version')
        if info and info['type'] != 'file':
            raise FileError('FS_NOT_REGULAR_FILE', f'Not a regular file: {path}')
        if editing:
            regular(path)
            before = normalized(text(Path(path).read_bytes()))
            edit = args['edit']
            old, new = normalized(edit['oldString']), normalized(edit['newString'])
            if not old:
                raise FileError('FS_EDIT_NOT_FOUND', 'old_string must not be empty')
            count = before.count(old)
            if not count:
                raise FileError('FS_EDIT_NOT_FOUND', 'The requested text does not occur in the file')
            if count > 1 and not edit['replaceAll']:
                raise FileError('FS_AMBIGUOUS_EDIT', f'The requested text occurs {count} times; provide more context or replace all')
            after = before.replace(old, new, -1 if edit['replaceAll'] else 1)
        else:
            after = normalized(args['content'])
            before = None
            # DSH accepts an omitted diff basis for large or binary overwrites.
            if info and info['size'] < 10 * 1024 * 1024 and len(after.encode()) < 10 * 1024 * 1024:
                try:
                    before = normalized(text(Path(path).read_bytes()))
                except FileError:
                    pass
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.mac-bench-write-', dir=parent)
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode) if info else 0o666 & ~_UMASK
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(after.encode('utf-8'))
            if expected and expected.get('kind') == 'createIfAbsent':
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    raise FileError('FS_NOT_OBSERVED', 'File was created by another operation') from None
            else:
                os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        outcome = {'version': metadata(path)['version'], 'before': before, 'after': after}
        if not editing:
            outcome['operation'] = 'update' if info else 'create'
        return outcome


def filesystem(operation, args, cwd):
    if operation == 'probe':
        # Batch metadata discovery without caching across agent turns. Resolve
        # each path in the sandbox so aliases and symlink/.. keep OS semantics.
        paths = args['paths']
        if not isinstance(paths, list) or len(paths) > 512 or not all(isinstance(p, str) for p in paths):
            raise ValueError('probe requires at most 512 paths')
        rows = []
        for raw in paths:
            try:
                resolved = target(raw, args.get('cwd') or cwd)
                rows.append({'path': raw, 'targetKey': resolved, 'displayPath': resolved,
                             'info': metadata(resolved)})
            except OSError:
                # A speculative inaccessible path must not break unrelated
                # discovery. Its ordinary operation will report the error.
                continue
        return rows
    path = target(args.get('path', '.'), args.get('cwd') or cwd)
    if operation == 'resolve':
        return {'targetKey': path, 'displayPath': path}
    if operation == 'stat':
        return metadata(path)
    if operation == 'lstat':
        raw = args['path']
        return metadata(raw if os.path.isabs(raw) else os.path.join(args.get('cwd') or cwd, raw), follow=False)
    if operation == 'list':
        rows = []
        with os.scandir(path) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                resolved = target(entry.path, cwd)
                info = metadata(resolved)
                rows.append({'name': entry.name, 'target': {'targetKey': resolved, 'displayPath': resolved},
                             **(info or {'type': 'other'})})
        return rows
    if operation in {'read_text', 'read_bytes', 'read_range'}:
        info = regular(path)
        with open(path, 'rb') as source:
            if operation == 'read_range':
                offset, length = args['offset'], args['length']
                if type(offset) is not int or type(length) is not int or min(offset, length) < 0:
                    raise ValueError('Byte range must contain nonnegative integers')
                source.seek(offset)
                data = source.read(length)
            elif operation == 'read_bytes':
                maximum = args['maxBytes']
                if type(maximum) is not int or maximum < 0:
                    raise ValueError('maxBytes must be a nonnegative integer')
                if info['size'] > maximum:
                    raise FileError('FS_TOO_LARGE', f'File exceeds the {maximum}-byte read limit')
                data = source.read(maximum + 1)
                if len(data) > maximum:
                    raise FileError('FS_TOO_LARGE', f'File exceeds the {maximum}-byte read limit')
            else:
                return text(source.read())
        return base64.b64encode(data).decode('ascii')
    if operation in {'write', 'edit'}:
        return mutate(path, args, operation == 'edit')
    if operation == 'executable':
        command = args['command']
        if not os.path.isabs(command) and '/' in command:
            raise ValueError('Executable must be absolute or a PATH name')
        resolved = shutil.which(command, path=args.get('env', {}).get('PATH'))
        if resolved is None:
            raise FileError('FS_NOT_FOUND', f'Executable not found: {command}')
        return os.path.realpath(resolved)
    raise ValueError('Unknown filesystem operation: ' + operation)


class Output:
    """Bounded collect-mode tail, or a disk-backed pipe with independent offsets."""
    def __init__(self, stream, mode, path, changed):
        self.mode, self.path = mode, Path(path)
        self.lock = Lock()
        self.tail = b''
        self.size = 0
        self.pipe = mode in ('pipe', 'inherit')
        self.maximum = None if self.pipe else mode['maxBytes']
        self.spill_max = None if self.pipe else mode.get('spill', {}).get('maxBytes', 0)
        self.spilled = self.pipe or bool(self.spill_max)
        self.output = self.path.open('wb') if self.spilled else None
        self.changed = changed
        self.thread = Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream):
        output = self.output
        try:
            while data := stream.read(65536):
                with self.lock:
                    self.size += len(data)
                    if not self.pipe:
                        self.tail = (self.tail + data)[-self.maximum:] if self.maximum else b''
                    if output:
                        if self.spill_max is not None and self.size > self.spill_max:
                            output.close()
                            output = None
                            self.path.unlink()
                            self.spilled = False
                        else:
                            output.write(data)
                            output.flush()
                self.changed()
        finally:
            if output:
                output.close()
            stream.close()

    def read(self, offset):
        with self.lock:
            if self.pipe:
                with self.path.open('rb') as source:
                    source.seek(offset)
                    data = source.read(1024 * 1024)
                return {'data': base64.b64encode(data).decode(), 'nextOffset': offset + len(data), 'endOffset': self.size, 'lossy': False}
            base = self.size - len(self.tail)
            data = self.tail[max(0, offset - base):]
            result = {'data': base64.b64encode(data).decode(), 'nextOffset': self.size, 'endOffset': self.size, 'lossy': offset < base}
            if base > 0 and self.spilled:
                result['spillPath'] = str(self.path)
            return result


class Process:
    def __init__(self, args, cwd, tool_environment=None):
        self.id = args['id']
        self.done, self.lock = Event(), Lock()
        self.changed = Condition()
        self.stopping = False
        self.stdin_lock = Lock()
        self.resource_limit = False
        self.grace = args['graceMs'] / 1000
        spec = args['stdio']
        if spec.get('control'):
            raise ValueError('Dedicated control pipes are not used by task tools')
        root = Path(cwd).parent / 'tool-output' / self.id
        root.mkdir(parents=True, exist_ok=False)
        environment = {k: v for k, v in os.environ.items()
            if not re.search(r'(TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL)', k, re.I) and not k.startswith('DSH_')}
        for key, value in args.get('env', {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        # Grants originate from the evaluator, never from shell-supplied env.
        environment.pop('MAC_BENCH_TOOLS_SOCKET', None)
        if tool_environment:
            environment['MAC_BENCH_TOOLS_SOCKET'] = tool_environment['socket']
            environment['PYTHONPATH'] = tool_environment['root'] + os.pathsep + environment.get('PYTHONPATH', '')
            environment['PATH'] = tool_environment['root'] + os.pathsep + environment.get('PATH', os.defpath)
        # Set limits in a fresh interpreter, not preexec_fn in this threaded worker.
        cap = COMMAND_MEMORY_MIB * 1024 * 1024
        launch = f'import os,resource,sys,json; resource.setrlimit(resource.RLIMIT_DATA,({cap},{cap})); a=json.loads(sys.argv[1]); os.execvpe(a[0],a,os.environ)'
        # The launch helper belongs to this worker, not to the requested child
        # PATH (which may deliberately contain only a virtual environment).
        self.process = subprocess.Popen([sys.executable, '-I', '-c', launch, json.dumps(args['argv'])],
            cwd=args['cwd'], env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        self.accounting = CommandResources(cwd, self.process.pid)
        self.stdout = Output(self.process.stdout, spec['stdout'], root / 'stdout.log', self.notify)
        self.stderr = Output(self.process.stderr, spec['stderr'], root / 'stderr.log', self.notify)
        self.monitor = Thread(target=self._monitor, daemon=True)
        self.monitor.start()

    def initialize_stdin(self, spec):
        if spec == 'ignore':
            self.write('', True)
        elif isinstance(spec, dict):
            self.write(base64.b64encode(spec['data'].encode()).decode(), True)

    def _monitor(self):
        while True:
            # Short commands wake immediately; long ones get four resource
            # samples per second instead of a /proc scan every 50 ms.
            try:
                code = self.process.wait(timeout=.25)
            except subprocess.TimeoutExpired:
                code = None
            members = session_members(self.process.pid)
            reason = self.accounting.reason()
            if reason:
                self.resource_limit = reason
                self.terminate()
            if code is not None and not members:
                break
            if code is not None:
                time.sleep(.25)  # The main process exited; descendants remain.
        self.process.wait()
        self.accounting.close()
        self.stdout.thread.join()
        self.stderr.thread.join()
        self.done.set()
        self.notify()

    def notify(self):
        with self.changed:
            self.changed.notify_all()

    def write(self, data, eof):
        with self.stdin_lock:
            pending = memoryview(base64.b64decode(data))
            while pending:
                pending = pending[self.process.stdin.write(pending):]
            if eof:
                self.process.stdin.close()

    def terminate(self):
        with self.lock:
            if self.stopping or self.done.is_set():
                return
            self.stopping = True
        def stop():
            for sig in (signal.SIGTERM, signal.SIGKILL):
                stop_session(self.process.pid, sig)
                if sig == signal.SIGTERM:
                    self.done.wait(self.grace)
        Thread(target=stop, daemon=True).start()

    def poll(self, args):
        # Output and exit wake the caller immediately. Quiet jobs keep one
        # request open instead of making five round trips per second.
        with self.changed:
            self.changed.wait_for(lambda: self.done.is_set()
                or self.stdout.size > args.get('stdoutOffset', 0)
                or self.stderr.size > args.get('stderrOffset', 0),
                timeout=min(max(args.get('waitMs', 0), 0), 30000) / 1000)
        code = self.process.returncode if self.done.is_set() else None
        result = {'done': self.done.is_set(), 'stdout': self.stdout.read(args.get('stdoutOffset', 0)),
                  'stderr': self.stderr.read(args.get('stderrOffset', 0)),
                  'exitCode': (137 if self.resource_limit else code if code is not None and code >= 0 else None),
                  'signal': signal.Signals(-code).name if code is not None and code < 0 else None}
        if self.resource_limit:
            result['resourceLimit'] = self.resource_limit
        return result


class Processes:
    def __init__(self):
        self.items, self.lock = {}, Lock()
        self.closed = False

    def call(self, operation, args, cwd, tool_environment=None):
        if operation == 'spawn':
            with self.lock:
                if self.closed:
                    raise ValueError('Task execution is closing')
                if args['id'] in self.items:
                    return {'id': args['id']}
                process = Process(args, cwd, tool_environment)
                self.items[process.id] = process
            process.initialize_stdin(args['stdio']['stdin'])
            return {'id': process.id}
        with self.lock:
            process = self.items[args['id']]
        if operation == 'poll':
            return process.poll(args)
        if operation == 'write':
            process.write(args['data'], args['eof'])
            return None
        if operation == 'terminate':
            process.terminate()
            return None
        raise ValueError('Unknown process operation: ' + operation)

    def identify(self, pid):
        session = os.getsid(pid)
        with self.lock:
            for process in self.items.values():
                if process.process.pid == session and not process.done.is_set():
                    return process.id
        raise ValueError('Caller is not part of an active task process')

    def close(self):
        with self.lock:
            self.closed = True
            processes = list(self.items.values())
        for process in processes:
            process.terminate()
        for process in processes:
            process.done.wait()


class PublicTools:
    """Carry local script calls over the existing evaluator channel.

    Peer credentials identify the managed process; no actor ID or application
    credential is accepted from the script. The host performs tool dispatch.
    """
    def __init__(self, processes, emit):
        self.processes, self.emit = processes, emit
        self.root = tempfile.TemporaryDirectory(prefix='mac-tools-')
        self.lock, self.pending = Lock(), {}
        self.sequence, self.closed = 0, False
        self.server = self.thread = None

    def install(self, args):
        if self.server is not None:
            raise ValueError('Public tools are already installed')
        root = Path(self.root.name)
        (root / 'mac_tools.py').write_text(args['client'])
        (root / 'tools.json').write_text(json.dumps(args['tools']))
        (root / 'README.md').write_text(args['guide'])
        command = root / 'mac-tools'
        command.write_text('#!/usr/bin/env python3\nfrom mac_tools import main\nmain()\n')
        command.chmod(0o755)
        tools = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    pid, _, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    process_id = tools.processes.identify(pid)
                    request = json.loads(self.rfile.readline())
                    response = tools.call(process_id, request)
                except Exception as exc:
                    response = {'error': {'code': 'TOOL_CONNECTION', 'message': str(exc)}}
                try:
                    self.wfile.write(json.dumps(response, allow_nan=False).encode() + b'\n')
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Accepted writes finish even if their caller has exited.
        class Server(socketserver.ThreadingUnixStreamServer):
            request_queue_size = 128
        self.server = Server(str(root / 'tools.sock'), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return {'guide': str(root / 'README.md')}

    def environment(self):
        if self.server is None:
            raise ValueError('Public tool client is not installed')
        return {'root': self.root.name, 'socket': str(Path(self.root.name) / 'tools.sock')}

    def call(self, process_id, request):
        name, arguments = request['name'], request['arguments']
        with self.lock:
            if self.closed:
                raise ValueError('Task tool connection is closed')
            self.sequence += 1
            key = self.sequence
            future = self.pending[key] = Future()
        self.emit({'callback_id': key, 'process_id': process_id,
                   'name': name, 'arguments': arguments})
        return future.result()

    def receive(self, response):
        with self.lock:
            future = self.pending.pop(response['callback_id'])
        future.set_result(response)

    def close(self):
        with self.lock:
            self.closed = True
            pending, self.pending = self.pending, {}
        for future in pending.values():
            future.set_result({'error': {'code': 'TOOL_CONNECTION',
                'message': 'Connection closed; inspect current state before retrying any write.'}})
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
        self.root.cleanup()


def respond(request, processes, tools):
    try:
        operation = request['operation']
        if operation == 'runtime.install_executable':
            args = request['arguments']
            if not re.fullmatch(r'[A-Za-z0-9_-]+', args['name']):
                raise ValueError('Invalid executable name')
            root = Path('/work/.agent-tools/bin')
            root.mkdir(parents=True, exist_ok=True)
            path = root / args['name']
            path.write_bytes(base64.b64decode(args['content'], validate=True))
            path.chmod(0o755)
            value = None
        elif operation == 'runtime.install_public_tools':
            value = tools.install(request['arguments'])
        elif operation.startswith('fs.'):
            value = filesystem(operation[3:], request['arguments'], request['cwd'])
        elif operation.startswith('process.'):
            environment = tools.environment() if request.get('public_tools') else None
            value = processes.call(operation[8:], request['arguments'], request['cwd'], environment)
        else:
            raise ValueError('Unknown execution operation: ' + operation)
        result = {'result': value}
    except FileError as exc:
        result = {'error': {'code': exc.code, 'message': exc.message}}
    except OSError as exc:
        codes = {errno.ENOENT: 'FS_NOT_FOUND', errno.ENOTDIR: 'FS_NOT_DIRECTORY',
                 errno.EISDIR: 'FS_NOT_REGULAR_FILE', errno.EACCES: 'FS_PERMISSION_DENIED',
                 errno.EPERM: 'FS_PERMISSION_DENIED'}
        result = {'error': {'code': codes.get(exc.errno, 'FS_IO_ERROR'), 'message': str(exc)}}
    except (KeyError, TypeError, ValueError) as exc:
        result = {'error': {'code': 'FS_IO_ERROR', 'message': str(exc)}}
    except Exception as exc:
        result = {'error': {'code': 'FS_IO_ERROR', 'message': f'Filesystem operation failed ({type(exc).__name__})'}}
    return {'id': request['id'], **result}


def main():
    output = Lock()
    processes = Processes()
    def emit(result):
        with output:
            sys.stdout.write(json.dumps(result, ensure_ascii=True) + '\n')
            sys.stdout.flush()
    tools = PublicTools(processes, emit)
    def dispatch(request):
        emit(respond(request, processes, tools))
    # Output waits cannot occupy the workers that send stdin, terminate a
    # process, or access files.
    with ThreadPoolExecutor(max_workers=16) as pool, ThreadPoolExecutor(max_workers=32) as polls:
        try:
            for line in sys.stdin:
                request = json.loads(line)
                if 'callback_id' in request:
                    tools.receive(request)
                    continue
                executor = polls if request['operation'] == 'process.poll' else pool
                executor.submit(dispatch, request)
        finally:
            # Stop consumers before joining pending writes: a child that does
            # not read stdin may otherwise keep a pool thread blocked forever.
            processes.close()
            tools.close()


if __name__ == '__main__':
    main()
