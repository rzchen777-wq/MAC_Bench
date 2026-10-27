"""Stable JSON content identities shared by planning and execution."""

import json
from hashlib import sha256


def digest(value):
    return sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
