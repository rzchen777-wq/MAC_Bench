"""Download task archives and their shared environment files."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import urlopen


def download_file(url, destination, checksum):
    destination = Path(destination)
    if destination.exists():
        with destination.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == checksum:
                return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            digest = hashlib.sha256()
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != checksum:
            raise ValueError("Download checksum differs: " + destination.name)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def download_tasks(root, url, rows):
    from mac_bench.data_cli import install_archive

    root = Path(root)
    with urlopen(url, timeout=30) as response:
        manifest = json.load(response)
    available = {row["task_id"]: row for row in manifest["tasks"]}
    assets = manifest.get("assets", {})
    for row in rows:
        item = available[row["task_id"]]
        archive = root / ".cache/downloads" / (row["task_id"] + ".tar.gz")
        download_file(urljoin(url, item["archive"]), archive, item["sha256"])
        install_archive(archive, root / "data" / row["bundle"], row)
        for name in item.get("environment_assets", []):
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Invalid environment file path")
            asset = assets[name]
            download_file(
                urljoin(url, asset["url"]), root / "data" / path, asset["sha256"]
            )
        print("Installed", row["task_id"])
