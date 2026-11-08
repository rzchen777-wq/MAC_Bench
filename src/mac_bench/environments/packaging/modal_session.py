"""Owner-side access to one live Modal world; no secrets enter baseline context."""

import base64
import gzip
import json
import queue
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from time import monotonic
from pathlib import Path

from mac_bench.environments.tools.api import SCHEMAS, INTERFACE_VERSION


class WorldRPCError(RuntimeError):
    """One failed exchange. A new connection must not replay this request."""
    def __init__(self, sent):
        self.sent = sent
        super().__init__('World RPC response unavailable; operation not replayed. '
            + ('The request may have run. Inspect its saved receipt and current state.' if sent
               else 'The request was not sent.'))


class _Relay:
    def __init__(self, process):
        self.process = process
        self.pending = {}
        self.outgoing = queue.Queue()


class _Request:
    def __init__(self):
        self.future = Future()
        self.sent = False


class ModalWorldSession:
    def __init__(self, backend, sandbox, *, root="/work/world"):
        self.backend, self.sandbox, self.root = backend, sandbox, root
        self._lock = threading.Lock()
        self._connection = None
        self._counter = 0
        self._closed = False
        self._slots = threading.BoundedSemaphore(32)

    def _connect(self):
        """Called under the state lock; subsequent I/O never holds that lock."""
        current = self.root + '/src/' + __package__.replace('.', '/') + '/rpc_client.py'
        process = self.sandbox.raw.exec('python3', '-I', '-u', current, text=True, bufsize=1)
        relay = self._connection = _Relay(process)
        threading.Thread(target=self._receive, args=(relay,), daemon=True).start()
        threading.Thread(target=self._send, args=(relay,), daemon=True).start()
        def drain_errors():
            for _ in process.stderr:
                pass
        threading.Thread(target=drain_errors, daemon=True).start()
        return relay

    def _receive(self, relay):
        try:
            for line in relay.process.stdout:
                envelope = json.loads(line)
                with self._lock:
                    if self._connection is not relay:
                        return
                    request = relay.pending.pop(envelope['id'])
                request.future.set_result(envelope)
        except Exception:
            pass
        finally:
            self._disconnect(relay)

    def _send(self, relay):
        """One writer batches adjacent calls; replies are matched independently."""
        try:
            while (first := relay.outgoing.get()) is not None:
                batch, size = [first], len(first[1])
                deadline = monotonic() + .002
                closing = False
                while size < 256 * 1024 and monotonic() < deadline:
                    try:
                        item = relay.outgoing.get(timeout=max(0, deadline - monotonic()))
                    except queue.Empty:
                        break
                    if item is None:
                        closing = True
                        break
                    batch.append(item)
                    size += len(item[1])
                with self._lock:
                    if self._connection is not relay:
                        return
                    pending = [(identity, line) for identity, line in batch if identity in relay.pending]
                    for identity, _ in pending:
                        # A partial write/drain failure may already have sent bytes.
                        relay.pending[identity].sent = True
                if pending:
                    relay.process.stdin.write(''.join(line for _, line in pending))
                    relay.process.stdin.drain()
                if closing:
                    return
        except Exception:
            self._disconnect(relay)

    def _disconnect(self, relay):
        with self._lock:
            if self._connection is not relay:
                return
            self._connection = None
            pending, relay.pending = relay.pending, {}
            relay.outgoing.put(None)
        for request in pending.values():
            request.future.set_exception(WorldRPCError(request.sent))
        try:
            relay.process.stdin.write_eof()
            relay.process.stdin.drain()
        except Exception:
            pass

    def close(self):
        with self._lock:
            self._closed = True
            relay = self._connection
        if relay is not None:
            self._disconnect(relay)

    @staticmethod
    def _decode_response(envelope):
        response = envelope['response']
        if response.get('transport') == 'gzip-base64/v1':
            response = json.loads(gzip.decompress(base64.b64decode(response['data'], validate=True)))
        if not isinstance(response, dict) or set(response) not in ({'result'}, {'error'}):
            raise ValueError('Invalid world RPC response')
        return response

    def request(self, path, arguments):
        # Bound queued requests without serializing their native execution.
        with self._slots:
            with self._lock:
                if self._closed:
                    raise RuntimeError('World RPC connection closed')
                self._counter += 1
                identity = self._counter
                payload = json.dumps({'id': identity, 'path': path, 'arguments': arguments},
                                     ensure_ascii=True, allow_nan=False) + '\n'
                try:
                    relay = self._connection or self._connect()
                except Exception as exc:
                    raise WorldRPCError(False) from exc
                request = relay.pending[identity] = _Request()
                relay.outgoing.put((identity, payload))
            try:
                envelope = request.future.result(timeout=210)
            except FutureTimeout as exc:
                self._disconnect(relay)
                raise WorldRPCError(request.sent) from exc
            if 'transport_error' in envelope:
                raise WorldRPCError(True)
            try:
                response = self._decode_response(envelope)
            except Exception as exc:
                self._disconnect(relay)
                raise WorldRPCError(request.sent) from exc
        if "error" in response:
            if path.startswith("/agent/"):
                return {"error": response["error"]}
            raise RuntimeError("World RPC: " + response["error"]["type"])
        return response["result"]

    def agent_call(self, name, arguments):
        if name not in SCHEMAS:
            raise ValueError("Not an agent tool entrypoint")
        try:
            return self.request("/agent/" + name, arguments)
        except WorldRPCError as exc:
            error = {'code': 'tool_transport_failed', 'message': str(exc),
                     'outcome': 'unknown' if exc.sent else 'not_started'}
            if name in {'call_tool', 'compute'} and arguments.get('request_id'):
                error.update(request_id=arguments['request_id'],
                    recovery='Use read_result with this request_id and path="" to inspect the saved receipt without executing the action. Then inspect current application state.')
            return {'error': error}

    def start_episode(self, instance_id):
        result = self.request("/owner/start", {"instance_id": instance_id})
        if result.get('tool_api_version') != INTERFACE_VERSION:
            raise RuntimeError('World runtime and client tool interfaces differ')
        self.instance_id = instance_id
        return result

    def stop_episode(self):
        return self.request("/owner/stop", {})

    def observe_digest(self):
        return self.request("/owner/observe_digest", {})["digest"]

    def observe(self):
        return self.request("/owner/observe", {})

    def evaluate(self):
        return self.request("/owner/evaluate", {})

    def observe_and_evaluate(self):
        """Grade the exact captured final state once."""
        return self.request('/owner/observe_and_evaluate', {})

    def export_receipts(self, destination):
        if not hasattr(self, "instance_id"):
            return
        Path(destination).write_bytes(
            self.backend.read_file(
                self.sandbox, path="/work/sessions/" + self.instance_id + ".sqlite"
            )
        )
