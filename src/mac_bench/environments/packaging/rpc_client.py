"""Persistent private RPC relay, owned by the evaluator."""
import base64
import gzip
import json
import sys
from urllib.request import Request, urlopen


def exchange(request):
    path = request["path"]
    if not path.startswith(("/agent/", "/owner/")) or "?" in path or "#" in path:
        raise ValueError("Invalid private RPC path")
    with urlopen(Request("http://127.0.0.1:6099" + path,
                         data=json.dumps(request["arguments"]).encode(),
                         headers={"Content-Type": "application/json"}), timeout=180) as response:
        raw = response.read()
        if len(raw) >= 32768:
            return {'transport': 'gzip-base64/v1',
                    'data': base64.b64encode(gzip.compress(raw, compresslevel=1)).decode('ascii')}
        return json.loads(raw)


def main():
    for line in sys.stdin:
        request = json.loads(line)
        try:
            response = {"id": request["id"], "response": exchange(request)}
        except Exception as exc:
            # A transport error must not trigger a replay of a possible write.
            response = {"id": request["id"], "transport_error": type(exc).__name__}
        print(json.dumps(response, ensure_ascii=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
