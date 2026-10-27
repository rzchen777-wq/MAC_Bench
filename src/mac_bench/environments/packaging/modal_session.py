"""Owner-side access to one live Modal world; no secrets enter baseline context."""

import json
import base64
import gzip
import shlex
from pathlib import Path

from mac_bench.environments.tools.api import SCHEMAS


class ModalWorldSession:
    def __init__(self, backend, sandbox, *, root="/work/world"):
        self.backend, self.sandbox, self.root = backend, sandbox, root

    def request(self, path, arguments):
        # A quoted heredoc carries one JSON line literally. No shell expansion,
        # file upload, mkdir exec or cleanup exec per business operation.
        payload = json.dumps({"path": path, "arguments": arguments}, ensure_ascii=True, allow_nan=False)
        current = self.root + '/src/mac_bench/environments/packaging/rpc_client.py'
        command = shlex.join(['python3', '-I', current, '--stdin', '--compressed'])
        command += " <<'MAC_BENCH_RPC_JSON'\n" + payload + '\nMAC_BENCH_RPC_JSON\n'
        result = self.backend.exec(self.sandbox, command, timeout=210)
        if result.exit_code:
            raise RuntimeError(f"Native world RPC failed: {result.stdout[-1200:]}")
        response = json.loads(result.stdout)
        if response.get('transport') == 'gzip-base64/v1':
            response = json.loads(gzip.decompress(base64.b64decode(response['data'], validate=True)))
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
        if result.get('tool_api_version') != 2:
            raise RuntimeError('World runtime and baseline tool interfaces differ; capture a v2 world')
        self.instance_id = instance_id
        return result

    def stop_episode(self):
        return self.request("/owner/stop", {})

    def observe(self):
        return self.request("/owner/observe", {})

    def evaluate(self):
        return self.request("/owner/evaluate", {})

    def observe_and_evaluate(self):
        """Grade the exact captured final state once, with legacy fallback."""
        if not hasattr(self, '_owner_capabilities'):
            self._owner_capabilities = self.request('/owner/interface', {}).get('owner_capabilities', [])
        if 'observe_and_evaluate' in self._owner_capabilities:
            return self.request('/owner/observe_and_evaluate', {})
        return {'observation': self.observe(), 'evaluation': self.evaluate()}

    def export_receipts(self, destination):
        if not hasattr(self, "instance_id"):
            return
        Path(destination).write_bytes(
            self.backend.read_file(
                self.sandbox, path="/work/sessions/" + self.instance_id + ".sqlite"
            )
        )
