"""Explicit, atomic run manifests; history is never selected by directory recency."""

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path


def write_json(value, path=None):
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        print(text, end="")
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def code_revision():
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


class RunArtifacts:
    """A caller-selected new output directory with explicit inputs and step results."""

    def __init__(self, directory, *, stage, inputs, item_id=None):
        if stage not in {"induce", "synthesize", "realize"}:
            raise ValueError("Unknown collaboration stage")
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=False)
        self.document = {
            "schema_version": 1,
            "stage": stage,
            "run_id": self.root.name,
            "item_id": item_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "code_revision": code_revision(),
            "inputs": inputs,
            "status": "running",
            "steps": {},
            "ready_for_agent": False,
        }
        self.save()

    def save(self):
        write_json(self.document, self.root / "manifest.json")

    def step(self, name, status, *, artifact=None):
        if artifact is not None:
            path = Path(artifact)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not (self.root / path).is_file()
            ):
                raise ValueError(
                    "Step artifact must be an existing file inside this run"
                )
        self.document["steps"][name] = {"status": status, "artifact": artifact}
        self.save()

    def finish(self, status):
        self.document["outputs"] = sorted(
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file() and path != self.root / "manifest.json"
        )
        self.document["status"] = status
        self.save()


def run_command(args, dispatch, *, stage):
    """Record a stage CLI invocation without overwriting another run."""
    inputs = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
        if key != "out"
    }
    run = RunArtifacts(args.out, stage=stage, inputs=inputs)
    try:
        result = dispatch(args)
        run.finish("completed" if result in (0, None) else "failed")
        return result
    except Exception as exc:
        run.document["error_type"] = type(exc).__name__
        run.finish("failed")
        raise
