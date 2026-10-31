"""Private loopback-only native world service, reached by the trusted Modal owner.

Only the four /agent endpoints are forwarded to agents. Administrative observation,
evaluation and episode controls remain on the owner side of the RPC boundary.
"""

import argparse
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mac_bench.environments.tools.api import INTERFACE_VERSION, dispatch
from mac_bench.environments.verification.contracts import digest
from mac_bench.environments.tools.contracts import ToolError


def serve(root, port):
    root = Path(root).resolve()
    spec = importlib.util.spec_from_file_location(
        "trusted_world_profile", root / "profile.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    world = module.build(root)
    world.bind()
    (Path("/work/sessions")).mkdir(exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 2_000_000:
                    raise ValueError("Request size")
                request = json.loads(self.rfile.read(length))
                if self.path.startswith("/agent/"):
                    result = dispatch(
                        world.agent, self.path.removeprefix("/agent/"), request
                    )
                elif self.path == "/owner/start":
                    result = {**world.start_episode(request["instance_id"]), 'tool_api_version': INTERFACE_VERSION}
                    forms = root / 'record_forms.json'
                    if forms.is_file():
                        world.tools.configure_record_forms(json.loads(forms.read_text()),
                            json.loads((root / 'record_directory.json').read_text()))
                        result['apps'] = world.agent.list_apps()
                    data = root / 'public_data_resources.json'
                    if data.is_file():
                        resources = json.loads(data.read_text())
                        world.tools.configure_data(resources, resources.get('public/registers.json'))
                        result['apps'] = world.agent.list_apps()
                elif self.path == "/owner/stop":
                    world.stop_episode()
                    result = {"stopped": True}
                elif self.path == "/owner/interface":
                    result = {"tool_api_version": INTERFACE_VERSION,
                              "owner_capabilities": ["observe_and_evaluate"]}
                elif self.path == "/owner/observe_digest":
                    result = {"digest": digest(world.observe())}
                elif self.path == "/owner/observe":
                    result = world.observe()
                elif self.path == "/owner/evaluate":
                    result = world.evaluate(world.observe())
                elif self.path == "/owner/observe_and_evaluate":
                    observation = world.observe()
                    result = {'observation': observation,
                              'evaluation': world.evaluate(observation)}
                else:
                    raise ValueError("Unknown endpoint")
                payload = {"result": result}
            except Exception as exc:
                payload = {
                    "error": {
                        "type": type(exc).__name__,
                        "code": exc.code if isinstance(exc, ToolError) else "world_operation_failed",
                        "message": str(exc)
                        if isinstance(exc, ToolError)
                        else "World operation failed",
                    }
                }
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, default=6099)
    args = parser.parse_args()
    serve(args.root, args.port)
