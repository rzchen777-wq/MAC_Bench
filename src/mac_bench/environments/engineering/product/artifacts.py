"""Durable stage records. A completed stage is reusable only with intact outputs."""

import json
import os
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


def file_hash(path):
    h = sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


class RunArtifacts:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def stage(self, name, inputs):
        directory = self.root / "steps" / name
        directory.mkdir(parents=True, exist_ok=True)
        record = directory / "step.json"
        revision = fingerprint(inputs)
        if record.exists():
            previous = json.loads(record.read_text())
            if previous["input_revision"] != revision:
                raise ValueError(f"{name}: changed inputs require a new run directory")
            if previous["status"] == "complete":
                for path, expected in previous["outputs"].items():
                    if file_hash(directory / path) != expected:
                        raise ValueError(f"{name}: output changed: {path}")
                yield directory, True
                return
        write_json(directory / "inputs.json", inputs)
        state = {"name": name, "input_revision": revision,
                 "status": "running", "started_at": time.time()}
        write_json(record, state)
        try:
            yield directory, False
        except BaseException as error:
            state.update(status="failed", finished_at=time.time(),
                         error_type=type(error).__name__, error=str(error))
            write_json(record, state)
            raise
        else:
            outputs = {str(p.relative_to(directory)): file_hash(p)
                       for p in sorted(directory.rglob("*"))
                       if p.is_file() and p != record}
            state.update(status="complete", finished_at=time.time(), outputs=outputs)
            write_json(record, state)

    def status(self, status, **fields):
        write_json(self.root / "run.json", {"format": "collaboration.product.run.v1",
                   "status": status, "updated_at": time.time(), **fields})
