"""One content-addressed cache format for prepared environment images."""

from dataclasses import asdict
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from .base import PreparedImage
from mac_bench.runtime.paths import repository_root


def recipe_key(recipe):
    return sha256(
        json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def image_directory(kind, scope, recipe):
    if kind not in {"business", "software"}:
        raise ValueError("Unknown environment kind")
    root = Path(
        os.environ.get(
            "MAC_BENCH_ENVIRONMENT_CACHE", repository_root() / ".cache/environments"
        )
    )
    return (
        root / kind / scope["provider"] / recipe_key({"recipe": recipe, "scope": scope})
    )


def write_record(path, image):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="image-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(asdict(image), stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_cached_image(backend, *, kind, recipe, build, rebuild=False):
    directory = image_directory(kind, backend.image_scope, recipe)
    directory.mkdir(parents=True, exist_ok=True)
    record = directory / "image.json"
    with (directory / "prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not rebuild and record.is_file():
            image = PreparedImage(**json.loads(record.read_text()))
            backend.validate_image(image)
            if image.recipe != recipe:
                raise ValueError("Prepared image recipe differs from its cache key")
            if backend.image_exists(image):
                return image
        image = build(directory)
        backend.validate_image(image)
        if image.recipe != recipe:
            raise ValueError("Captured image does not match the preparation recipe")
        write_record(record, image)
        return image
