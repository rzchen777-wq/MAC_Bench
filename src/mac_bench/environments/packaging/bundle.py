"""Content-addressed, self-contained task artifacts; deployment secrets stay private."""

import json
from hashlib import sha256
from pathlib import Path

from mac_bench.runtime.common.artifacts import write_json
from mac_bench.runtime.common.hashing import digest
def scenario_context(name):
    if name not in {'business_finance', 'service_administration', 'project_content_operations'}:
        raise ValueError('Unknown business task category')


def _file(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Bundle paths must be relative and contained")
    target = root / path
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Bundle paths cannot escape through symlinks")
    if not target.is_file():
        raise FileNotFoundError(relative)
    return target


def seal_bundle(directory, *, world_id, public_files, metadata):
    root = Path(directory)
    if (root / "manifest.json").exists():
        raise FileExistsError("World bundle is already sealed")
    metadata = dict(metadata)
    if "scenario_family" in metadata:
        scenario_context(metadata["scenario_family"])
    if "public/task.txt" not in public_files:
        raise ValueError("Public task description is required")
    for path in public_files:
        if not path.startswith("public/"):
            raise ValueError("Private world material cannot be exposed to the agent")
        _file(root, path)
    files = {
        str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    for path in files:
        _file(root, path)
    manifest = {
        "format": "collaboration.world.v1",
        "world_id": world_id,
        "public_files": list(public_files),
        "files": files,
        "metadata": metadata,
    }
    manifest["revision"] = digest(manifest)
    write_json(manifest, root / "manifest.json")
    return WorldBundle.load(root)


class WorldBundle:
    def __init__(self, root, manifest):
        self.root, self.manifest = root, manifest

    @classmethod
    def load(cls, directory, *, verify=False):
        root = Path(directory).resolve()
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest.get("format") not in {"collaboration.world.v1", "mac_bench.task.v1"}:
            raise ValueError("Unsupported world bundle format")
        if verify or manifest["format"] == "collaboration.world.v1":
            if manifest.get("revision") != digest(
                {k: v for k, v in manifest.items() if k != "revision"}
            ):
                raise ValueError("World manifest changed after sealing")
            for name, checksum in manifest["files"].items():
                if sha256(_file(root, name).read_bytes()).hexdigest() != checksum:
                    raise ValueError("World artifact changed: " + name)
        for name in manifest["public_files"]:
            if not name.startswith("public/") or name not in manifest["files"]:
                raise ValueError("Invalid public artifact declaration")
        return cls(root, manifest)

    def public_task(self):
        parts = []
        for name in self.manifest["public_files"]:
            parts.append(
                "==========\n" + name + "\n" + _file(self.root, name).read_text()
            )
        return "\n".join(parts)
