"""Standard-library private RPC client executed by the sandbox owner."""

import json
import base64
import gzip
import sys
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    request = json.load(sys.stdin) if sys.argv[1] == '--stdin' else json.loads(Path(sys.argv[1]).read_text())
    path = request["path"]
    if not path.startswith(("/agent/", "/owner/")) or "?" in path or "#" in path:
        raise ValueError("Invalid private RPC path")
    body = json.dumps(request["arguments"]).encode()
    with urlopen(
        Request(
            "http://127.0.0.1:6099" + path,
            data=body,
            headers={"Content-Type": "application/json"},
        ),
        timeout=180,
    ) as response:
        raw = response.read()
        if '--compressed' in sys.argv:
            print(json.dumps({'transport': 'gzip-base64/v1',
                              'data': base64.b64encode(gzip.compress(raw)).decode('ascii')}))
        else:
            print(raw.decode())


if __name__ == "__main__":
    main()
