"""Owner-side access to one live Modal world; no secrets enter baseline context."""

import base64
import gzip
import json
import queue
import threading
from pathlib import Path

from mac_bench.environments.tools.api import SCHEMAS, INTERFACE_VERSION


class ModalWorldSession:
    def __init__(self, backend, sandbox, *, root="/work/world"):
        self.backend, self.sandbox, self.root = backend, sandbox, root
        self._lock = threading.Lock()
        self._process = None
        self._responses = queue.Queue()
        self._counter = 0
        self._closed = False

    def _connect(self):
        current = self.root + '/src/' + __package__.replace('.', '/') + '/rpc_client.py'
        self._process = self.sandbox.raw.exec('python3', '-I', '-u', current,
                                               text=True, bufsize=1)
        process = self._process
        def read():
            try:
                for line in process.stdout:
                    self._responses.put(json.loads(line))
            except Exception as exc:
                self._responses.put(exc)
            finally:
                self._responses.put(EOFError('World RPC connection closed'))
        def drain_errors():
            for _ in process.stderr:
                pass
        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=drain_errors, daemon=True).start()

    def close(self):
        self._closed = True
        process, self._process = self._process, None
        if process is not None:
            process.stdin.write_eof()
            process.stdin.drain()

    def request(self, path, arguments):
        with self._lock:
            if self._closed:
                raise RuntimeError('World RPC connection closed')
            self._counter += 1
            payload = json.dumps({'id': self._counter, 'path': path, 'arguments': arguments},
                                 ensure_ascii=True, allow_nan=False)
            try:
                if self._process is None:
                    self._connect()
                self._process.stdin.write(payload + '\n')
                self._process.stdin.drain()
                envelope = self._responses.get(timeout=210)
                if isinstance(envelope, Exception):
                    raise envelope
                if envelope.get('id') != self._counter or 'transport_error' in envelope:
                    raise RuntimeError('World RPC exchange failed; operation not replayed')
                response = envelope['response']
                if response.get('transport') == 'gzip-base64/v1':
                    response = json.loads(gzip.decompress(base64.b64decode(response['data'], validate=True)))
                if not isinstance(response, dict) or set(response) not in ({'result'}, {'error'}):
                    raise RuntimeError('Invalid world RPC response; operation not replayed')
            except Exception:
                self._closed = True
                raise
        if "error" in response:
            if path.startswith("/agent/"):
                return {"error": response["error"]}
            raise RuntimeError("World RPC: " + response["error"]["type"])
        return response["result"]

    def agent_call(self, name, arguments):
        if name not in SCHEMAS:
            raise ValueError("Not an agent tool entrypoint")
        return self.request("/agent/" + name, arguments)

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
